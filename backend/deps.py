from __future__ import annotations

from fastapi import HTTPException, Request

from services import crypto


def require_unlocked(request: Request) -> bytes:
    if not request.session.get("vault_unlocked"):
        raise HTTPException(status_code=401, detail="Vault is locked")
    raw = request.session.get("master_key")
    if not raw:
        raise HTTPException(status_code=401, detail="Vault is locked")
    try:
        return crypto.b64d(raw)
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Vault is locked") from exc
