from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from services import crypto
from storage import auth_context
from storage import users as users_store

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginBody(BaseModel):
    username: str = Field(min_length=1)
    passphrase: str = Field(min_length=1)


@router.post("/login")
def login(request: Request, body: LoginBody) -> dict[str, str | bool]:
    try:
        user, master_key = users_store.verify_login(body.username, body.passphrase)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    request.session["vault_unlocked"] = True
    request.session["user_id"] = user["id"]
    request.session["username"] = user["username"]
    request.session["master_key"] = crypto.b64e(master_key)
    auth_context.set_auth(user["id"], master_key)
    return {"ok": True, "username": user["username"]}


@router.post("/logout")
def logout(request: Request) -> dict[str, bool]:
    request.session.pop("vault_unlocked", None)
    request.session.pop("user_id", None)
    request.session.pop("username", None)
    request.session.pop("master_key", None)
    auth_context.clear_auth()
    return {"ok": True}
