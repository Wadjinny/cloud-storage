from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field

from deps import require_accounts_manageable
from providers.mega import verify_mega_login
from storage import accounts as accounts_store
from storage import activity as activity_store
from storage import auth_context

router = APIRouter(prefix="/api/auth/mega", tags=["auth"])


class MegaLoginBody(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1)
    mfa: str | None = Field(default=None, description="Optional 2FA authenticator code")


@router.post("")
async def mega_login(request: Request, body: MegaLoginBody) -> dict[str, str]:
    require_accounts_manageable(request)
    user_id = auth_context.require_user_id()
    try:
        await verify_mega_login(body.email, body.password, body.mfa)
    except ValueError as exc:
        activity_store.record(
            user_id,
            "account_connect",
            "error",
            path=body.email,
            detail=str(exc),
            meta={"provider": "mega", "label": body.email},
        )
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except Exception as exc:
        detail = f"MEGA login failed: {exc}"
        activity_store.record(
            user_id,
            "account_connect",
            "error",
            path=body.email,
            detail=detail,
            meta={"provider": "mega", "label": body.email},
        )
        raise HTTPException(status_code=401, detail=detail) from exc

    credentials: dict[str, str] = {"email": body.email, "password": body.password}
    if body.mfa:
        credentials["mfa"] = body.mfa

    account = accounts_store.add_account(
        provider="mega",
        label=body.email,
        credentials=credentials,
    )
    public = accounts_store.public_account(account)
    activity_store.record(
        user_id,
        "account_connect",
        "success",
        path=public.get("label"),
        meta={
            "provider": "mega",
            "account_id": public.get("id"),
            "label": public.get("label"),
        },
    )
    return public
