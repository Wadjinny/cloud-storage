from __future__ import annotations

import secrets

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

from config import (
    FRONTEND_URL,
    GOOGLE_CLIENT_ID,
    GOOGLE_CLIENT_SECRET,
    GOOGLE_REDIRECT_URI,
    GOOGLE_SCOPES,
    google_configured,
)
from storage import accounts as accounts_store

router = APIRouter(prefix="/api/auth/google", tags=["auth"])


def _flow() -> Flow:
    return Flow.from_client_config(
        {
            "web": {
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [GOOGLE_REDIRECT_URI],
            }
        },
        scopes=GOOGLE_SCOPES,
        redirect_uri=GOOGLE_REDIRECT_URI,
    )


@router.get("/start")
def google_start(request: Request) -> RedirectResponse:
    if not google_configured():
        raise HTTPException(
            status_code=503,
            detail="Google OAuth is not configured (set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)",
        )
    state = secrets.token_urlsafe(24)
    flow = _flow()
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        state=state,
    )
    # PKCE: authorization_url may auto-generate code_verifier; persist for callback.
    request.session["oauth_state"] = state
    request.session["oauth_code_verifier"] = flow.code_verifier
    return RedirectResponse(auth_url)


@router.get("/callback")
def google_callback(request: Request, code: str | None = None, state: str | None = None) -> RedirectResponse:
    if not google_configured():
        raise HTTPException(status_code=503, detail="Google OAuth is not configured")
    if not code or not state or state != request.session.get("oauth_state"):
        raise HTTPException(status_code=400, detail="Invalid OAuth state")

    code_verifier = request.session.pop("oauth_code_verifier", None)
    request.session.pop("oauth_state", None)

    flow = _flow()
    if code_verifier:
        flow.code_verifier = code_verifier
    try:
        flow.fetch_token(code=code)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Google token exchange failed: {exc}") from exc

    creds = flow.credentials

    service = build("oauth2", "v2", credentials=creds, cache_discovery=False)
    info = service.userinfo().get().execute()
    email = info.get("email") or "gdrive"

    expiry = creds.expiry.isoformat() if creds.expiry is not None else None
    accounts_store.add_account(
        provider="gdrive",
        label=email,
        credentials={
            "token": creds.token,
            "refresh_token": creds.refresh_token,
            "expiry": expiry,
        },
    )
    return RedirectResponse(f"{FRONTEND_URL.rstrip('/')}/?connected=gdrive")
