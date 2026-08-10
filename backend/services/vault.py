from __future__ import annotations

import hashlib
import logging
import os
import queue
import re
import secrets
import tempfile
import threading
import time
from collections.abc import Callable
from concurrent.futures import (
    FIRST_COMPLETED,
    Future,
    ThreadPoolExecutor,
    as_completed,
    wait,
)
from contextvars import copy_context
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

from config import (
    DATA_DIR,
    DELETE_CONCURRENCY,
    DOWNLOAD_QUEUE_DEPTH,
    GDRIVE_ACCOUNT_DOWNLOAD_CONCURRENCY,
    GDRIVE_ACCOUNT_UPLOAD_CONCURRENCY,
    MEGA_ACCOUNT_DOWNLOAD_CONCURRENCY,
    MEGA_ACCOUNT_UPLOAD_CONCURRENCY,
    SHARD_DOWNLOAD_CONCURRENCY,
    SHARD_UPLOAD_CONCURRENCY,
    STORAGE_FULL_RATIO,
    THUMB_MAX_SOURCE_BYTES,
    THUMB_VIDEO_SPOOL_BYTES,
    choose_chunk_size,
)
from providers.factory import get_provider
from services import crypto
from services.capacity import PlacementPlanner, cipher_size
from services.jobs import JobCancelled, raise_if_cancelled
from services.thumbnails import attach_after_upload, schedule_after_upload
from services.thumbnails.classify import classify_filename
from storage import accounts as accounts_store
from storage import auth_context
from storage import folder_remotes
from storage import folders as folders_store
from storage import upload_sessions
from storage import vault_index

log = logging.getLogger(__name__)


class DownloadCancelled(RuntimeError):
    """Raised when a download job is aborted by the user or client disconnect."""


# phase, current, total, message, optional stats dict
ProgressFn = Callable[..., None]


def _progress(
    cb: ProgressFn | None,
    phase: str,
    current: int,
    total: int,
    message: str,
    *,
    stats: dict[str, Any] | None = None,
) -> None:
    if not cb:
        return
    try:
        cb(phase, current, total, message, stats)
    except TypeError:
        cb(phase, current, total, message)


def _providers_by_id(account_list: list[dict[str, Any]]) -> dict[str, Any]:
    return {a["id"]: get_provider(a) for a in account_list}


def discard_upload_session(session_id: str) -> None:
    """Delete a partial upload session and its per-account remote folders."""
    session = upload_sessions.load(session_id)
    if session is None:
        return
    account_list = accounts_store.load_accounts()
    providers = _providers_by_id(account_list)
    for account_id, folder_id in (session.get("remote_folders") or {}).items():
        provider = providers.get(account_id)
        if provider is None:
            continue
        try:
            provider.delete(folder_id)
        except Exception:
            log.exception(
                "discard_upload_session folder delete failed session=%s account=%s",
                session_id,
                account_id,
            )
    upload_sessions.delete(session_id)


def list_entries(path: str = "/") -> list[dict[str, Any]]:
    index = vault_index.load_index()
    return _list_entries_clean(index, _normalize_dir(path))


SEARCH_RESULT_LIMIT = 500


def search_entries(
    query: str,
    *,
    scope: str = "folder",
    path: str = "/",
    kind: str = "all",
    regex: bool = False,
) -> list[dict[str, Any]]:
    """Search vault entry names (case-insensitive).

    ``scope``: ``folder`` (recursive under ``path``) or ``global``.
    ``kind``: ``all`` | ``file`` | ``dir``.
    Matches basename only, not the full path.
    """
    q = (query or "").strip()
    if not q:
        return []
    if scope not in ("folder", "global"):
        raise ValueError("scope must be 'folder' or 'global'")
    if kind not in ("all", "file", "dir"):
        raise ValueError("kind must be 'all', 'file', or 'dir'")

    if regex:
        # Common glob habit: bare "*" means "match anything".
        pattern_src = ".*" if q == "*" else q
        try:
            pattern = re.compile(pattern_src, re.IGNORECASE)
        except re.error as exc:
            raise ValueError(f"Invalid regex: {exc}") from exc

        def matches(name: str) -> bool:
            return pattern.search(name) is not None
    else:
        needle = q.casefold()

        def matches(name: str) -> bool:
            return needle in name.casefold()

    base = "/" if scope == "global" else _normalize_dir(path)
    index = vault_index.load_index()
    results: list[dict[str, Any]] = []

    if kind in ("all", "dir"):
        dirs_by_path: dict[str, dict[str, Any]] = {}
        for folder in folders_store.list_folders_under(base):
            dirs_by_path[folder["path"]] = {
                "id": folder["id"],
                "name": folder["name"],
                "path": folder["path"],
                "size": None,
                "mtime": folder.get("mtime"),
                "kind": "dir",
                "shard_count": 0,
            }
        # Implied intermediate folders from file paths (not yet in folders table).
        for entry in index.get("files", []):
            file_path = str(entry.get("path") or "")
            if not _path_under_base(file_path, base):
                continue
            parts = [p for p in PurePosixPath(file_path).parts if p != "/"]
            for i in range(1, len(parts)):
                dir_path = "/" + "/".join(parts[:i])
                if not _path_under_base(dir_path, base):
                    continue
                if dir_path in dirs_by_path:
                    continue
                dirs_by_path[dir_path] = {
                    "id": f"dir:{dir_path}",
                    "name": parts[i - 1],
                    "path": dir_path,
                    "size": None,
                    "mtime": None,
                    "kind": "dir",
                    "shard_count": 0,
                }
        for dir_path in sorted(dirs_by_path):
            item = dirs_by_path[dir_path]
            if matches(item["name"]):
                results.append(item)
                if len(results) >= SEARCH_RESULT_LIMIT:
                    return results

    if kind in ("all", "file"):
        files: list[dict[str, Any]] = []
        for entry in index.get("files", []):
            file_path = str(entry.get("path") or "")
            if not _path_under_base(file_path, base):
                continue
            name = PurePosixPath(file_path).name
            if not matches(name):
                continue
            files.append(
                {
                    "id": entry["id"],
                    "name": name,
                    "path": file_path,
                    "size": entry.get("size"),
                    "mtime": entry.get("mtime"),
                    "kind": "file",
                    "shard_count": len(entry.get("shards", [])),
                    "thumb_status": entry.get("thumb_status") or "none",
                }
            )
        files.sort(key=lambda f: (f["name"].casefold(), f["path"]))
        for item in files:
            results.append(item)
            if len(results) >= SEARCH_RESULT_LIMIT:
                break

    return results


def _path_under_base(path: str, base: str) -> bool:
    """True if ``path`` is ``base`` or a descendant (path form, not name match)."""
    if base == "/":
        return bool(path) and path.startswith("/")
    if path == base:
        return True
    return path.startswith(base + "/")


def mkdir(parent: str, name: str) -> dict[str, Any]:
    parent_norm = _normalize_dir(parent)
    clean = folders_store.validate_name(name)
    if any(
        e["kind"] == "dir" and e["name"] == clean for e in list_entries(parent_norm)
    ):
        raise ValueError("Folder already exists")
    if any(
        e["kind"] == "file" and e["name"] == clean for e in list_entries(parent_norm)
    ):
        raise ValueError("A file already exists at that path")
    folder = folders_store.mkdir(parent_norm, clean)
    return {
        "id": folder["id"],
        "name": PurePosixPath(folder["path"]).name,
        "path": folder["path"],
        "size": None,
        "mtime": folder.get("mtime"),
        "kind": "dir",
        "shard_count": 0,
    }


def _dir_exists(folder_path: str) -> bool:
    """True if folder is registered or implied by nested files/folders."""
    norm = folders_store.normalize_dir(folder_path)
    if norm == "/":
        return True
    if folders_store.get_folder(norm) is not None:
        return True
    if vault_index.list_files_under_prefix(norm):
        return True
    # Child folders under this path.
    for child in folders_store.list_child_names(norm):
        if child["path"].startswith(norm + "/") or child["path"] == norm:
            return True
    # Nested folder rows deeper than one level.
    parent = folders_store.normalize_dir(str(PurePosixPath(norm).parent))
    name = PurePosixPath(norm).name
    return any(c["name"] == name for c in folders_store.list_child_names(parent))


def _dest_occupied(path: str, *, moving_prefix: str | None = None) -> str | None:
    """Return 'file' / 'folder' if path is taken, else None.

    Paths under ``moving_prefix`` are ignored (they move with the source).
    """
    if vault_index.find_file_by_path(path) is not None:
        if moving_prefix and (
            path == moving_prefix or path.startswith(moving_prefix + "/")
        ):
            pass
        else:
            return "file"
    if _dir_exists(path):
        if moving_prefix and (
            path == moving_prefix or path.startswith(moving_prefix + "/")
        ):
            return None
        return "folder"
    return None


def move_entry(*, src_path: str, dest_path: str, kind: str | None = None) -> dict[str, Any]:
    """Move/rename a file or folder (DB path rewrite; remote shards unchanged)."""
    src_raw = src_path.rstrip("/") if src_path != "/" else src_path
    file_entry = vault_index.find_file_by_path(src_raw)
    folder_path = folders_store.normalize_dir(src_path)

    resolved_kind = kind
    if resolved_kind not in ("file", "dir"):
        if file_entry is not None:
            resolved_kind = "file"
        elif _dir_exists(folder_path):
            resolved_kind = "dir"
        else:
            raise FileNotFoundError("Path not found")

    if resolved_kind == "file":
        if file_entry is None:
            raise FileNotFoundError("File not found")
        return _move_file(file_entry, dest_path)
    return _move_folder(folder_path, dest_path)


def _move_file(entry: dict[str, Any], dest_path: str) -> dict[str, Any]:
    dest = dest_path.rstrip("/") if dest_path != "/" else dest_path
    if not dest.startswith("/"):
        dest = "/" + dest
    folders_store.validate_name(PurePosixPath(dest).name)
    parent = folders_store.normalize_dir(str(PurePosixPath(dest).parent))
    if parent != "/":
        folders_store.ensure_folder(parent)
    occupied = _dest_occupied(dest)
    if occupied:
        raise ValueError(f"A {occupied} already exists at the destination")
    updated = vault_index.rename_file_path(entry["id"], dest)
    assert updated is not None
    return {
        "id": updated["id"],
        "name": PurePosixPath(dest).name,
        "path": dest,
        "kind": "file",
        "size": updated.get("size"),
        "mtime": updated.get("mtime"),
        "shard_count": len(updated.get("shards") or []),
    }


def _move_folder(src_path: str, dest_path: str) -> dict[str, Any]:
    src = folders_store.normalize_dir(src_path)
    dest = folders_store.normalize_dir(dest_path)
    if src == "/":
        raise ValueError("Cannot move root")
    if dest == "/":
        raise ValueError("Invalid destination")
    if not _dir_exists(src):
        raise FileNotFoundError("Folder not found")
    if dest == src:
        return {
            "id": f"dir:{dest}",
            "name": PurePosixPath(dest).name,
            "path": dest,
            "kind": "dir",
            "size": None,
            "mtime": None,
            "shard_count": 0,
        }
    if dest.startswith(src + "/"):
        raise ValueError("Cannot move a folder into itself")
    folders_store.validate_name(PurePosixPath(dest).name)
    occupied = _dest_occupied(dest, moving_prefix=src)
    if occupied:
        raise ValueError(f"A {occupied} already exists at the destination")

    parent = folders_store.normalize_dir(str(PurePosixPath(dest).parent))
    if parent != "/":
        folders_store.ensure_folder(parent)

    vault_index.rename_files_under_prefix(src, dest)
    folders_store.rename_folder_tree(src, dest)
    folders_store.ensure_folder(dest)
    return {
        "id": f"dir:{dest}",
        "name": PurePosixPath(dest).name,
        "path": dest,
        "kind": "dir",
        "size": None,
        "mtime": None,
        "shard_count": 0,
    }


def delete_directory(path: str, on_progress: ProgressFn | None = None) -> dict[str, Any]:
    """Recursively delete a folder: all files under it, then folder rows.

    New uploads nest per-file remote folders under opaque vault-dir folders on
    each provider. When every file under ``path`` was placed in that tree (no
    legacy flat remotes, no files moved out while leaving remotes behind), delete
    is one remote folder remove per account. Otherwise fall back to per-file
    deletes (move leaves remotes in place by design).
    """
    folder_path = folders_store.normalize_dir(path)
    if folder_path == "/":
        raise ValueError("Cannot delete root")
    if not _dir_exists(folder_path):
        raise FileNotFoundError("Folder not found")

    files = vault_index.list_files_under_prefix(folder_path)
    dir_ids = set(folders_store.list_folder_ids_under(folder_path))
    root_folder = folders_store.get_folder(folder_path)
    root_folder_id = root_folder["id"] if root_folder else None
    root_remotes = (
        folder_remotes.load_for_folder(root_folder_id) if root_folder_id else {}
    )

    under_ids = {f["id"] for f in files}
    placed = folder_remotes.list_files_placed_under(dir_ids)
    strays = [p for p in placed if p["id"] not in under_ids]
    legacy = [
        f
        for f in files
        if not f.get("remote_vault_dir_id") and (f.get("remote_folders") or f.get("shards"))
    ]
    can_fast = (
        bool(files)
        and bool(root_remotes)
        and not strays
        and not legacy
        and all(f.get("remote_vault_dir_id") in dir_ids for f in files)
    ) or (not files and bool(root_remotes))

    if can_fast and files:
        raise_if_cancelled("Delete cancelled")
        total = max(1, len(root_remotes) + 1)
        _progress(on_progress, "delete_shard", 0, total, "Deleting remote folder tree…")
        account_list = accounts_store.load_accounts()
        providers = _providers_by_id(
            [a for a in account_list if a["id"] in root_remotes]
        )
        done = 0
        for account_id, remote_id in root_remotes.items():
            raise_if_cancelled("Delete cancelled")
            provider = providers.get(account_id)
            if provider is not None:
                try:
                    provider.delete(remote_id)
                except Exception:
                    log.exception(
                        "fast folder delete failed path=%s account=%s remote=%s",
                        folder_path,
                        account_id,
                        remote_id,
                    )
            done += 1
            _progress(
                on_progress,
                "delete_shard",
                done,
                total,
                f"Deleted remote tree {done}/{len(root_remotes)}…",
            )
        index = vault_index.load_index()
        for entry in files:
            vault_index.remove_file(index, entry["id"])
        vault_index.save_index(index)
        folder_remotes.delete_for_folders(dir_ids)
        folders_store.delete_folder_tree(folder_path)
        _progress(on_progress, "done", total, total, "Done")
        return {"path": folder_path, "deleted_files": len(files), "mode": "fast"}

    total = max(1, len(files) + 1)
    user_id = auth_context.require_user_id()
    master_key = auth_context.require_master_key()
    account_list = accounts_store.load_accounts()
    needed_ids: set[str] = set()
    for entry in files:
        needed_ids.update(entry.get("remote_folders") or {})
        needed_ids.update(s["account_id"] for s in entry.get("shards") or [])
        thumb = entry.get("thumb") or {}
        if thumb.get("account_id"):
            needed_ids.add(thumb["account_id"])
    providers_by_id = _providers_by_id(
        [a for a in account_list if a["id"] in needed_ids]
    )
    workers = max(1, min(DELETE_CONCURRENCY, max(1, len(files))))
    done_count = 0
    progress_lock = threading.Lock()
    _progress(on_progress, "delete_shard", 0, total, f"Deleting {len(files)} file(s)…")
    raise_if_cancelled("Delete cancelled")

    def _delete_one(entry: dict[str, Any]) -> None:
        raise_if_cancelled("Delete cancelled")
        with auth_context.auth_scope(user_id, master_key):
            delete_file(
                entry["id"],
                on_progress=None,
                providers_by_id=providers_by_id,
                parallel_remotes=False,
            )

    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = {
            pool.submit(copy_context().run, _delete_one, entry): entry for entry in files
        }
        for fut in as_completed(futures):
            raise_if_cancelled("Delete cancelled")
            entry = futures[fut]
            fut.result()
            with progress_lock:
                done_count += 1
                name = PurePosixPath(entry["path"]).name
                _progress(
                    on_progress,
                    "delete_shard",
                    done_count,
                    total,
                    f"Deleted {name} ({done_count}/{len(files)})…",
                )
    except JobCancelled:
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    else:
        pool.shutdown(wait=True, cancel_futures=False)

    # Drop mirrored dir folders when safe (no moved-out files still using them).
    raise_if_cancelled("Delete cancelled")
    if root_remotes and not strays:
        providers = _providers_by_id(
            [a for a in account_list if a["id"] in root_remotes]
        )
        for account_id, remote_id in root_remotes.items():
            provider = providers.get(account_id)
            if provider is None:
                continue
            try:
                provider.delete(remote_id)
            except Exception:
                log.exception(
                    "folder remote cleanup failed path=%s account=%s remote=%s",
                    folder_path,
                    account_id,
                    remote_id,
                )
        folder_remotes.delete_for_folders(dir_ids)
    folders_store.delete_folder_tree(folder_path)
    _progress(on_progress, "done", total, total, "Done")
    return {"path": folder_path, "deleted_files": len(files), "mode": "per_file"}


def get_file_detail(file_id: str) -> dict[str, Any]:
    index = vault_index.load_index()
    entry = vault_index.find_file(index, file_id)
    if entry is None:
        raise FileNotFoundError("File not found")

    account_map = {a["id"]: a for a in accounts_store.load_accounts()}
    shards = sorted(entry.get("shards", []), key=lambda s: s["i"])
    by_account: dict[str, dict[str, Any]] = {}
    for shard_rec in shards:
        account_id = shard_rec["account_id"]
        bucket = by_account.get(account_id)
        if bucket is None:
            account = account_map.get(account_id)
            bucket = {
                "account_id": account_id,
                "provider": account["provider"] if account else "unknown",
                "label": account["label"] if account else "(missing account)",
                "count": 0,
                "shard_indices": [],
            }
            by_account[account_id] = bucket
        bucket["count"] += 1
        bucket["shard_indices"].append(shard_rec["i"])

    return {
        "id": entry["id"],
        "path": entry["path"],
        "name": PurePosixPath(entry["path"]).name,
        "size": entry["size"],
        "mtime": entry.get("mtime"),
        "chunk_size": entry.get("chunk_size"),
        "alg": entry.get("crypto", {}).get("alg"),
        "thumb_status": entry.get("thumb_status") or "none",
        "shard_count": len(shards),
        "shards_by_account": sorted(
            by_account.values(),
            key=lambda b: (b["provider"], b["label"]),
        ),
        "shards": [
            {
                "i": s["i"],
                "account_id": s["account_id"],
                "provider": account_map[s["account_id"]]["provider"]
                if s["account_id"] in account_map
                else "unknown",
                "label": account_map[s["account_id"]]["label"]
                if s["account_id"] in account_map
                else "(missing account)",
                "remote_id": s["remote_id"],
                "sha256": s["sha256"],
            }
            for s in shards
        ],
    }


def _list_entries_clean(index: dict[str, Any], base: str) -> list[dict[str, Any]]:
    dirs: dict[str, dict[str, Any]] = {}
    files: list[dict[str, Any]] = []
    base_parts = () if base == "/" else tuple(p for p in PurePosixPath(base).parts if p != "/")

    for child in folders_store.list_child_names(base):
        dirs[child["name"]] = {
            "id": child["id"],
            "name": child["name"],
            "path": child["path"],
            "size": None,
            "mtime": child.get("mtime"),
            "kind": "dir",
            "shard_count": 0,
        }

    for entry in index.get("files", []):
        clean_parts = tuple(p for p in PurePosixPath(entry["path"]).parts if p != "/")
        if clean_parts[: len(base_parts)] != base_parts:
            continue
        rest = clean_parts[len(base_parts) :]
        if not rest:
            continue
        if len(rest) == 1:
            files.append(
                {
                    "id": entry["id"],
                    "name": rest[0],
                    "path": entry["path"],
                    "size": entry["size"],
                    "mtime": entry.get("mtime"),
                    "kind": "file",
                    "shard_count": len(entry.get("shards", [])),
                    "thumb_status": entry.get("thumb_status") or "none",
                }
            )
        else:
            name = rest[0]
            if name not in dirs:
                path = f"{base.rstrip('/')}/{name}" if base != "/" else f"/{name}"
                dirs[name] = {
                    "id": f"dir:{path}",
                    "name": name,
                    "path": path,
                    "size": None,
                    "mtime": None,
                    "kind": "dir",
                    "shard_count": 0,
                }

    result: list[dict[str, Any]] = [dirs[name] for name in sorted(dirs)]
    result.extend(sorted(files, key=lambda f: f["name"]))
    return result


def _normalize_dir(path: str) -> str:
    return folders_store.normalize_dir(path)


def _join_path(dir_path: str, name: str) -> str:
    return folders_store.join_path(dir_path, name)


def _worker_count(n_tasks: int, *, download: bool = False) -> int:
    limit = SHARD_DOWNLOAD_CONCURRENCY if download else SHARD_UPLOAD_CONCURRENCY
    return max(1, min(limit, max(1, n_tasks)))


def _provider_account_limit(provider: str, *, workers: int, download: bool = False) -> int:
    """Per-account in-flight cap for a provider, never above the job worker pool."""
    if provider == "mega":
        configured = (
            MEGA_ACCOUNT_DOWNLOAD_CONCURRENCY
            if download
            else MEGA_ACCOUNT_UPLOAD_CONCURRENCY
        )
    elif provider == "gdrive":
        configured = (
            GDRIVE_ACCOUNT_DOWNLOAD_CONCURRENCY
            if download
            else GDRIVE_ACCOUNT_UPLOAD_CONCURRENCY
        )
    else:
        configured = workers
    return max(1, min(workers, max(1, configured)))


def _iter_plaintext_chunks(source: BinaryIO, chunk_size: int) -> Any:
    """Yield ``chunk_size`` plaintext pieces from a binary stream (one empty chunk if empty)."""
    saw_data = False
    while True:
        plain = source.read(chunk_size)
        if plain:
            saw_data = True
            yield plain
            if len(plain) < chunk_size:
                break
        elif saw_data:
            break
        else:
            yield b""
            break


def upload_file(
    master_key: bytes,
    dir_path: str,
    filename: str,
    chunks: Any,
    *,
    size: int | None = None,
    chunk_size: int | None = None,
    on_progress: ProgressFn | None = None,
    should_abort: Callable[[], bool] | None = None,
    on_chunk_done: Callable[[], None] | None = None,
    session_id: str | None = None,
    resume_from_byte: int = 0,
) -> dict[str, Any]:
    """Encrypt and upload from an iterable of plaintext chunks.

    ``chunks`` yields up to ``chunk_size`` bytes each (final chunk may be shorter;
    empty files yield a single ``b""``). At most ``SHARD_UPLOAD_CONCURRENCY`` encrypt
    uploads run at once, and the next chunk is only pulled once a slot is free
    so RAM stays O(concurrency × shard size) regardless of file size.

    When ``session_id`` refers to a persisted upload session, completed shards are
    skipped and ``resume_from_byte`` is the file offset of the first chunk in
    ``chunks`` (0 if the client re-streams the whole file).
    """
    shard_size = chunk_size if chunk_size is not None else choose_chunk_size(size)
    if shard_size < 1:
        raise ValueError("chunk_size must be >= 1")
    if resume_from_byte < 0 or (
        resume_from_byte > 0 and resume_from_byte % shard_size != 0
    ):
        raise ValueError("resume_from_byte must be a non-negative multiple of chunk_size")

    account_list = accounts_store.require_enabled_accounts()

    # Capture for worker threads (ThreadPoolExecutor does not inherit contextvars).
    user_id = auth_context.require_user_id()

    planner = PlacementPlanner.from_accounts(account_list, user_id=user_id)
    providers_by_id = _providers_by_id(account_list)

    existing = upload_sessions.load(session_id) if session_id else None
    if existing is not None:
        file_id = existing["id"]
        file_key = crypto.unwrap_file_key(
            master_key, existing["crypto"]["wrapped_file_key"]
        )
        if size is None:
            size = existing.get("size")
        shard_size = int(existing["chunk_size"])
        dir_path = existing.get("path") or dir_path
        filename = existing.get("filename") or filename
        shard_records = {int(s["i"]): s for s in existing.get("shards", [])}
        remote_folders = dict(existing.get("remote_folders") or {})
        existing["user_id"] = user_id
        existing["status"] = "active"
        existing["error"] = None
    else:
        file_id = session_id or f"f_{secrets.token_hex(8)}"
        file_key = crypto.new_file_key()
        shard_records = {}
        remote_folders = {}

    # Opaque vault-dir chain for nesting remotes (empty when uploading to ``/``).
    vault_dir_ids = folders_store.folder_id_chain(_normalize_dir(dir_path))
    remote_vault_dir_id = vault_dir_ids[-1] if vault_dir_ids else None

    if existing is None:
        upload_sessions.save(
            {
                "version": 1,
                "id": file_id,
                "user_id": user_id,
                "status": "active",
                "path": _normalize_dir(dir_path),
                "filename": filename,
                "size": size,
                "chunk_size": shard_size,
                "crypto": {
                    "alg": crypto.ALG_CHUNK,
                    "wrapped_file_key": crypto.wrap_file_key(master_key, file_key),
                },
                "remote_folders": {},
                "remote_vault_dir_id": remote_vault_dir_id,
                "shards": [],
                "error": None,
            }
        )
    else:
        existing["remote_vault_dir_id"] = (
            existing.get("remote_vault_dir_id") or remote_vault_dir_id
        )
        upload_sessions.save(existing)

    start_chunk = resume_from_byte // shard_size if resume_from_byte else 0

    if size is not None and size >= 0:
        expected_chunks = max(1, (size + shard_size - 1) // shard_size) if size > 0 else 1
        total_steps = max(1, size) + 1
        # Plaintext plus one GCM tag per chunk.
        estimated_cipher = size + cipher_size(0) * expected_chunks
        placeable = planner.placeable_total()
        if placeable is not None and estimated_cipher > placeable and existing is None:
            ratio_pct = int(round(STORAGE_FULL_RATIO * 100))
            raise ValueError(
                f"Not enough free space under the {ratio_pct}% full threshold "
                f"(need ~{estimated_cipher} bytes, ~{placeable} bytes placeable)"
            )
    else:
        expected_chunks = None
        total_steps = 1

    # Soft per-account shard balance (skip when total shard count is unknown).
    balance_cap = planner.configure_balance(expected_chunks)
    seeded: dict[str, int] = {}
    if shard_records:
        for rec in shard_records.values():
            aid = rec["account_id"]
            seeded[aid] = seeded.get(aid, 0) + 1
        planner.seed_shards(seeded)
    if balance_cap is not None:
        log.info(
            "upload_file shard balance cap=%s placeable=%s expected_chunks=%s "
            "seeded=%s",
            balance_cap,
            len(planner.placeable_accounts()),
            expected_chunks,
            seeded,
        )

    done_bytes = 0
    if size is not None and size >= 0 and expected_chunks:
        for i in sorted(shard_records):
            if i < expected_chunks - 1:
                done_bytes += shard_size
            else:
                done_bytes += max(0, size - i * shard_size)
    else:
        done_bytes = len(shard_records) * shard_size
    done_chunks = len(shard_records)
    # Per-account shards currently encrypting/uploading (not yet recorded as done).
    inflight_by_account: dict[str, int] = {}

    progress_lock = threading.Lock()
    workers = _worker_count(expected_chunks or SHARD_UPLOAD_CONCURRENCY)
    in_flight: dict[Future[dict[str, Any]], int] = {}
    folders_lock = threading.Lock()
    folder_create_locks: dict[str, threading.Lock] = {}
    session_lock = threading.Lock()

    def _persist_session(*, status: str | None = None, error: str | None = None) -> None:
        with folders_lock:
            folders_snapshot = dict(remote_folders)
        with progress_lock:
            shards_snapshot = [shard_records[i] for i in sorted(shard_records)]
        with session_lock:
            session = {
                "version": 1,
                "id": file_id,
                "user_id": user_id,
                "status": status or "active",
                "path": _normalize_dir(dir_path),
                "filename": filename,
                "size": size,
                "chunk_size": shard_size,
                "crypto": {
                    "alg": crypto.ALG_CHUNK,
                    "wrapped_file_key": crypto.wrap_file_key(master_key, file_key),
                },
                "remote_folders": folders_snapshot,
                "remote_vault_dir_id": remote_vault_dir_id,
                "shards": shards_snapshot,
                "error": error,
            }
            upload_sessions.save(session)

    def _upload_stats() -> dict[str, Any]:
        """Per-account shard/byte breakdown for the live progress UI.

        Call under progress_lock after shard_records / done_bytes updates.
        """
        n_accounts = max(1, len(account_list))
        finished = len(shard_records)
        est_total = expected_chunks or max(finished, 1)
        # Round-robin placement → roughly equal share per connected account.
        estimated_each = (est_total + n_accounts - 1) // n_accounts
        by_id: dict[str, dict[str, Any]] = {
            a["id"]: {
                "account_id": a["id"],
                "provider": a["provider"],
                "label": a.get("label") or a["id"],
                "shards": 0,
                "active": inflight_by_account.get(a["id"], 0),
                "bytes": 0,
                "estimated_shards": estimated_each,
            }
            for a in account_list
        }
        for idx, rec in shard_records.items():
            aid = rec["account_id"]
            bucket = by_id.get(aid)
            if bucket is None:
                continue
            bucket["shards"] += 1
            if size is not None and size >= 0 and expected_chunks:
                if idx < expected_chunks - 1:
                    bucket["bytes"] += shard_size
                else:
                    bucket["bytes"] += max(0, size - idx * shard_size)
            else:
                bucket["bytes"] += shard_size
        return {
            "done_shards": finished,
            "expected_shards": expected_chunks,
            "done_bytes": done_bytes,
            "total_bytes": size,
            "chunk_size": shard_size,
            "active_shards": sum(inflight_by_account.values()),
            "by_account": sorted(
                by_id.values(),
                key=lambda b: (b["provider"], b["label"]),
            ),
        }

    _progress(
        on_progress,
        "upload_shard",
        min(done_bytes, max(1, size or done_bytes or 1)),
        total_steps,
        f"Resuming… {done_chunks} shards already uploaded"
        if done_chunks
        else "Encrypting and uploading…",
        stats=_upload_stats(),
    )

    def _aborted() -> bool:
        return bool(should_abort and should_abort())

    def _cleanup_remote_folders() -> None:
        with folders_lock:
            folders = list(remote_folders.items())
        for account_id, folder_id in folders:
            try:
                providers_by_id[account_id].delete(folder_id)
                log.info(
                    "upload cleanup deleted folder file=%s account=%s folder=%s",
                    file_id,
                    account_id,
                    folder_id,
                )
            except Exception:
                log.exception(
                    "upload cleanup folder delete failed file=%s account=%s folder=%s",
                    file_id,
                    account_id,
                    folder_id,
                )
        upload_sessions.delete(file_id)

    def _ensure_remote_dir_parent(account_id: str) -> str | None:
        """Ensure mirrored vault-dir folders on ``account_id``; return leaf remote id."""
        if not vault_dir_ids:
            return None
        parent_remote: str | None = None
        for vault_dir_id in vault_dir_ids:
            cached = folder_remotes.get(vault_dir_id, account_id)
            if cached is not None:
                parent_remote = cached
                continue
            parent_remote = providers_by_id[account_id].ensure_child_folder(
                vault_dir_id,
                parent_id=parent_remote,
            )
            folder_remotes.put(vault_dir_id, account_id, parent_remote)
        return parent_remote

    def _folder_for(account_id: str) -> str:
        with folders_lock:
            existing_folder = remote_folders.get(account_id)
            if existing_folder is not None:
                return existing_folder
            create_lock = folder_create_locks.setdefault(account_id, threading.Lock())
        with create_lock:
            with folders_lock:
                existing_folder = remote_folders.get(account_id)
                if existing_folder is not None:
                    return existing_folder
            parent_remote = _ensure_remote_dir_parent(account_id)
            folder_id = providers_by_id[account_id].ensure_file_folder(
                file_id,
                parent_id=parent_remote,
            )
            with folders_lock:
                remote_folders[account_id] = folder_id
            _persist_session()
            return folder_id

    account_meta = {a["id"]: a for a in account_list}
    # Cap in-flight ops per account (MEGA/Drive upload concurrency envs).
    account_limits: dict[str, threading.Semaphore] = {}
    for account in account_list:
        limit = _provider_account_limit(account["provider"], workers=workers)
        account_limits[account["id"]] = threading.Semaphore(limit)

    def _encrypt_upload(
        i: int,
        plain: bytes,
        account_id: str,
        *,
        release_slot: bool,
    ) -> dict[str, Any]:
        """Encrypt+upload one shard. Caller already holds account_limits[account_id]."""
        nonlocal done_bytes, done_chunks
        t0 = time.monotonic()
        label = account_meta.get(account_id, {}).get("label") or account_id
        provider = account_meta.get(account_id, {}).get("provider") or "?"
        marked_active = True
        try:
            if _aborted():
                raise RuntimeError("Upload cancelled")
            ciphertext, nonce = crypto.encrypt_chunk(file_key, plain)
            # Drop plaintext ref before network I/O so peak ≈ concurrency × shard.
            plain_len = len(plain)
            del plain
            remote_name = f"shard_{file_id}_{i}"
            cipher_len = len(ciphertext)
            parent_id = _folder_for(account_id)
            log.info(
                "shard upload start file=%s i=%s%s plain=%s cipher=%s → %s:%s folder=%s",
                file_id,
                i + 1,
                f"/{expected_chunks}" if expected_chunks else "",
                plain_len,
                cipher_len,
                provider,
                label,
                parent_id,
            )
            try:
                remote_id = providers_by_id[account_id].upload(
                    remote_name,
                    ciphertext,
                    parent_id=parent_id,
                )
            except Exception:
                log.exception(
                    "shard upload failed file=%s i=%s → %s:%s elapsed=%.2fs",
                    file_id,
                    i + 1,
                    provider,
                    label,
                    time.monotonic() - t0,
                )
                raise
            if _aborted():
                raise RuntimeError("Upload cancelled")
            record = {
                "i": i,
                "account_id": account_id,
                "remote_id": remote_id,
                "sha256": hashlib.sha256(ciphertext).hexdigest(),
                "nonce": crypto.b64e(nonce),
            }
            with progress_lock:
                if marked_active:
                    inflight_by_account[account_id] = max(
                        0, inflight_by_account.get(account_id, 0) - 1
                    )
                    marked_active = False
                shard_records[i] = record
                done_chunks = len(shard_records)
                done_bytes += plain_len
                stats = _upload_stats()
                if size is not None and size >= 0:
                    current = min(done_bytes, max(1, size))
                    msg = f"Uploaded chunk {done_chunks}" + (
                        f"/{expected_chunks}" if expected_chunks else ""
                    )
                    _progress(
                        on_progress,
                        "upload_shard",
                        current,
                        total_steps,
                        f"{msg}…",
                        stats=stats,
                    )
                else:
                    _progress(
                        on_progress,
                        "upload_shard",
                        done_chunks,
                        max(done_chunks + 1, total_steps),
                        f"Uploaded chunk {done_chunks}…",
                        stats=stats,
                    )
            _persist_session()
            log.info(
                "shard upload done file=%s i=%s%s remote_id=%s elapsed=%.2fs "
                "done_chunks=%s done_bytes=%s",
                file_id,
                i + 1,
                f"/{expected_chunks}" if expected_chunks else "",
                remote_id,
                time.monotonic() - t0,
                done_chunks,
                done_bytes,
            )
            return record
        finally:
            if marked_active:
                with progress_lock:
                    inflight_by_account[account_id] = max(
                        0, inflight_by_account.get(account_id, 0) - 1
                    )
            account_limits[account_id].release()
            if release_slot and on_chunk_done is not None:
                on_chunk_done()

    chunk_index = start_chunk
    stream_bytes = 0
    pool = ThreadPoolExecutor(max_workers=workers)
    abort_exc: BaseException | None = None
    # Tee plaintext for thumbnail gen (only when streaming from byte 0).
    # Video: prefix only (ffmpeg needs early frames). Office/image/pdf: full file.
    # Unsupported / oversized office: skip spool.
    spool_path: Path | None = None
    spool_fp: Any = None
    # Set once the spool is handed to the background thumbnail thread, which then
    # owns spool cleanup so this function's finally must not unlink it.
    thumb_scheduled = False
    thumb_kind = classify_filename(filename)
    spool_limit: int | None = None
    want_spool = False
    if resume_from_byte == 0 and thumb_kind != "unsupported":
        if thumb_kind == "video":
            want_spool = True
            spool_limit = max(0, THUMB_VIDEO_SPOOL_BYTES)
        elif thumb_kind == "office" and size is not None and size > THUMB_MAX_SOURCE_BYTES:
            want_spool = False
        else:
            want_spool = True
            spool_limit = None
    if want_spool:
        spool_dir = DATA_DIR / "tmp"
        spool_dir.mkdir(parents=True, exist_ok=True)
        spool_path = spool_dir / f"{file_id}.spool"
        spool_fp = open(spool_path, "wb")

        def _tee_chunks() -> Any:
            assert spool_fp is not None
            written = 0
            try:
                for piece in chunks:
                    if not spool_fp.closed:
                        if spool_limit is None:
                            spool_fp.write(piece)
                        elif written < spool_limit:
                            take = min(len(piece), spool_limit - written)
                            if take:
                                spool_fp.write(piece[:take])
                                written += take
                            if written >= spool_limit:
                                spool_fp.flush()
                                spool_fp.close()
                    yield piece
            finally:
                if not spool_fp.closed:
                    spool_fp.close()

        chunk_iter = iter(_tee_chunks())
    else:
        chunk_iter = iter(chunks)
    log.info(
        "upload_file start file=%s path=%s filename=%s size=%s chunk_size=%s "
        "workers=%s expected_chunks=%s resume_from_byte=%s done_shards=%s",
        file_id,
        dir_path,
        filename,
        size,
        shard_size,
        workers,
        expected_chunks,
        resume_from_byte,
        len(shard_records),
    )

    def _reap_completed(*, block: bool = False, timeout: float = 0.05) -> int:
        """Remove finished futures from ``in_flight``.

        Critical: workers unmark ``active`` as soon as they finish, but until we
        reap here ``len(in_flight)`` still counts them. Without continuous
        reaping, the submit loop thinks the pool is full and cannot hand the
        next shard to the first free worker.
        """
        reaped = 0
        while in_flight:
            if _aborted():
                raise RuntimeError("Upload cancelled")
            done, _ = wait(
                in_flight,
                timeout=(timeout if block else 0),
                return_when=FIRST_COMPLETED,
            )
            if not done:
                return reaped
            for fut in done:
                i = in_flight.pop(fut)
                shard_records[i] = fut.result()
                reaped += 1
            # After the first blocking wait, drain anything else already finished.
            block = False
            timeout = 0
        return reaped

    def _wait_one() -> None:
        if not in_flight:
            return
        while not _reap_completed(block=True, timeout=0.5):
            if _aborted():
                raise RuntimeError("Upload cancelled")
            if not in_flight:
                return

    def _claim_account(need: int) -> str:
        """Acquire a per-account permit, then reserve placement on that account.

        Permits are taken *before* submitting to the thread pool so MEGA's
        concurrency=1 cannot park pool workers and starve Drive (or other)
        accounts. If every account is at its cap, wait for an in-flight shard.
        """
        while True:
            if _aborted():
                raise RuntimeError("Upload cancelled")
            _reap_completed()
            available: list[str] = []
            for account in account_list:
                aid = account["id"]
                if account_limits[aid].acquire(blocking=False):
                    available.append(aid)
            if not available:
                if not in_flight:
                    # No work running and no permits — should not happen; block.
                    preferred = account_list[0]["id"]
                    account_limits[preferred].acquire()
                    available = [preferred]
                else:
                    _wait_one()
                    continue
            try:
                chosen = planner.pick(need, only=set(available))
            except ValueError:
                for aid in available:
                    account_limits[aid].release()
                raise
            for aid in available:
                if aid != chosen:
                    account_limits[aid].release()
            return chosen

    def _mark_active(account_id: str, chunk_i: int) -> None:
        label = account_meta.get(account_id, {}).get("label") or account_id
        provider = account_meta.get(account_id, {}).get("provider") or "?"
        with progress_lock:
            inflight_by_account[account_id] = inflight_by_account.get(account_id, 0) + 1
            current = done_bytes
            if size is not None and size >= 0:
                current = min(done_bytes, max(1, size))
            _progress(
                on_progress,
                "upload_shard",
                current,
                total_steps,
                f"Uploading chunk {chunk_i + 1}"
                + (f"/{expected_chunks}" if expected_chunks else "")
                + f" → {provider}:{label}…",
                stats=_upload_stats(),
            )

    # Feeder pulls plaintext in a side thread so the submit loop can keep
    # reaping finished workers (first-finished → next job) instead of blocking
    # inside next(chunk_iter) while in_flight fills with zombie done futures.
    _CHUNK_END = object()
    pending_chunks: queue.Queue[Any] = queue.Queue(maxsize=max(2, workers))
    feeder_error: list[BaseException] = []

    def _chunk_feeder() -> None:
        try:
            for plain in chunk_iter:
                if _aborted():
                    break
                pending_chunks.put(plain)
            pending_chunks.put(_CHUNK_END)
        except BaseException as exc:
            feeder_error.append(exc)
            try:
                pending_chunks.put(_CHUNK_END)
            except Exception:
                pass

    feeder_thread = threading.Thread(
        target=_chunk_feeder,
        name=f"upload-feeder-{file_id}",
        daemon=True,
    )
    feeder_thread.start()

    try:
        try:
            while True:
                if _aborted():
                    raise RuntimeError("Upload cancelled")
                # Drop finished futures so free workers can take new shards.
                _reap_completed()
                while len(in_flight) >= workers:
                    _wait_one()

                try:
                    item = pending_chunks.get(timeout=0.05)
                except queue.Empty:
                    if feeder_error:
                        raise feeder_error[0]
                    # No plaintext yet — keep reaping so active/in_flight stay honest.
                    continue

                if item is _CHUNK_END:
                    if feeder_error:
                        raise feeder_error[0]
                    break

                plain = item
                release_slot = not (chunk_index == 0 and plain == b"" and start_chunk == 0)
                stream_bytes += len(plain)

                # Already on provider from a prior attempt: drop plaintext and continue.
                if chunk_index in shard_records:
                    if release_slot and on_chunk_done is not None:
                        on_chunk_done()
                    log.info(
                        "shard upload skip file=%s i=%s%s (already uploaded)",
                        file_id,
                        chunk_index + 1,
                        f"/{expected_chunks}" if expected_chunks else "",
                    )
                    chunk_index += 1
                    continue

                need = cipher_size(len(plain))
                account_id = _claim_account(need)
                _mark_active(account_id, chunk_index)
                try:
                    fut = pool.submit(
                        _encrypt_upload,
                        chunk_index,
                        plain,
                        account_id,
                        release_slot=release_slot,
                    )
                except Exception:
                    with progress_lock:
                        inflight_by_account[account_id] = max(
                            0, inflight_by_account.get(account_id, 0) - 1
                        )
                    account_limits[account_id].release()
                    if release_slot and on_chunk_done is not None:
                        on_chunk_done()
                    raise
                in_flight[fut] = chunk_index
                chunk_index += 1

            while in_flight:
                _wait_one()
        except BaseException as exc:
            abort_exc = exc
    finally:
        # Unblock feeder if it is sitting on a full pending queue.
        try:
            while True:
                pending_chunks.get_nowait()
        except queue.Empty:
            pass
        feeder_thread.join(timeout=5)
        # Wait for in-flight uploads so folder delete cannot race late writes.
        pool.shutdown(wait=True, cancel_futures=abort_exc is not None or _aborted())
        # Only wipe remotes on explicit user cancel. Provider glitches / interrupts
        # keep per-file folders so progress is not thrown away for a retryable error.
        if _aborted():
            _cleanup_remote_folders()
        elif abort_exc is not None:
            err = str(abort_exc) or type(abort_exc).__name__
            _persist_session(status="failed", error=err)
            log.warning(
                "upload failed; session kept for resume file=%s done_shards=%s error=%s",
                file_id,
                len(shard_records),
                abort_exc,
            )
        # Drop spool on failure/cancel; success path unlinks after thumb gen.
        if abort_exc is not None or _aborted():
            if spool_fp is not None and not spool_fp.closed:
                try:
                    spool_fp.close()
                except Exception:
                    pass
            if spool_path is not None:
                try:
                    spool_path.unlink(missing_ok=True)
                except Exception:
                    pass

    if abort_exc is not None:
        raise abort_exc
    if _aborted():
        raise RuntimeError("Upload cancelled")

    try:
        plaintext_size = resume_from_byte + stream_bytes
        # Full re-stream from 0: stream_bytes is the whole file; resume_from_byte is 0.
        # Offset resume: stream covers the suffix only.
        if size is not None and size >= 0 and plaintext_size != size:
            # Incomplete body cannot be indexed; keep session for resume (not cancel).
            _persist_session(
                status="failed",
                error=(
                    f"Upload truncated or oversized: received {plaintext_size} bytes, "
                    f"expected {size} bytes"
                ),
            )
            raise ValueError(
                f"Upload truncated or oversized: received {plaintext_size} bytes, "
                f"expected {size} bytes"
            )

        if expected_chunks is not None:
            missing = [i for i in range(expected_chunks) if i not in shard_records]
            if missing:
                _persist_session(status="failed", error=f"Missing shards: {missing[:8]}")
                raise RuntimeError(
                    f"Upload incomplete: missing {len(missing)} shards (e.g. {missing[:5]})"
                )
            ordered = [shard_records[i] for i in range(expected_chunks)]
        else:
            ordered = [shard_records[i] for i in range(chunk_index)]

        if size is not None and size >= 0:
            total_steps = max(1, size) + 1
            final_size = size
        else:
            final_size = plaintext_size
            total_steps = final_size + 1 if final_size > 0 else 2

        _progress(on_progress, "index", total_steps - 1, total_steps, "Updating vault index…")
        log.info(
            "upload_file indexing file=%s shards=%s plaintext=%s",
            file_id,
            len(ordered),
            final_size,
        )
        with folders_lock:
            folders_snapshot = dict(remote_folders)
        entry = {
            "id": file_id,
            "path": _join_path(dir_path, filename),
            "size": final_size,
            "mtime": int(time.time()),
            "chunk_size": shard_size,
            "crypto": {
                "alg": crypto.ALG_CHUNK,
                "wrapped_file_key": crypto.wrap_file_key(master_key, file_key),
            },
            "remote_folders": folders_snapshot,
            "remote_vault_dir_id": remote_vault_dir_id,
            "shards": ordered,
        }

        parent = _normalize_dir(dir_path)
        if parent != "/":
            folders_store.ensure_folder(parent)

        index = vault_index.load_index()
        index["files"] = [f for f in index.get("files", []) if f["path"] != entry["path"]]
        entry["thumb_status"] = "none"
        vault_index.upsert_file(index, entry)
        vault_index.save_index(index)
        upload_sessions.delete(file_id)

        # Thumbnail generation (LibreOffice/ffmpeg/Pillow) can be slow, so keep it
        # off the upload critical path: hand the spool to a background thread and let
        # the job complete now. The scheduled task owns spool cleanup.
        entry["thumb_status"] = "none"
        entry["thumb"] = None
        if spool_path is not None:
            schedule_after_upload(
                user_id=user_id,
                master_key=master_key,
                file_id=file_id,
                filename=filename,
                spool_path=spool_path,
                remote_folders=folders_snapshot,
                accounts=account_list,
            )
            thumb_scheduled = True
        else:
            # No plaintext spool (unsupported / resume / oversized office): attach just
            # records the final status and does no heavy work, so run it inline.
            attach_after_upload(
                file_id=file_id,
                filename=filename,
                spool_path=None,
                master_key=master_key,
                remote_folders=folders_snapshot,
                accounts=account_list,
            )
            refreshed = vault_index.find_file(None, file_id) or entry
            entry["thumb_status"] = refreshed.get("thumb_status") or "none"
            entry["thumb"] = refreshed.get("thumb")

        _progress(on_progress, "done", total_steps, total_steps, "Done")
        log.info(
            "upload_file complete file=%s path=%s size=%s shards=%s thumb=%s",
            file_id,
            entry["path"],
            final_size,
            len(ordered),
            entry.get("thumb_status"),
        )
        return entry
    finally:
        if spool_fp is not None and not spool_fp.closed:
            try:
                spool_fp.close()
            except Exception:
                pass
        # When handed to the background thumbnail thread, that thread unlinks the spool.
        if spool_path is not None and not thumb_scheduled:
            try:
                spool_path.unlink(missing_ok=True)
            except Exception:
                log.exception("spool cleanup failed file=%s path=%s", file_id, spool_path)


def upload_file_from_stream(
    master_key: bytes,
    dir_path: str,
    filename: str,
    source: BinaryIO,
    *,
    size: int | None = None,
    chunk_size: int | None = None,
    on_progress: ProgressFn | None = None,
) -> dict[str, Any]:
    shard_size = chunk_size if chunk_size is not None else choose_chunk_size(size)
    return upload_file(
        master_key,
        dir_path,
        filename,
        _iter_plaintext_chunks(source, shard_size),
        size=size,
        chunk_size=shard_size,
        on_progress=on_progress,
    )


def download_file(
    master_key: bytes,
    file_id: str,
    on_progress: ProgressFn | None = None,
) -> tuple[str, str]:
    """Fetch and decrypt to a temp file. Returns (filename, temp_path)."""
    fd, out_path = tempfile.mkstemp(prefix="vault-dl-", suffix=".bin")
    os.close(fd)
    try:
        name = None
        with open(out_path, "wb") as out:
            for name, chunk in _iter_download_named(master_key, file_id, on_progress):
                out.write(chunk)
        assert name is not None
        return name, out_path
    except Exception:
        try:
            os.unlink(out_path)
        except OSError:
            pass
        raise


def iter_download(
    master_key: bytes,
    file_id: str,
    on_progress: ProgressFn | None = None,
    *,
    should_abort: Callable[[], bool] | None = None,
) -> Any:
    """Yield plaintext chunks while fetching/decrypting with bounded buffers."""
    for _name, chunk in _iter_download_named(
        master_key,
        file_id,
        on_progress,
        should_abort=should_abort,
    ):
        yield chunk


def _iter_download_named(
    master_key: bytes,
    file_id: str,
    on_progress: ProgressFn | None = None,
    *,
    should_abort: Callable[[], bool] | None = None,
) -> Any:
    index = vault_index.load_index()
    entry = vault_index.find_file(index, file_id)
    if entry is None:
        raise FileNotFoundError("File not found")

    alg = entry.get("crypto", {}).get("alg")
    if alg != crypto.ALG_CHUNK:
        raise ValueError(f"Unsupported crypto algorithm: {alg!r}")

    account_list = accounts_store.load_accounts()
    account_map = {a["id"]: a for a in account_list}
    providers_by_id = _providers_by_id(account_list)
    shard_list = sorted(entry["shards"], key=lambda s: s["i"])
    total_steps = max(1, len(shard_list))
    name = PurePosixPath(entry["path"]).name
    file_key = crypto.unwrap_file_key(master_key, entry["crypto"]["wrapped_file_key"])
    file_size = entry.get("size")
    chunk_size = int(entry.get("chunk_size") or 0) or None

    yield from _iter_chunked_named(
        name,
        shard_list,
        account_list,
        account_map,
        providers_by_id,
        file_key,
        total_steps,
        on_progress,
        total_bytes=file_size if isinstance(file_size, int) else None,
        chunk_size=chunk_size,
        should_abort=should_abort,
    )


def _iter_chunked_named(
    name: str,
    shard_list: list[dict[str, Any]],
    account_list: list[dict[str, Any]],
    account_map: dict[str, Any],
    providers_by_id: dict[str, Any],
    file_key: bytes,
    total_steps: int,
    on_progress: ProgressFn | None,
    *,
    total_bytes: int | None = None,
    chunk_size: int | None = None,
    should_abort: Callable[[], bool] | None = None,
) -> Any:
    if not shard_list:
        _progress(on_progress, "done", 1, 1, "Done")
        yield name, b""
        return

    def _aborted() -> bool:
        return bool(should_abort and should_abort())

    n = len(shard_list)
    workers = _worker_count(n, download=True)
    max_buffered = max(workers, DOWNLOAD_QUEUE_DEPTH)
    progress_lock = threading.Lock()
    done_count = 0
    done_bytes = 0
    inflight_by_account: dict[str, int] = {}
    done_by_account: dict[str, dict[str, int]] = {}
    # Exact per-account share from the vault index (not an estimate).
    expected_by_account: dict[str, int] = {}
    for shard_rec in shard_list:
        aid = shard_rec["account_id"]
        expected_by_account[aid] = expected_by_account.get(aid, 0) + 1
        done_by_account.setdefault(aid, {"shards": 0, "bytes": 0})

    account_limits: dict[str, threading.Semaphore] = {}
    for account in account_list:
        limit = _provider_account_limit(
            account["provider"], workers=workers, download=True
        )
        account_limits[account["id"]] = threading.Semaphore(limit)
    # Shards may reference accounts removed from the store; still allow one slot.
    for aid in expected_by_account:
        account_limits.setdefault(aid, threading.Semaphore(1))

    def _download_stats() -> dict[str, Any]:
        by_id: dict[str, dict[str, Any]] = {}
        for aid, expected in expected_by_account.items():
            meta = account_map.get(aid) or {}
            done = done_by_account.get(aid) or {"shards": 0, "bytes": 0}
            by_id[aid] = {
                "account_id": aid,
                "provider": meta.get("provider") or "?",
                "label": meta.get("label") or aid,
                "shards": done["shards"],
                "active": inflight_by_account.get(aid, 0),
                "bytes": done["bytes"],
                "estimated_shards": expected,
            }
        return {
            "done_shards": done_count,
            "expected_shards": n,
            "done_bytes": done_bytes,
            "total_bytes": total_bytes,
            "chunk_size": chunk_size or 0,
            "active_shards": sum(inflight_by_account.values()),
            "by_account": sorted(
                by_id.values(),
                key=lambda b: (b["provider"], b["label"]),
            ),
        }

    def _emit(message: str) -> None:
        _progress(
            on_progress,
            "fetch_shard",
            done_count,
            total_steps,
            message,
            stats=_download_stats(),
        )

    _emit("Fetching shards…")

    def _fetch(step: int, shard_rec: dict[str, Any]) -> tuple[int, bytes, str, int]:
        account_id = shard_rec["account_id"]
        try:
            if _aborted():
                raise DownloadCancelled("Download cancelled")
            data = _fetch_shard(shard_rec, account_map, providers_by_id)
            if _aborted():
                raise DownloadCancelled("Download cancelled")
            return step, data, account_id, len(data)
        finally:
            account_limits[account_id].release()

    submitted: set[int] = set()
    next_yield = 0
    in_flight: dict[Future[tuple[int, bytes, str, int]], int] = {}
    ready: dict[int, bytes] = {}
    pool = ThreadPoolExecutor(max_workers=workers)
    cancelled = False

    def _reap_completed(*, block: bool = False, timeout: float = 0.05) -> int:
        nonlocal done_count, done_bytes
        reaped = 0
        while in_flight:
            done, _ = wait(
                in_flight,
                timeout=(timeout if block else 0),
                return_when=FIRST_COMPLETED,
            )
            if not done:
                return reaped
            for fut in done:
                step = in_flight.pop(fut)
                _step, data, account_id, nbytes = fut.result()
                ready[step] = data
                with progress_lock:
                    inflight_by_account[account_id] = max(
                        0, inflight_by_account.get(account_id, 0) - 1
                    )
                    done_count += 1
                    done_bytes += nbytes
                    bucket = done_by_account.setdefault(
                        account_id, {"shards": 0, "bytes": 0}
                    )
                    bucket["shards"] += 1
                    bucket["bytes"] += nbytes
                    _emit(f"Fetched {done_count}/{n} shards…")
                reaped += 1
            block = False
            timeout = 0
        return reaped

    def _try_submit() -> bool:
        """Submit the lowest-index ready shard whose account has capacity."""
        if _aborted() or cancelled:
            return False
        if len(in_flight) >= workers:
            return False
        if (len(in_flight) + len(ready)) >= max_buffered:
            return False
        for i in range(n):
            if i in submitted:
                continue
            account_id = shard_list[i]["account_id"]
            if not account_limits[account_id].acquire(blocking=False):
                continue
            submitted.add(i)
            with progress_lock:
                inflight_by_account[account_id] = (
                    inflight_by_account.get(account_id, 0) + 1
                )
                _emit(f"Fetching shard {i + 1}/{n}…")
            fut = pool.submit(_fetch, i, shard_list[i])
            in_flight[fut] = i
            return True
        return False

    try:
        while next_yield < n:
            if _aborted():
                cancelled = True
                raise DownloadCancelled("Download cancelled")
            _reap_completed()
            while _try_submit():
                pass

            while next_yield in ready:
                if _aborted():
                    cancelled = True
                    raise DownloadCancelled("Download cancelled")
                cipher = ready.pop(next_yield)
                nonce = crypto.b64d(shard_list[next_yield]["nonce"])
                plain = crypto.decrypt_chunk(file_key, cipher, nonce)
                yield name, plain
                next_yield += 1
                # Free buffer slots → immediately hand work to free workers.
                while _try_submit():
                    pass

            if next_yield >= n:
                break
            if in_flight:
                _reap_completed(block=True, timeout=0.5)
                continue
            if next_yield not in ready:
                raise RuntimeError(
                    f"Download stalled: waiting for shard {next_yield} with nothing in flight"
                )
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    if cancelled or _aborted():
        raise DownloadCancelled("Download cancelled")

    _progress(
        on_progress,
        "done",
        total_steps,
        total_steps,
        "Done",
        stats=_download_stats(),
    )


def _fetch_shard(
    shard_rec: dict[str, Any],
    account_map: dict[str, Any],
    providers_by_id: dict[str, Any],
) -> bytes:
    account = account_map.get(shard_rec["account_id"])
    if account is None:
        raise RuntimeError(f"Missing account for shard {shard_rec['i']}")
    data = providers_by_id[account["id"]].download(shard_rec["remote_id"])
    digest = hashlib.sha256(data).hexdigest()
    if digest != shard_rec["sha256"]:
        raise RuntimeError(f"Shard {shard_rec['i']} hash mismatch")
    return data


def _purge_file_remotes(
    entry: dict[str, Any],
    providers_by_id: dict[str, Any],
    *,
    on_progress: ProgressFn | None = None,
    parallel: bool = True,
) -> int:
    """Best-effort remote cleanup for one vault file. Returns progress total_steps."""
    shard_list = entry.get("shards", [])
    remote_folders = entry.get("remote_folders") or {}
    thumb = entry.get("thumb") or {}
    thumb_account_id = thumb.get("account_id")
    thumb_remote_id = thumb.get("remote_id")
    done_count = 0
    progress_lock = threading.Lock()

    if remote_folders:
        # One folder delete per account removes all shards for this file.
        folder_items = [
            (account_id, folder_id)
            for account_id, folder_id in remote_folders.items()
            if account_id in providers_by_id
        ]
        total_steps = max(1, len(folder_items) + 1)

        def _delete_folder(item: tuple[str, str]) -> None:
            nonlocal done_count
            account_id, folder_id = item
            try:
                providers_by_id[account_id].delete(folder_id)
            except Exception:
                pass
            with progress_lock:
                done_count += 1
                _progress(
                    on_progress,
                    "delete_shard",
                    done_count,
                    total_steps,
                    f"Deleted remote folder {done_count}/{len(folder_items)}…",
                )

        if folder_items:
            _progress(on_progress, "delete_shard", 0, total_steps, "Deleting remote folders…")
            if parallel and len(folder_items) > 1:
                with ThreadPoolExecutor(
                    max_workers=_worker_count(len(folder_items))
                ) as pool:
                    list(pool.map(_delete_folder, folder_items))
            else:
                for item in folder_items:
                    _delete_folder(item)
    else:
        # Legacy entries without per-file folders: delete shards one by one.
        total_steps = max(1, len(shard_list) + 1)

        def _delete_one(shard_rec: dict[str, Any]) -> None:
            nonlocal done_count
            provider = providers_by_id.get(shard_rec["account_id"])
            if provider is not None:
                try:
                    provider.delete(shard_rec["remote_id"])
                except Exception:
                    pass
            with progress_lock:
                done_count += 1
                _progress(
                    on_progress,
                    "delete_shard",
                    done_count,
                    total_steps,
                    f"Deleted {done_count}/{len(shard_list)} shards…",
                )

        if shard_list:
            _progress(on_progress, "delete_shard", 0, total_steps, "Deleting shards…")
            if parallel and len(shard_list) > 1:
                with ThreadPoolExecutor(
                    max_workers=_worker_count(len(shard_list))
                ) as pool:
                    list(pool.map(_delete_one, shard_list))
            else:
                for shard_rec in shard_list:
                    _delete_one(shard_rec)

    # Root-level / cross-account thumbs are not removed by per-file folder delete.
    if (
        thumb_remote_id
        and thumb_account_id
        and thumb_account_id not in remote_folders
        and thumb_account_id in providers_by_id
    ):
        try:
            providers_by_id[thumb_account_id].delete(thumb_remote_id)
        except Exception:
            pass

    return total_steps


def delete_file(
    file_id: str,
    on_progress: ProgressFn | None = None,
    *,
    providers_by_id: dict[str, Any] | None = None,
    parallel_remotes: bool = True,
) -> dict[str, Any]:
    raise_if_cancelled("Delete cancelled")
    entry = vault_index.remove_file(None, file_id)
    if entry is None:
        raise FileNotFoundError("File not found")
    file_path = str(entry.get("path") or "")

    if providers_by_id is None:
        shard_list = entry.get("shards", [])
        remote_folders = entry.get("remote_folders") or {}
        thumb = entry.get("thumb") or {}
        needed_ids = set(remote_folders) | {s["account_id"] for s in shard_list}
        if thumb.get("account_id"):
            needed_ids.add(thumb["account_id"])
        account_list = accounts_store.load_accounts()
        providers_by_id = _providers_by_id(
            [a for a in account_list if a["id"] in needed_ids]
        )

    total_steps = _purge_file_remotes(
        entry,
        providers_by_id,
        on_progress=on_progress,
        parallel=parallel_remotes,
    )
    _progress(on_progress, "index", total_steps - 1, total_steps, "Updating vault index…")
    _progress(on_progress, "done", total_steps, total_steps, "Done")
    return {"id": file_id, "path": file_path}
