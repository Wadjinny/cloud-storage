from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from config import STORAGE_FULL_RATIO
from deps import require_accounts_manageable, require_unlocked
from providers.factory import get_provider
from services import evacuate as evacuate_service
from services import health as health_service
from services import prune as prune_service
from services.capacity import headroom_bytes, is_full
from services.jobs import jobs
from storage import accounts as accounts_store
from storage import activity as activity_store
from storage import auth_context
from storage import upload_sessions
from storage import vault_index

router = APIRouter(prefix="/api/accounts", tags=["accounts"])


class AccountEnabledBody(BaseModel):
    enabled: bool


@router.get("")
def list_accounts(request: Request) -> list[dict[str, Any]]:
    require_unlocked(request)
    return accounts_store.list_public()


def _quota_for_account(account: dict[str, Any]) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": account["id"],
        "provider": account["provider"],
        "label": account["label"],
        "enabled": bool(account.get("enabled", True)),
        "used": None,
        "total": None,
        "available": None,
        "placeable": None,
        "full": False,
        "error": None,
    }
    try:
        quota = get_provider(account).quota()
        used = quota.get("used")
        total = quota.get("total")
        row["used"] = used
        row["total"] = total
        if used is not None and total is not None:
            row["available"] = max(0, total - used)
            row["placeable"] = headroom_bytes(used, total)
            row["full"] = is_full(used, total)
    except Exception as exc:
        row["error"] = str(exc)
    return row


@router.get("/storage")
def list_storage(request: Request) -> dict[str, Any]:
    master_key = require_unlocked(request)
    user_id = auth_context.require_user_id()
    accounts = accounts_store.load_accounts()
    rows: list[dict[str, Any]] = []
    vault_by_account = vault_index.estimated_vault_bytes_by_account(user_id=user_id)
    if accounts:
        # Provider quota calls may refresh Drive tokens on worker threads.
        def _quota(account: dict[str, Any]) -> dict[str, Any]:
            with auth_context.auth_scope(user_id, master_key):
                return _quota_for_account(account)

        with ThreadPoolExecutor(max_workers=min(8, len(accounts))) as pool:
            futures = {pool.submit(_quota, account): account["id"] for account in accounts}
            by_id: dict[str, dict[str, Any]] = {}
            for fut in as_completed(futures):
                by_id[futures[fut]] = fut.result()
        rows = [by_id[a["id"]] for a in accounts]

    used_sum = 0
    total_sum = 0
    available_sum = 0
    placeable_sum = 0
    vault_sum = 0
    used_known = 0
    total_known = 0
    available_known = 0
    placeable_known = 0
    for row in rows:
        vault_used = int(vault_by_account.get(row["id"], 0))
        row["vault_used"] = vault_used
        vault_sum += vault_used
        if row["used"] is not None:
            used_sum += row["used"]
            used_known += 1
        if row["total"] is not None:
            total_sum += row["total"]
            total_known += 1
        if row["available"] is not None:
            available_sum += row["available"]
            available_known += 1
        if row["placeable"] is not None:
            placeable_sum += row["placeable"]
            placeable_known += 1

    return {
        "accounts": rows,
        "full_ratio": STORAGE_FULL_RATIO,
        "combined": {
            "used": used_sum if used_known else None,
            "total": total_sum if total_known else None,
            "available": available_sum if available_known else None,
            "placeable": placeable_sum if placeable_known else None,
            "vault_used": vault_sum,
            "complete": total_known == len(rows) and len(rows) > 0,
            "account_count": len(rows),
        },
    }


@router.post("/health")
def health_check(request: Request) -> dict[str, str]:
    """Verify every indexed shard (and ready thumbnail) still exists on providers."""
    master_key = require_unlocked(request)
    user_id = auth_context.require_user_id()
    job_id = jobs.run_in_thread(
        "health",
        user_id=user_id,
        master_key=master_key,
        work=health_service.check_shards,
        queued_message="Starting health check…",
    )
    return {"job_id": job_id}


@router.post("/prune/analyze")
def prune_analyze(request: Request) -> dict[str, str]:
    """Scan all accounts for orphan remotes; does not delete."""
    master_key = require_unlocked(request)
    user_id = auth_context.require_user_id()
    job_id = jobs.run_in_thread(
        "prune_analyze",
        user_id=user_id,
        master_key=master_key,
        work=prune_service.analyze_all,
        queued_message="Starting prune analysis…",
    )
    return {"job_id": job_id}


@router.post("/prune")
def prune_all(request: Request) -> dict[str, str]:
    """Delete orphan remotes on every connected account."""
    master_key = require_unlocked(request)
    user_id = auth_context.require_user_id()
    job_id = jobs.run_in_thread(
        "prune",
        user_id=user_id,
        master_key=master_key,
        work=prune_service.prune_all,
        queued_message="Starting prune…",
    )
    return {"job_id": job_id}


@router.patch("/{account_id}")
def patch_account(request: Request, account_id: str, body: AccountEnabledBody) -> dict[str, Any]:
    """Enable or disable an account for new upload placement (download/health unchanged)."""
    require_accounts_manageable(request)
    user_id = auth_context.require_user_id()
    action = "account_enable" if body.enabled else "account_disable"
    updated = accounts_store.set_enabled(account_id, body.enabled)
    if updated is None:
        activity_store.record(
            user_id,
            action,
            "error",
            detail="Account not found",
            meta={"account_id": account_id},
        )
        raise HTTPException(status_code=404, detail="Account not found")
    activity_store.record(
        user_id,
        action,
        "success",
        path=updated.get("label"),
        meta={
            "account_id": account_id,
            "provider": updated.get("provider"),
            "label": updated.get("label"),
            "enabled": body.enabled,
        },
    )
    return updated


@router.delete("/{account_id}")
def delete_account(request: Request, account_id: str) -> dict[str, bool]:
    require_accounts_manageable(request)
    user_id = auth_context.require_user_id()
    account = accounts_store.get_account(account_id)
    if account is None:
        activity_store.record(
            user_id,
            "account_disconnect",
            "error",
            detail="Account not found",
            meta={"account_id": account_id},
        )
        raise HTTPException(status_code=404, detail="Account not found")

    usage = vault_index.usage_for_account(account_id)
    if usage["in_use"]:
        parts: list[str] = []
        if usage["shard_count"]:
            parts.append(
                f"{usage['shard_count']} shard(s) in {usage['file_count']} file(s)"
            )
        if usage["thumb_count"]:
            parts.append(f"{usage['thumb_count']} thumbnail(s)")
        sample = usage.get("sample_paths") or []
        message = "Account still holds " + " and ".join(parts) + "."
        if sample:
            message += " Examples: " + ", ".join(sample)
        activity_store.record(
            user_id,
            "account_disconnect",
            "error",
            path=account.get("label"),
            detail=message,
            meta={
                "account_id": account_id,
                "provider": account.get("provider"),
                "label": account.get("label"),
                "code": "account_in_use",
                **{k: usage[k] for k in ("shard_count", "file_count", "thumb_count")},
            },
        )
        raise HTTPException(
            status_code=409,
            detail={
                "code": "account_in_use",
                "message": message,
                "account_id": account_id,
                "label": account.get("label"),
                "provider": account.get("provider"),
                "shard_count": usage["shard_count"],
                "file_count": usage["file_count"],
                "thumb_count": usage["thumb_count"],
                "sample_paths": sample,
            },
        )

    active = upload_sessions.list_for_account(account_id, status="active")
    if active:
        detail = "Account has an active upload. Cancel it before disconnecting."
        activity_store.record(
            user_id,
            "account_disconnect",
            "error",
            path=account.get("label"),
            detail=detail,
            meta={
                "account_id": account_id,
                "provider": account.get("provider"),
                "label": account.get("label"),
            },
        )
        raise HTTPException(status_code=409, detail=detail)

    if account["provider"] == "mega":
        from providers.mega_session import mega_sessions

        mega_sessions.drop(account["credentials"]["email"])
    if not accounts_store.delete_account(account_id):
        activity_store.record(
            user_id,
            "account_disconnect",
            "error",
            detail="Account not found",
            meta={"account_id": account_id},
        )
        raise HTTPException(status_code=404, detail="Account not found")
    activity_store.record(
        user_id,
        "account_disconnect",
        "success",
        path=account.get("label"),
        meta={
            "account_id": account_id,
            "provider": account.get("provider"),
            "label": account.get("label"),
        },
    )
    return {"ok": True}


@router.post("/{account_id}/evacuate")
def evacuate_account(request: Request, account_id: str) -> dict[str, str]:
    """Move shards/thumbs off this account onto others, then disconnect it."""
    master_key = require_accounts_manageable(request)
    user_id = auth_context.require_user_id()
    account = accounts_store.get_account(account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")

    job_id = jobs.run_in_thread(
        "evacuate",
        user_id=user_id,
        master_key=master_key,
        work=lambda on_progress: evacuate_service.evacuate_and_delete(
            account_id, on_progress
        ),
        queued_message="Moving account data…",
    )
    return {"job_id": job_id}
