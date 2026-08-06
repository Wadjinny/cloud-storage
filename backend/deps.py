from __future__ import annotations

from fastapi import HTTPException, Request

from services import crypto
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


def session_user_id(request: Request) -> str | None:
    if not request.session.get("vault_unlocked"):
        return None
    user_id = request.session.get("user_id")
    return str(user_id) if user_id else None
