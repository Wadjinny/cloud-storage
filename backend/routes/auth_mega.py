from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, EmailStr, Field

from providers.mega import verify_mega_login
from storage import accounts as accounts_store

router = APIRouter(prefix="/api/auth/mega", tags=["auth"])


class MegaLoginBody(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1)
    mfa: str | None = Field(default=None, description="Optional 2FA authenticator code")


@router.post("")
async def mega_login(body: MegaLoginBody) -> dict[str, str]:
    try:
        await verify_mega_login(body.email, body.password, body.mfa)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=401, detail=f"MEGA login failed: {exc}") from exc

    credentials: dict[str, str] = {"email": body.email, "password": body.password}
    if body.mfa:
        credentials["mfa"] = body.mfa

    account = accounts_store.add_account(
        provider="mega",
        label=body.email,
        credentials=credentials,
    )
    return accounts_store.public_account(account)
