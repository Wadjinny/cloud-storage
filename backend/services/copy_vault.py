"""Copy vault files/folders by cloning ciphertext (no re-encryption)."""

from __future__ import annotations

import logging
import secrets
import time
from collections import defaultdict
from collections.abc import Callable
from pathlib import PurePosixPath
from typing import Any

from config import STORAGE_FULL_RATIO
from providers.factory import get_provider
from services.capacity import (
    PlacementPlanner,
    can_fit,
    cipher_size,
    estimate_shard_cipher_bytes,
)
from services.jobs import raise_if_cancelled
from services.remote_dirs import ensure_remote_dir_parent
from storage import accounts as accounts_store
from storage import folders as folders_store
from storage import vault_index

log = logging.getLogger(__name__)

ProgressFn = Callable[..., None]


def _progress(
    cb: ProgressFn | None,
    phase: str,
    current: int,
    total: int,
    message: str,
) -> None:
    if not cb:
        return
    try:
        cb(phase, current, total, message)
    except TypeError:
        cb(phase, current, total, message)


def _dir_exists(folder_path: str) -> bool:
    from services import vault as vault_service

    return vault_service._dir_exists(folder_path)


def _dest_occupied(path: str) -> str | None:
    from services import vault as vault_service

    return vault_service._dest_occupied(path)


def copy_entry(
    *,
    src_path: str,
    dest_path: str,
    kind: str | None = None,
    on_progress: ProgressFn | None = None,
) -> dict[str, Any]:
    """Copy a file or folder to ``dest_path`` (full destination path)."""
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
        result = _copy_file(file_entry, dest_path, on_progress=on_progress)
        return result

    return _copy_folder(folder_path, dest_path, on_progress=on_progress)


def _check_copy_headroom(entry: dict[str, Any]) -> None:
    """Fail early if any account cannot hold another copy of its shards (+ thumb)."""
    shards = entry.get("shards") or []
    if not shards:
        return
    size = int(entry.get("size") or 0)
    chunk = int(entry.get("chunk_size") or 1)
    need_by_account: dict[str, int] = defaultdict(int)
    for shard in shards:
        aid = shard["account_id"]
        need_by_account[aid] += estimate_shard_cipher_bytes(size, chunk, int(shard["i"]))
    thumb = entry.get("thumb") or {}
    if (entry.get("thumb_status") or thumb.get("status")) == "ready":
        t_account = thumb.get("account_id")
        t_bytes = int(thumb.get("bytes") or 0)
        if t_account and t_bytes > 0:
            need_by_account[t_account] += t_bytes + cipher_size(0)

    account_list = accounts_store.load_accounts()
    by_id = {a["id"]: a for a in account_list}
    planner = PlacementPlanner.from_accounts(
        [by_id[aid] for aid in need_by_account if aid in by_id]
        or account_list
    )
    quota_by_id = {q.account_id: q for q in planner.quotas}
    ratio_pct = int(round(STORAGE_FULL_RATIO * 100))
    for account_id, need in need_by_account.items():
        q = quota_by_id.get(account_id)
        if q is None:
            account = by_id.get(account_id)
            if account is None:
                raise ValueError(f"Account {account_id} not found for copy")
            provider = get_provider(account)
            try:
                quota = provider.quota()
            except Exception:
                continue
            used = quota.get("used")
            total = quota.get("total")
            if not can_fit(used, total, need):
                raise ValueError(
                    f"Not enough free space on {account.get('label') or account_id} "
                    f"under the {ratio_pct}% full threshold "
                    f"(need ~{need} bytes for copy)"
                )
            continue
        if not q.can_fit(need):
            account = by_id.get(account_id) or {}
            raise ValueError(
                f"Not enough free space on {account.get('label') or account_id} "
                f"under the {ratio_pct}% full threshold "
                f"(need ~{need} bytes for copy)"
            )


def _copy_file(
    entry: dict[str, Any],
    dest_path: str,
    *,
    on_progress: ProgressFn | None = None,
    progress_base: int = 0,
    progress_total: int | None = None,
) -> dict[str, Any]:
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

    raise_if_cancelled("Copy cancelled")
    _check_copy_headroom(entry)

    shards = list(entry.get("shards") or [])
    thumb = entry.get("thumb") or {}
    thumb_ready = (entry.get("thumb_status") or thumb.get("status")) == "ready"
    steps = max(1, len(shards) + (1 if thumb_ready else 0))
    total = progress_total if progress_total is not None else steps
    done = progress_base

    new_id = f"f_{secrets.token_hex(8)}"
    vault_dir_ids = folders_store.folder_id_chain(parent)
    remote_vault_dir_id = vault_dir_ids[-1] if vault_dir_ids else None

    accounts_needed = {s["account_id"] for s in shards}
    if thumb_ready and thumb.get("account_id"):
        accounts_needed.add(thumb["account_id"])

    providers: dict[str, Any] = {}
    new_remote_folders: dict[str, str] = {}
    for account_id in accounts_needed:
        account = accounts_store.get_account(account_id)
        if account is None:
            raise ValueError(f"Account {account_id} not found for copy")
        provider = get_provider(account)
        providers[account_id] = provider
        parent_remote = ensure_remote_dir_parent(parent, account_id, provider)
        folder_id = provider.ensure_file_folder(new_id, parent_id=parent_remote)
        new_remote_folders[account_id] = folder_id

    new_shards: list[dict[str, Any]] = []
    created_remotes: list[tuple[Any, str]] = []
    try:
        for shard in shards:
            raise_if_cancelled("Copy cancelled")
            account_id = shard["account_id"]
            provider = providers[account_id]
            parent_id = new_remote_folders[account_id]
            remote_name = f"shard_{new_id}_{int(shard['i'])}"
            new_remote = provider.copy_item(
                shard["remote_id"],
                parent_id=parent_id,
                name=remote_name,
            )
            created_remotes.append((provider, new_remote))
            new_shards.append(
                {
                    "i": int(shard["i"]),
                    "account_id": account_id,
                    "remote_id": new_remote,
                    "sha256": shard["sha256"],
                    "nonce": shard["nonce"],
                }
            )
            done += 1
            _progress(
                on_progress,
                "copy_shard",
                done,
                total,
                f"Copied shard {len(new_shards)}/{len(shards)}",
            )

        thumb_fields: dict[str, Any] = {
            "thumb_status": entry.get("thumb_status") or "none",
        }
        if thumb_ready and thumb.get("remote_id") and thumb.get("account_id"):
            raise_if_cancelled("Copy cancelled")
            t_account = thumb["account_id"]
            provider = providers[t_account]
            parent_id = new_remote_folders[t_account]
            remote_name = f"thumb_{new_id}.webp.enc"
            new_thumb_remote = provider.copy_item(
                thumb["remote_id"],
                parent_id=parent_id,
                name=remote_name,
            )
            created_remotes.append((provider, new_thumb_remote))
            thumb_fields = {
                "thumb_status": "ready",
                "thumb": {
                    "status": "ready",
                    "account_id": t_account,
                    "remote_id": new_thumb_remote,
                    "wrapped_key": thumb.get("wrapped_key"),
                    "mime": thumb.get("mime"),
                    "bytes": thumb.get("bytes"),
                    "error": None,
                },
            }
            done += 1
            _progress(on_progress, "copy_thumb", done, total, "Copied thumbnail")
        elif (entry.get("thumb_status") or "") in ("failed", "unsupported"):
            thumb_fields = {
                "thumb_status": entry.get("thumb_status"),
                "thumb": {
                    "status": entry.get("thumb_status"),
                    "error": thumb.get("error"),
                },
            }

        now = int(time.time())
        new_entry = {
            "id": new_id,
            "path": dest,
            "size": entry.get("size") or 0,
            "mtime": now,
            "chunk_size": entry.get("chunk_size"),
            "crypto": dict(entry.get("crypto") or {}),
            "shards": new_shards,
            "remote_folders": new_remote_folders,
            "remote_vault_dir_id": remote_vault_dir_id,
            "created_at": now,
            **thumb_fields,
        }
        vault_index.upsert_file(None, new_entry)
    except Exception:
        for provider, remote_id in created_remotes:
            try:
                provider.delete(remote_id)
            except Exception:
                log.exception("copy cleanup failed remote=%s", remote_id)
        for account_id, folder_id in new_remote_folders.items():
            try:
                providers[account_id].delete(folder_id)
            except Exception:
                log.exception(
                    "copy cleanup folder failed account=%s folder=%s",
                    account_id,
                    folder_id,
                )
        raise

    _progress(on_progress, "copy_done", done, total, f"Copied {PurePosixPath(dest).name}")
    return {
        "id": new_id,
        "name": PurePosixPath(dest).name,
        "path": dest,
        "kind": "file",
        "size": new_entry.get("size"),
        "mtime": new_entry.get("mtime"),
        "shard_count": len(new_shards),
    }


def _copy_folder(
    src_path: str,
    dest_path: str,
    *,
    on_progress: ProgressFn | None = None,
) -> dict[str, Any]:
    src = folders_store.normalize_dir(src_path)
    dest = folders_store.normalize_dir(dest_path)
    if src == "/":
        raise ValueError("Cannot copy root")
    if dest == "/":
        raise ValueError("Invalid destination")
    if not _dir_exists(src):
        raise FileNotFoundError("Folder not found")
    if dest == src or dest.startswith(src + "/"):
        raise ValueError("Cannot copy a folder into itself")
    folders_store.validate_name(PurePosixPath(dest).name)
    occupied = _dest_occupied(dest)
    if occupied:
        raise ValueError(f"A {occupied} already exists at the destination")

    parent = folders_store.normalize_dir(str(PurePosixPath(dest).parent))
    if parent != "/":
        folders_store.ensure_folder(parent)

    files = vault_index.list_files_under_prefix(src)
    # Also mirror empty subfolders.
    subfolders = [
        f
        for f in folders_store.list_folders_under(src)
        if f["path"] == src or f["path"].startswith(src + "/")
    ]

    raise_if_cancelled("Copy cancelled")
    folders_store.ensure_folder(dest)
    for folder in subfolders:
        rel = folder["path"][len(src) :].lstrip("/")
        if not rel:
            continue
        folders_store.ensure_folder(f"{dest}/{rel}" if dest != "/" else f"/{rel}")

    # Progress: one step per file shard (+thumb) estimate; use file count if empty.
    total_steps = 0
    for entry in files:
        shards = entry.get("shards") or []
        thumb = entry.get("thumb") or {}
        total_steps += max(
            1,
            len(shards)
            + (
                1
                if (entry.get("thumb_status") or thumb.get("status")) == "ready"
                else 0
            ),
        )
    if total_steps == 0:
        total_steps = 1

    done = 0
    _progress(on_progress, "copy_folder", done, total_steps, f"Copying {src} → {dest}")
    for entry in files:
        raise_if_cancelled("Copy cancelled")
        rel = entry["path"][len(src) :].lstrip("/")
        file_dest = f"{dest}/{rel}" if dest != "/" else f"/{rel}"
        shard_n = len(entry.get("shards") or [])
        thumb = entry.get("thumb") or {}
        file_steps = max(
            1,
            shard_n
            + (
                1
                if (entry.get("thumb_status") or thumb.get("status")) == "ready"
                else 0
            ),
        )
        _copy_file(
            entry,
            file_dest,
            on_progress=on_progress,
            progress_base=done,
            progress_total=total_steps,
        )
        done += file_steps

    _progress(on_progress, "copy_done", total_steps, total_steps, f"Copied folder {dest}")
    return {
        "id": f"dir:{dest}",
        "name": PurePosixPath(dest).name,
        "path": dest,
        "kind": "dir",
        "size": None,
        "mtime": None,
        "shard_count": 0,
        "copied_files": len(files),
    }
