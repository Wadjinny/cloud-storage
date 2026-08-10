"""Resolve multi-select into streaming ZIP members (store-like, no full-file RAM)."""

from __future__ import annotations

import logging
import threading
import zlib
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any

from stream_zip import ZIP_AUTO, stream_zip

from config import ZIP_PREFETCH_FILES, ZIP_PREFETCH_MAX_FILE_BYTES
from services import vault as vault_service
from storage import auth_context
from storage import folders as folders_store
from storage import vault_index

log = logging.getLogger(__name__)

ProgressFn = Callable[..., None]


def parent_dir(path: str) -> str:
    norm = path if path.startswith("/") else f"/{path}"
    parent = str(PurePosixPath(norm).parent)
    return "/" if parent in (".", "") else parent


def common_parent_dir(paths: list[str]) -> str:
    """Longest common directory prefix of absolute vault paths."""
    if not paths:
        return "/"
    split: list[list[str]] = []
    for p in paths:
        norm = p if p.startswith("/") else f"/{p}"
        parts = [x for x in PurePosixPath(norm).parts if x != "/"]
        split.append(parts)
    common: list[str] = []
    for segment in zip(*split, strict=False):
        if len(set(segment)) != 1:
            break
        common.append(segment[0])
    if not common:
        return "/"
    return "/" + "/".join(common)


def zip_name_from_base(base: str) -> str:
    if base == "/":
        return "vault.zip"
    return f"{PurePosixPath(base).name}.zip"


def _safe_arcname(base: str, absolute_path: str, *, is_dir: bool = False) -> str:
    """Path relative to ``base``; never absolute / never ``..``."""
    base_n = folders_store.normalize_dir(base)
    path = absolute_path if absolute_path.startswith("/") else f"/{absolute_path}"
    if is_dir:
        path = folders_store.normalize_dir(path)
    if base_n == "/":
        rel = path.lstrip("/")
    elif path == base_n:
        rel = ""
    elif path.startswith(base_n + "/"):
        rel = path[len(base_n) + 1 :]
    else:
        rel = PurePosixPath(path).name
    rel = rel.replace("\\", "/").lstrip("/")
    while rel.startswith("../"):
        rel = rel.removeprefix("../")
    if rel == "..":
        rel = ""
    if is_dir and rel and not rel.endswith("/"):
        rel += "/"
    return rel


def resolve_zip_selection(
    items: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]], list[str]]:
    """Expand UI selection into ZIP members.

    ``items``: ``{kind: 'file'|'dir', id?: str, path: str}``.

    Returns ``(zip_filename, file_members, empty_dir_arcnames)``.
    Each file member: ``{file_id, arcname, size, mtime}``.
    """
    if not items:
        raise ValueError("Nothing selected to download")

    roots: list[tuple[str, str, str | None]] = []  # kind, path, optional file id
    for raw in items:
        kind = raw.get("kind") or "file"
        path = raw.get("path") or ""
        if not path:
            raise ValueError("Each item needs a path")
        file_id = raw.get("id")
        if kind == "dir":
            roots.append(("dir", folders_store.normalize_dir(path), None))
        else:
            p = path if path.startswith("/") else f"/{path}"
            roots.append(("file", p, file_id if isinstance(file_id, str) else None))

    if len(roots) == 1 and roots[0][0] == "dir":
        # Single folder: archive rooted at that folder, named after it.
        base = roots[0][1]
    else:
        # Common parent of selected roots (file → its parent for LCA).
        lca_inputs = [
            path if kind == "dir" else parent_dir(path) for kind, path, _ in roots
        ]
        base = common_parent_dir(lca_inputs)

    zip_filename = zip_name_from_base(base)

    files_by_id: dict[str, dict[str, Any]] = {}
    empty_dirs: set[str] = set()

    for kind, path, file_id in roots:
        if kind == "file":
            entry = None
            if file_id:
                entry = vault_index.find_file(None, file_id)
            if entry is None:
                entry = vault_index.find_file_by_path(path)
            if entry is None:
                raise FileNotFoundError(f"File not found: {path}")
            arc = _safe_arcname(base, entry["path"]) or PurePosixPath(entry["path"]).name
            files_by_id[entry["id"]] = {
                "file_id": entry["id"],
                "arcname": arc,
                "size": int(entry.get("size") or 0),
                "mtime": int(entry.get("mtime") or 0),
            }
            continue

        under = vault_index.list_files_under_prefix(path)
        if not under:
            arc = _safe_arcname(base, path, is_dir=True)
            if not arc and path != "/":
                arc = PurePosixPath(path).name + "/"
            if arc:
                empty_dirs.add(arc)
            continue
        for entry in under:
            arc = _safe_arcname(base, entry["path"]) or PurePosixPath(entry["path"]).name
            files_by_id[entry["id"]] = {
                "file_id": entry["id"],
                "arcname": arc,
                "size": int(entry.get("size") or 0),
                "mtime": int(entry.get("mtime") or 0),
            }

    members = sorted(files_by_id.values(), key=lambda m: m["arcname"])
    empty_list = sorted(empty_dirs)
    if not members and not empty_list:
        raise ValueError("Selection has no files to download")
    return zip_filename, members, empty_list


def _mtime_dt(mtime: int) -> datetime:
    if mtime <= 0:
        return datetime.now(tz=timezone.utc)
    return datetime.fromtimestamp(mtime, tz=timezone.utc)


class _ZipPrefetcher:
    """Download/decrypt upcoming zip members while the current one is packed.

    Zip writes stay sequential; this only overlaps provider IO. Whole-file
    buffers are used for members at or under ``max_file_bytes``; larger members
    are streamed when reached (no ahead buffer).
    """

    def __init__(
        self,
        *,
        master_key: bytes,
        user_id: str | None,
        members: list[dict[str, Any]],
        depth: int,
        max_file_bytes: int,
        should_abort: Callable[[], bool] | None,
    ) -> None:
        self._master_key = master_key
        self._user_id = user_id
        self._members = members
        self._depth = max(1, depth)
        self._max_file_bytes = max(0, max_file_bytes)
        self._should_abort = should_abort
        self._lock = threading.Lock()
        self._futures: dict[int, Future[list[bytes]]] = {}
        self._pool = ThreadPoolExecutor(
            max_workers=self._depth,
            thread_name_prefix="zip-prefetch",
        )
        self._closed = False

    def _aborted(self) -> bool:
        return bool(self._should_abort and self._should_abort())

    def _download_member(self, idx: int) -> list[bytes]:
        mem = self._members[idx]
        file_id = str(mem["file_id"])
        if self._user_id:
            with auth_context.auth_scope(self._user_id, self._master_key):
                return list(
                    vault_service.iter_download(
                        self._master_key,
                        file_id,
                        None,
                        should_abort=self._should_abort,
                    )
                )
        return list(
            vault_service.iter_download(
                self._master_key,
                file_id,
                None,
                should_abort=self._should_abort,
            )
        )

    def _can_prefetch(self, idx: int) -> bool:
        """Whole-file buffer only when under the size cap (keeps RAM bounded)."""
        if idx < 0 or idx >= len(self._members):
            return False
        size = int(self._members[idx].get("size") or 0)
        return size <= self._max_file_bytes

    def ensure(self, packing_idx: int) -> None:
        """Start ahead downloads only; the packing member streams live for progress."""
        if self._closed or self._aborted():
            return
        # depth includes the live packing member, so prefetch depth-1 ahead.
        ahead = max(0, self._depth - 1)
        start = packing_idx + 1
        end = min(len(self._members), start + ahead)
        with self._lock:
            for i in range(start, end):
                if i in self._futures:
                    continue
                if not self._can_prefetch(i):
                    continue
                self._futures[i] = self._pool.submit(self._download_member, i)

    def prefetching_names(self, packing_idx: int) -> list[str]:
        with self._lock:
            return [
                str(self._members[i]["arcname"])
                for i in sorted(self._futures)
                if i > packing_idx
            ]

    def take(
        self,
        idx: int,
        on_live_progress: ProgressFn | None = None,
    ) -> Iterator[bytes]:
        """Yield plaintext chunks for member ``idx`` (from prefetch or live stream)."""
        self.ensure(idx)
        with self._lock:
            fut = self._futures.pop(idx, None)
        if fut is not None:
            try:
                chunks = fut.result()
            except vault_service.DownloadCancelled:
                raise
            except Exception:
                log.exception("zip prefetch failed idx=%s; streaming live", idx)
                yield from self._stream_live(idx, on_live_progress)
                self.ensure(idx + 1)
                return
            self.ensure(idx + 1)
            yield from chunks
            return
        # Large file or over the size cap: stream while ahead members prefetch.
        yield from self._stream_live(idx, on_live_progress)
        self.ensure(idx + 1)

    def _stream_live(
        self,
        idx: int,
        on_progress: ProgressFn | None = None,
    ) -> Iterator[bytes]:
        mem = self._members[idx]
        file_id = str(mem["file_id"])
        if self._user_id:
            with auth_context.auth_scope(self._user_id, self._master_key):
                yield from vault_service.iter_download(
                    self._master_key,
                    file_id,
                    on_progress,
                    should_abort=self._should_abort,
                )
            return
        yield from vault_service.iter_download(
            self._master_key,
            file_id,
            on_progress,
            should_abort=self._should_abort,
        )

    def close(self) -> None:
        self._closed = True
        with self._lock:
            futures = list(self._futures.values())
            self._futures.clear()
        for fut in futures:
            fut.cancel()
        self._pool.shutdown(wait=False, cancel_futures=True)


def iter_zip_download(
    master_key: bytes,
    members: list[dict[str, Any]],
    empty_dirs: list[str] | None = None,
    on_progress: ProgressFn | None = None,
    *,
    should_abort: Callable[[], bool] | None = None,
    user_id: str | None = None,
) -> Iterator[bytes]:
    """Yield ZIP bytes; members are written sequentially with optional prefetch.

    Zip emission stays sequential. Up to ``ZIP_PREFETCH_FILES`` members may
    download/decrypt at once (whole-file buffers capped by
    ``ZIP_PREFETCH_MAX_FILE_BYTES``); larger members stream when reached.

    ``user_id`` is re-applied on each thread-pool hop (StreamingResponse) before
    touching the vault index, because zip headers can yield before file bytes.
    """
    total_files = max(len(members), 1)
    # Byte-weighted overall progress (empty members count as 1 so the bar can move).
    weights = [max(1, int(m.get("size") or 0)) for m in members]
    total_bytes = max(1, sum(weights))
    prefix_bytes = [0]
    for w in weights:
        prefix_bytes.append(prefix_bytes[-1] + w)
    empty_dirs = empty_dirs or []
    uid = user_id or auth_context.get_user_id()
    prefetch = _ZipPrefetcher(
        master_key=master_key,
        user_id=uid,
        members=members,
        depth=ZIP_PREFETCH_FILES,
        max_file_bytes=ZIP_PREFETCH_MAX_FILE_BYTES,
        should_abort=should_abort,
    )

    def _ensure_auth() -> None:
        if uid:
            auth_context.set_auth(uid, master_key)

    def _abort() -> bool:
        return bool(should_abort and should_abort())

    def member_files() -> Iterable[tuple[str, datetime, int, Any, Iterable[bytes]]]:
        try:
            for i, mem in enumerate(members):
                _ensure_auth()
                if _abort():
                    raise vault_service.DownloadCancelled("Download cancelled")
                arc = mem["arcname"]
                size = int(mem.get("size") or 0)
                mtime = _mtime_dt(int(mem.get("mtime") or 0))
                file_index = i

                def chunks(
                    idx: int = file_index,
                    name: str = arc,
                    declared_size: int = size,
                ) -> Iterator[bytes]:
                    _ensure_auth()
                    prefetch.ensure(idx)
                    base = prefix_bytes[idx]
                    weight = weights[idx]

                    def _zip_stats(
                        extra: dict[str, Any] | None = None,
                    ) -> dict[str, Any]:
                        out: dict[str, Any] = {
                            "zip_file_index": idx,
                            "zip_file_total": total_files,
                            "zip_arcname": name,
                            "zip_prefetching": prefetch.prefetching_names(idx),
                            "done_bytes": base,
                            "total_bytes": total_bytes,
                            # Always present so the transfer bar can parse stats
                            # even before the first shard breakdown arrives.
                            "done_shards": 0,
                            "expected_shards": 0,
                            "active_shards": 0,
                            "by_account": [],
                        }
                        if extra:
                            # Keep zip overall totals; merge per-file shard stats under.
                            for key, value in extra.items():
                                if key in ("done_bytes", "total_bytes"):
                                    continue
                                out[key] = value
                            if not isinstance(out.get("by_account"), list):
                                out["by_account"] = []
                            file_done = extra.get("done_bytes")
                            if isinstance(file_done, int) and file_done >= 0:
                                out["done_bytes"] = min(total_bytes, base + file_done)
                        return out

                    if on_progress:
                        on_progress(
                            "zip",
                            base,
                            total_bytes,
                            f"Packing {name} ({idx + 1}/{total_files})…",
                            _zip_stats(),
                        )

                    def file_progress(
                        phase: str,
                        current: int,
                        total: int,
                        message: str,
                        stats: dict[str, Any] | None = None,
                    ) -> None:
                        if on_progress is None:
                            return
                        file_done = 0
                        if stats and isinstance(stats.get("done_bytes"), int):
                            file_done = max(0, int(stats["done_bytes"]))
                        elif total > 0:
                            file_done = int(weight * min(max(current, 0), total) / total)
                        on_progress(
                            phase,
                            min(total_bytes, base + file_done),
                            total_bytes,
                            f"[{idx + 1}/{total_files}] {name}: {message}",
                            _zip_stats(stats),
                        )

                    written = 0
                    last_report = -1
                    for chunk in prefetch.take(idx, on_live_progress=file_progress):
                        if _abort():
                            raise vault_service.DownloadCancelled("Download cancelled")
                        written += len(chunk)
                        # Prefetched members have no live shard callbacks; report
                        # write progress so the bar still moves.
                        if on_progress and (
                            written == len(chunk)
                            or written - last_report >= 256 * 1024
                            or written >= declared_size
                        ):
                            last_report = written
                            on_progress(
                                "zip",
                                min(total_bytes, base + min(written, weight)),
                                total_bytes,
                                f"[{idx + 1}/{total_files}] {name}: writing…",
                                _zip_stats({"done_bytes": min(written, weight)}),
                            )
                        yield chunk

                    if on_progress:
                        on_progress(
                            "zip",
                            prefix_bytes[idx + 1],
                            total_bytes,
                            f"[{idx + 1}/{total_files}] {name}: done",
                            _zip_stats({"done_bytes": weight}),
                        )

                yield arc, mtime, 0o600, ZIP_AUTO(size), chunks()

            for dir_arc in empty_dirs:
                _ensure_auth()
                if _abort():
                    raise vault_service.DownloadCancelled("Download cancelled")
                name = dir_arc if dir_arc.endswith("/") else f"{dir_arc}/"
                yield name, datetime.now(tz=timezone.utc), 0o755, ZIP_AUTO(0), iter(())
        finally:
            prefetch.close()

    _ensure_auth()
    try:
        yield from stream_zip(
            member_files(),
            get_compressobj=lambda: zlib.compressobj(wbits=-zlib.MAX_WBITS, level=0),
        )
    finally:
        prefetch.close()
    if on_progress:
        on_progress(
            "done",
            total_bytes,
            total_bytes,
            "Done",
            {
                "zip_file_index": max(0, total_files - 1),
                "zip_file_total": total_files,
                "zip_arcname": members[-1]["arcname"] if members else None,
                "zip_prefetching": [],
                "done_bytes": total_bytes,
                "total_bytes": total_bytes,
                "done_shards": 0,
                "expected_shards": 0,
                "active_shards": 0,
                "by_account": [],
            },
        )
