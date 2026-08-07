from __future__ import annotations

from fastapi import HTTPException, Request

from services import crypto
from services.account_lock import accounts_manage_blocked_reason
from storage import auth_context


def require_unlocked(request: Request) -> bytes:
    if not request.session.get("vault_unlocked"):
        raise HTTPException(status_code=401, detail="Vault is locked")
    user_id = request.session.get("user_id")
    raw = request.session.get("master_key")
    if not user_id or not raw:
        raise HTTPException(status_code=401, detail="Vault is locked")
    try:
        master_key = crypto.b64d(raw)
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Vault is locked") from exc
    auth_context.set_auth(str(user_id), master_key)
    return master_key


def require_accounts_manageable(request: Request) -> bytes:
    """Vault unlocked and no user-level op that locks provider account management."""
    master_key = require_unlocked(request)
    user_id = auth_context.require_user_id()
    reason = accounts_manage_blocked_reason(user_id)
    if reason:
        raise HTTPException(status_code=409, detail=reason)
    return master_key


def session_user_id(request: Request) -> str | None:
    if not request.session.get("vault_unlocked"):
        return None
    user_id = request.session.get("user_id")
    return str(user_id) if user_id else None
