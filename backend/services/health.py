from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextvars import copy_context
from typing import Any

from config import (
    GDRIVE_ACCOUNT_DOWNLOAD_CONCURRENCY,
    HEALTH_CHECK_CONCURRENCY,
)
from providers.factory import get_provider
from services.jobs import JobCancelled, raise_if_cancelled
from storage import accounts as accounts_store
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


def _account_limit(provider: str, *, workers: int) -> int:
    if provider == "mega":
        # exists() is a local map lookup after warm_exists_cache().
        configured = HEALTH_CHECK_CONCURRENCY
    elif provider == "gdrive":
        configured = max(GDRIVE_ACCOUNT_DOWNLOAD_CONCURRENCY, HEALTH_CHECK_CONCURRENCY)
    else:
        configured = workers
    return max(1, min(workers, max(1, configured)))


def _progress_message(item: dict[str, Any]) -> str:
    label = item["path"]
    if item["kind"] == "thumb":
        return f"Checked thumbnail {label}…"
    return f"Checked {label} shard {item['i']}…"


def check_shards(on_progress: ProgressCb | None = None) -> dict[str, Any]:
    """Verify every indexed shard (and ready thumbnail) still exists on its provider."""
    accounts = {a["id"]: a for a in accounts_store.load_accounts()}
    checklist = vault_index.list_health_objects()
    items: list[dict[str, Any]] = checklist["items"]
    file_count = int(checklist["file_count"])
    total = max(len(items), 1)
    workers = max(1, min(HEALTH_CHECK_CONCURRENCY, max(1, len(items))))

    providers: dict[str, Any] = {}
    account_errors: dict[str, str] = {}
    account_limits: dict[str, threading.Semaphore] = {}
    providers_lock = threading.Lock()
    progress_lock = threading.Lock()
    missing: list[dict[str, Any]] = []
    checked = 0
    ok = 0
    done = 0

    _emit(on_progress, "scan", 0, total, f"Checking {len(items)} remote object(s)…")
    raise_if_cancelled("Health check cancelled")

    def _provider_for(account_id: str) -> Any | None:
        with providers_lock:
            if account_id in account_errors:
                return None
            if account_id in providers:
                return providers[account_id]
            account = accounts.get(account_id)
            if account is None:
                return None
            try:
                provider = get_provider(account)
                warm = getattr(provider, "warm_exists_cache", None)
                if callable(warm):
                    warm()
                providers[account_id] = provider
                account_limits[account_id] = threading.Semaphore(
                    _account_limit(account.get("provider") or "", workers=workers)
                )
                return provider
            except Exception as exc:
                log.exception("health provider init failed account=%s", account_id)
                account_errors[account_id] = str(exc)
                return None

    def _check_one(item: dict[str, Any]) -> dict[str, Any] | None:
        """Return an issue row, or None when the remote object is present."""
        raise_if_cancelled("Health check cancelled")
        account_id = item["account_id"]
        account = accounts.get(account_id)
        if account is None:
            return {
                **item,
                "label": account_id,
                "provider": "",
                "reason": "no_account",
                "error": "Account disconnected",
            }

        with providers_lock:
            err = account_errors.get(account_id)
        if err is not None:
            return {
                **item,
                "label": account.get("label") or account_id,
                "provider": account.get("provider") or "",
                "reason": "account_error",
                "error": err,
            }

        provider = _provider_for(account_id)
        if provider is None:
            with providers_lock:
                init_err = account_errors.get(account_id)
            if init_err is not None:
                return {
                    **item,
                    "label": account.get("label") or account_id,
                    "provider": account.get("provider") or "",
                    "reason": "account_error",
                    "error": init_err,
                }
            return {
                **item,
                "label": account.get("label") or account_id,
                "provider": account.get("provider") or "",
                "reason": "no_account",
                "error": "Account disconnected",
            }

        limit = account_limits[account_id]
        limit.acquire()
        try:
            present = bool(provider.exists(item["remote_id"]))
        except Exception as exc:
            log.warning(
                "health exists failed account=%s remote=%s: %s",
                account_id,
                item["remote_id"],
                exc,
            )
            return {
                **item,
                "label": account.get("label") or account_id,
                "provider": account.get("provider") or "",
                "reason": "check_error",
                "error": str(exc),
            }
        finally:
            limit.release()

        if present:
            return None
        return {
            **item,
            "label": account.get("label") or account_id,
            "provider": account.get("provider") or "",
            "reason": "missing",
            "error": None,
        }

    # Warm providers up front so MEGA filesystem fetches are not stampeded.
    for account_id in {item["account_id"] for item in items}:
        raise_if_cancelled("Health check cancelled")
        if account_id in accounts:
            _provider_for(account_id)

    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = {
            pool.submit(copy_context().run, _check_one, item): item for item in items
        }
        for fut in as_completed(futures):
            raise_if_cancelled("Health check cancelled")
            item = futures[fut]
            try:
                issue = fut.result()
            except JobCancelled:
                raise
            except Exception as exc:
                log.exception("health worker failed")
                account = accounts.get(item["account_id"]) or {}
                issue = {
                    **item,
                    "label": account.get("label") or item["account_id"],
                    "provider": account.get("provider") or "",
                    "reason": "check_error",
                    "error": str(exc),
                }

            with progress_lock:
                done += 1
                if issue is None:
                    checked += 1
                    ok += 1
                else:
                    missing.append(issue)
                    if issue["reason"] == "missing":
                        checked += 1
                _emit(on_progress, "scan", done, total, _progress_message(item))
    except JobCancelled:
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    else:
        pool.shutdown(wait=True, cancel_futures=False)

    _emit(
        on_progress,
        "done",
        total,
        total,
        "Health check complete" if not missing else f"{len(missing)} issue(s) found",
    )

    by_file: dict[str, dict[str, Any]] = {}
    for row in missing:
        fid = row["file_id"]
        bucket = by_file.setdefault(
            fid,
            {
                "file_id": fid,
                "path": row["path"],
                "issues": [],
            },
        )
        bucket["issues"].append(row)

    return {
        "file_count": file_count,
        "object_count": len(items),
        "checked": checked,
        "ok": ok,
        "issue_count": len(missing),
        "healthy": len(missing) == 0,
        "missing": missing,
        "files": list(by_file.values()),
        "account_errors": [
            {"account_id": aid, "error": err} for aid, err in account_errors.items()
        ],
    }
