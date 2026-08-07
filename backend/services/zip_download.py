"""Resolve multi-select into streaming ZIP members (store-like, no full-file RAM)."""

from __future__ import annotations

import logging
import zlib
from collections.abc import Callable, Iterable, Iterator
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any

from stream_zip import ZIP_AUTO, stream_zip

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

    root_paths = [p for _, p, _ in roots]
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


def iter_zip_download(
    master_key: bytes,
    members: list[dict[str, Any]],
    empty_dirs: list[str] | None = None,
    on_progress: ProgressFn | None = None,
    *,
    should_abort: Callable[[], bool] | None = None,
    user_id: str | None = None,
) -> Iterator[bytes]:
    """Yield ZIP bytes; each vault file is decrypted shard-by-shard into the archive.

    Uses deflate level 0 (store-like) so the stream stays O(shard) RAM and only
    the selected files' shards are fetched.

    ``user_id`` is re-applied on each thread-pool hop (StreamingResponse) before
    touching the vault index, because zip headers can yield before file bytes.
    """
    total_files = max(len(members), 1)
    empty_dirs = empty_dirs or []
    uid = user_id or auth_context.get_user_id()

    def _ensure_auth() -> None:
        if uid:
            auth_context.set_auth(uid, master_key)

    def _abort() -> bool:
        return bool(should_abort and should_abort())

    def member_files() -> Iterable[tuple[str, datetime, int, Any, Iterable[bytes]]]:
        for i, mem in enumerate(members):
            _ensure_auth()
            if _abort():
                raise vault_service.DownloadCancelled("Download cancelled")
            arc = mem["arcname"]
            size = int(mem.get("size") or 0)
            mtime = _mtime_dt(int(mem.get("mtime") or 0))
            file_index = i
            file_id = mem["file_id"]

            def chunks(
                fid: str = file_id,
                idx: int = file_index,
                name: str = arc,
            ) -> Iterator[bytes]:
                _ensure_auth()
                if on_progress:
                    on_progress(
                        "zip",
                        idx,
                        total_files,
                        f"Packing {name} ({idx + 1}/{total_files})…",
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
                    on_progress(
                        phase,
                        idx * 1000 + current,
                        total_files * 1000,
                        f"{name}: {message}",
                        stats,
                    )

                yield from vault_service.iter_download(
                    master_key,
                    fid,
                    file_progress,
                    should_abort=should_abort,
                )

            yield arc, mtime, 0o600, ZIP_AUTO(size), chunks()

        for dir_arc in empty_dirs:
            _ensure_auth()
            if _abort():
                raise vault_service.DownloadCancelled("Download cancelled")
            name = dir_arc if dir_arc.endswith("/") else f"{dir_arc}/"
            yield name, datetime.now(tz=timezone.utc), 0o755, ZIP_AUTO(0), iter(())

    _ensure_auth()
    yield from stream_zip(
        member_files(),
        get_compressobj=lambda: zlib.compressobj(wbits=-zlib.MAX_WBITS, level=0),
    )
    if on_progress:
        on_progress("done", total_files, total_files, "Done")
