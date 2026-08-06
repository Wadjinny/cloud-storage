from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from config import STORAGE_FULL_RATIO
from deps import require_unlocked
from providers.factory import get_provider
from services.capacity import headroom_bytes, is_full
from storage import accounts as accounts_store
from storage import auth_context

router = APIRouter(prefix="/api/accounts", tags=["accounts"])


@router.get("")
def list_accounts(request: Request) -> list[dict[str, str]]:
    require_unlocked(request)
    return accounts_store.list_public()


def _quota_for_account(account: dict[str, Any]) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": account["id"],
        "provider": account["provider"],
        "label": account["label"],
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
    used_known = 0
    total_known = 0
    available_known = 0
    placeable_known = 0
    for row in rows:
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
            "complete": total_known == len(rows) and len(rows) > 0,
            "account_count": len(rows),
        },
    }


@router.delete("/{account_id}")
def delete_account(request: Request, account_id: str) -> dict[str, bool]:
    require_unlocked(request)
    account = accounts_store.get_account(account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    if account["provider"] == "mega":
        from providers.mega_session import mega_sessions

        mega_sessions.drop(account["credentials"]["email"])
    if not accounts_store.delete_account(account_id):
        raise HTTPException(status_code=404, detail="Account not found")
    return {"ok": True}
