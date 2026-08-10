from __future__ import annotations

import hashlib
import logging
import secrets
import threading
from collections.abc import Callable
from concurrent.futures import (
    FIRST_COMPLETED,
    Future,
    ThreadPoolExecutor,
    as_completed,
    wait,
)
from contextvars import copy_context
from pathlib import PurePosixPath
from typing import Any

from config import (
    DELETE_CONCURRENCY,
    DOWNLOAD_QUEUE_DEPTH,
    GDRIVE_ACCOUNT_DOWNLOAD_CONCURRENCY,
    GDRIVE_ACCOUNT_UPLOAD_CONCURRENCY,
    MEGA_ACCOUNT_DOWNLOAD_CONCURRENCY,
    MEGA_ACCOUNT_UPLOAD_CONCURRENCY,
    SHARD_DOWNLOAD_CONCURRENCY,
    SHARD_UPLOAD_CONCURRENCY,
    STORAGE_FULL_RATIO,
)
from providers.factory import get_provider
from services.capacity import PlacementPlanner, estimate_shard_cipher_bytes
from services.jobs import JobCancelled, raise_if_cancelled
from storage import accounts as accounts_store
from storage import auth_context
from storage import folder_remotes
from storage import folders as folders_store
from storage import upload_sessions
from storage import vault_index

log = logging.getLogger(__name__)

ProgressCb = Callable[[str, int, int, str], None]


def _emit(
    cb: ProgressCb | None,
    phase: str,
    current: int,
    total: int,
    message: str,
) -> None:
    if cb is None:
        return
    cb(phase, current, total, message)


def _fmt_bytes(n: int) -> str:
    value = float(max(0, n))
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{n} B"


def _parent_dir(path: str) -> str:
    parent = str(PurePosixPath(path).parent)
    return "/" if parent in ("", ".") else parent


def _account_limit(provider: str, *, workers: int, download: bool) -> int:
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


def _ensure_remote_dir_parent(
    providers_by_id: dict[str, Any],
    account_id: str,
    vault_dir_ids: list[str],
) -> str | None:
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


def _estimate_item_bytes(item: dict[str, Any]) -> int:
    if item["kind"] == "thumb":
        return max(0, int(item.get("bytes") or 0))
    return estimate_shard_cipher_bytes(
        int(item.get("size") or 0),
        int(item.get("chunk_size") or 1),
        int(item.get("i") or 0),
    )


def evacuate_and_delete(
    account_id: str,
    on_progress: ProgressCb | None = None,
) -> dict[str, Any]:
    """Move all shards/thumbs off ``account_id``, then disconnect the account."""
    account = accounts_store.get_account(account_id)
    if account is None:
        raise FileNotFoundError("Account not found")

    active = upload_sessions.list_for_account(account_id, status="active")
    if active:
        raise ValueError("Account has an active upload. Cancel it before disconnecting.")

    dest_accounts = [
        a for a in accounts_store.load_enabled_accounts() if a["id"] != account_id
    ]
    if not dest_accounts:
        raise ValueError(
            "Connect or enable another account before moving data off this one"
        )

    planner = PlacementPlanner.from_accounts(dest_accounts)
    if not planner.placeable_accounts():
        raise ValueError(
            "No other enabled account has free space under the storage-full threshold"
        )

    inventory = vault_index.list_objects_for_account(account_id)
    shards: list[dict[str, Any]] = inventory["shards"]
    thumbs: list[dict[str, Any]] = inventory["thumbs"]
    source_folders: list[dict[str, Any]] = inventory["source_folders"]
    work_items: list[dict[str, Any]] = [*shards, *thumbs]

    if not work_items:
        _emit(on_progress, "delete", 0, 1, "Disconnecting empty account…")
        _delete_account_row(account)
        _emit(on_progress, "done", 1, 1, "Done")
        return {
            "account_id": account_id,
            "moved_shards": 0,
            "moved_thumbs": 0,
            "deleted": True,
            "moved_bytes": 0,
        }

    estimated_bytes = sum(_estimate_item_bytes(item) for item in work_items)
    placeable = planner.placeable_total()
    if placeable is not None and estimated_bytes > placeable:
        ratio_pct = int(round(STORAGE_FULL_RATIO * 100))
        raise ValueError(
            f"Not enough free space under the {ratio_pct}% full threshold "
            f"(need ~{_fmt_bytes(estimated_bytes)}, "
            f"~{_fmt_bytes(placeable)} placeable)"
        )

    planner.configure_balance(len(work_items))
    source_provider = get_provider(account)
    providers_by_id: dict[str, Any] = {account_id: source_provider}
    for dest in dest_accounts:
        providers_by_id[dest["id"]] = get_provider(dest)

    download_workers = max(1, min(SHARD_DOWNLOAD_CONCURRENCY, len(work_items)))
    upload_workers = max(1, min(SHARD_UPLOAD_CONCURRENCY, len(work_items)))
    # Live ciphertext buffers ≈ download + queue + upload workers.
    max_live = max(
        1,
        download_workers + max(0, DOWNLOAD_QUEUE_DEPTH) + upload_workers,
    )
    live_slots = threading.Semaphore(max_live)
    source_slots = threading.Semaphore(
        _account_limit(
            str(account.get("provider") or "memory"),
            workers=download_workers,
            download=True,
        )
    )
    dest_slots: dict[str, threading.Semaphore] = {}
    for dest in dest_accounts:
        dest_slots[dest["id"]] = threading.Semaphore(
            _account_limit(
                str(dest.get("provider") or "memory"),
                workers=upload_workers,
                download=False,
            )
        )

    folder_cache: dict[tuple[str, str], str] = {}
    folder_locks: dict[tuple[str, str], threading.Lock] = {}
    folder_meta_lock = threading.Lock()
    progress_lock = threading.Lock()
    done_count = 0
    done_bytes = 0
    total_bytes = max(1, estimated_bytes)
    files_with_folder = {f["file_id"] for f in source_folders}
    cleanup_estimate = max(
        1,
        len(source_folders)
        + sum(1 for item in work_items if item["file_id"] not in files_with_folder),
    )
    # Move bytes + cleanup steps share one scale for the transfer bar.
    progress_total = max(1, total_bytes + cleanup_estimate)
    moved_shards = 0
    moved_thumbs = 0

    user_id = auth_context.require_user_id()
    master_key = auth_context.require_master_key()

    _emit(
        on_progress,
        "evacuate",
        0,
        progress_total,
        f"Moving {len(work_items)} object(s) "
        f"(~{_fmt_bytes(estimated_bytes)}) off {account.get('label') or account_id}…",
    )
    raise_if_cancelled("Evacuate cancelled")

    def _folder_for(file_id: str, path: str, dest_account_id: str) -> str:
        key = (file_id, dest_account_id)
        with folder_meta_lock:
            cached = folder_cache.get(key)
            if cached is not None:
                return cached
            create_lock = folder_locks.setdefault(key, threading.Lock())
        with create_lock:
            with folder_meta_lock:
                cached = folder_cache.get(key)
                if cached is not None:
                    return cached
            vault_dir_ids = folders_store.folder_id_chain(_parent_dir(path))
            parent_remote = _ensure_remote_dir_parent(
                providers_by_id, dest_account_id, vault_dir_ids
            )
            folder_id = providers_by_id[dest_account_id].ensure_file_folder(
                file_id,
                parent_id=parent_remote,
            )
            vault_index.set_file_remote_folder(file_id, dest_account_id, folder_id)
            with folder_meta_lock:
                folder_cache[key] = folder_id
            return folder_id

    def _download_one(item: dict[str, Any]) -> tuple[dict[str, Any], bytes]:
        # Caller already reserved a live_slots permit for this cipher buffer.
        try:
            raise_if_cancelled("Evacuate cancelled")
            with auth_context.auth_scope(user_id, master_key):
                source_slots.acquire()
                try:
                    raise_if_cancelled("Evacuate cancelled")
                    data = source_provider.download(item["remote_id"])
                finally:
                    source_slots.release()
                if item["kind"] == "shard":
                    digest = hashlib.sha256(data).hexdigest()
                    if digest != item["sha256"]:
                        raise RuntimeError(
                            f"Shard {item['file_id']}[{item['i']}] hash mismatch "
                            "while evacuating"
                        )
                return item, data
        except Exception:
            live_slots.release()
            raise

    def _upload_one(
        item: dict[str, Any], data: bytes
    ) -> tuple[dict[str, Any], str, int]:
        try:
            raise_if_cancelled("Evacuate cancelled")
            with auth_context.auth_scope(user_id, master_key):
                kind = item["kind"]
                file_id = item["file_id"]
                path = item["path"]
                dest_account_id = planner.pick(len(data))
                dest_sem = dest_slots[dest_account_id]
                dest_sem.acquire()
                try:
                    raise_if_cancelled("Evacuate cancelled")
                    parent_id = _folder_for(file_id, path, dest_account_id)
                    # Unique name so source folder cleanup cannot wipe the dest
                    # object when providers share a name-keyed store.
                    suffix = secrets.token_hex(4)
                    if kind == "shard":
                        remote_name = f"shard_{file_id}_{item['i']}_{suffix}"
                    else:
                        remote_name = f"thumb_{file_id}_{suffix}.webp.enc"
                    new_remote = providers_by_id[dest_account_id].upload(
                        remote_name, data, parent_id=parent_id
                    )
                finally:
                    dest_sem.release()
                if kind == "shard":
                    vault_index.update_shard_placement(
                        file_id,
                        int(item["i"]),
                        account_id=dest_account_id,
                        remote_id=new_remote,
                    )
                else:
                    vault_index.update_thumbnail(
                        file_id,
                        status="ready",
                        account_id=dest_account_id,
                        remote_id=new_remote,
                        wrapped_key=item.get("wrapped_key"),
                        mime=item.get("mime"),
                        size_bytes=item.get("bytes"),
                        error=None,
                    )
                return item, kind, len(data)
        finally:
            live_slots.release()

    download_pool = ThreadPoolExecutor(max_workers=download_workers)
    upload_pool = ThreadPoolExecutor(max_workers=upload_workers)
    download_futs: dict[Future[tuple[dict[str, Any], bytes]], dict[str, Any]] = {}
    upload_futs: dict[
        Future[tuple[dict[str, Any], str, int]], dict[str, Any]
    ] = {}
    next_index = 0
    n = len(work_items)

    def _submit_downloads() -> None:
        nonlocal next_index
        while next_index < n and len(download_futs) < download_workers:
            if not live_slots.acquire(blocking=False):
                break
            item = work_items[next_index]
            next_index += 1
            fut = download_pool.submit(copy_context().run, _download_one, item)
            download_futs[fut] = item

    def _on_upload_done(item: dict[str, Any], kind: str, nbytes: int) -> None:
        nonlocal done_count, done_bytes, moved_shards, moved_thumbs, total_bytes
        nonlocal progress_total
        with progress_lock:
            done_count += 1
            done_bytes += nbytes
            if done_bytes > total_bytes:
                # Keep cleanup headroom when the estimate was low.
                progress_total += done_bytes - total_bytes
                total_bytes = done_bytes
            if kind == "shard":
                moved_shards += 1
            else:
                moved_thumbs += 1
            label = PurePosixPath(item["path"]).name
            if item["kind"] == "thumb":
                msg = (
                    f"Moved thumbnail {label} "
                    f"({_fmt_bytes(done_bytes)}/{_fmt_bytes(total_bytes)})…"
                )
            else:
                msg = (
                    f"Moved {label} shard {item['i']} "
                    f"({_fmt_bytes(done_bytes)}/{_fmt_bytes(total_bytes)})…"
                )
            _emit(on_progress, "evacuate", done_bytes, progress_total, msg)

    try:
        _submit_downloads()
        while download_futs or upload_futs or next_index < n:
            raise_if_cancelled("Evacuate cancelled")
            _submit_downloads()
            pending = set(download_futs) | set(upload_futs)
            if not pending:
                _submit_downloads()
                pending = set(download_futs) | set(upload_futs)
                if not pending:
                    break
            done, _ = wait(pending, timeout=0.5, return_when=FIRST_COMPLETED)
            if not done:
                continue
            for fut in done:
                if fut in download_futs:
                    item = download_futs.pop(fut)
                    try:
                        got_item, data = fut.result()
                    except Exception:
                        raise
                    upload_fut = upload_pool.submit(
                        copy_context().run, _upload_one, got_item, data
                    )
                    upload_futs[upload_fut] = got_item
                else:
                    item = upload_futs.pop(fut)
                    got_item, kind, nbytes = fut.result()
                    _on_upload_done(got_item, kind, nbytes)
            _submit_downloads()
    except JobCancelled:
        download_pool.shutdown(wait=False, cancel_futures=True)
        upload_pool.shutdown(wait=False, cancel_futures=True)
        raise
    except Exception:
        download_pool.shutdown(wait=False, cancel_futures=True)
        upload_pool.shutdown(wait=False, cancel_futures=True)
        raise
    else:
        download_pool.shutdown(wait=True, cancel_futures=False)
        upload_pool.shutdown(wait=True, cancel_futures=False)

    raise_if_cancelled("Evacuate cancelled")
    # Prefer one folder delete per file (covers shards/thumbs inside). Only delete
    # loose remotes when there is no per-file folder. Parallelize with progress so
    # large accounts do not look stuck on "Cleaning source remotes…".
    folders_to_delete: list[dict[str, Any]] = []
    for folder in source_folders:
        remote_folder_id = folder["remote_folder_id"]
        if vault_index.remote_folder_used_by_other_account(
            remote_folder_id, account_id
        ):
            log.info(
                "evacuate skip shared source folder account=%s file=%s folder=%s",
                account_id,
                folder["file_id"],
                remote_folder_id,
            )
            try:
                vault_index.clear_file_remote_folder(folder["file_id"], account_id)
            except Exception:
                log.exception(
                    "evacuate clear file_remote_folder failed account=%s file=%s",
                    account_id,
                    folder["file_id"],
                )
            continue
        folders_to_delete.append(folder)

    loose_remotes = [
        item for item in work_items if item["file_id"] not in files_with_folder
    ]
    cleanup_jobs: list[tuple[str, dict[str, Any]]] = [
        *[("folder", f) for f in folders_to_delete],
        *[("remote", item) for item in loose_remotes],
    ]
    cleanup_n = len(cleanup_jobs)
    if cleanup_n > cleanup_estimate:
        progress_total += cleanup_n - cleanup_estimate
    cleanup_done = 0
    cleanup_lock = threading.Lock()

    _emit(
        on_progress,
        "cleanup",
        total_bytes,
        progress_total,
        f"Cleaning source remotes ({len(folders_to_delete)} folder(s), "
        f"{len(loose_remotes)} loose object(s))…",
    )

    def _cleanup_one(job: tuple[str, dict[str, Any]]) -> None:
        kind, payload = job
        raise_if_cancelled("Evacuate cancelled")
        with auth_context.auth_scope(user_id, master_key):
            if kind == "folder":
                try:
                    source_provider.delete(payload["remote_folder_id"])
                except Exception:
                    log.exception(
                        "evacuate source folder delete failed account=%s file=%s folder=%s",
                        account_id,
                        payload["file_id"],
                        payload["remote_folder_id"],
                    )
                try:
                    vault_index.clear_file_remote_folder(payload["file_id"], account_id)
                except Exception:
                    log.exception(
                        "evacuate clear file_remote_folder failed account=%s file=%s",
                        account_id,
                        payload["file_id"],
                    )
            else:
                try:
                    source_provider.delete(payload["remote_id"])
                except Exception:
                    log.exception(
                        "evacuate old remote delete failed account=%s file=%s remote=%s",
                        account_id,
                        payload["file_id"],
                        payload["remote_id"],
                    )

    cleanup_workers = max(
        1,
        min(
            DELETE_CONCURRENCY,
            _account_limit(
                str(account.get("provider") or "memory"),
                workers=DELETE_CONCURRENCY,
                download=True,
            ),
            max(1, len(cleanup_jobs)),
        ),
    )
    if cleanup_jobs:
        pool = ThreadPoolExecutor(max_workers=cleanup_workers)
        try:
            futures = {
                pool.submit(copy_context().run, _cleanup_one, job): job
                for job in cleanup_jobs
            }
            for fut in as_completed(futures):
                raise_if_cancelled("Evacuate cancelled")
                fut.result()
                job = futures[fut]
                with cleanup_lock:
                    cleanup_done += 1
                    label = PurePosixPath(job[1]["path"]).name
                    if job[0] == "folder":
                        msg = (
                            f"Removed folder {label} "
                            f"({cleanup_done}/{cleanup_n})…"
                        )
                    else:
                        msg = (
                            f"Removed remote {label} "
                            f"({cleanup_done}/{cleanup_n})…"
                        )
                    _emit(
                        on_progress,
                        "cleanup",
                        total_bytes + cleanup_done,
                        progress_total,
                        msg,
                    )
        except JobCancelled:
            pool.shutdown(wait=False, cancel_futures=True)
            raise
        except Exception:
            pool.shutdown(wait=False, cancel_futures=True)
            raise
        else:
            pool.shutdown(wait=True, cancel_futures=False)

    try:
        folder_remotes.delete_for_account(account_id)
    except Exception:
        log.exception("evacuate folder_remotes cleanup failed account=%s", account_id)

    usage = vault_index.usage_for_account(account_id)
    if usage["in_use"]:
        raise RuntimeError(
            "Evacuate finished but account still holds indexed data; "
            "retry or delete the remaining files"
        )

    _delete_account_row(account)
    _emit(on_progress, "done", progress_total, progress_total, "Done")
    return {
        "account_id": account_id,
        "moved_shards": moved_shards,
        "moved_thumbs": moved_thumbs,
        "moved_bytes": done_bytes,
        "deleted": True,
        "label": account.get("label"),
        "provider": account.get("provider"),
    }


def _delete_account_row(account: dict[str, Any]) -> None:
    account_id = account["id"]
    if account.get("provider") == "mega":
        from providers.mega_session import mega_sessions

        email = (account.get("credentials") or {}).get("email")
        if email:
            mega_sessions.drop(email)
    if not accounts_store.delete_account(account_id):
        raise FileNotFoundError("Account not found")
