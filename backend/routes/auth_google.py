from __future__ import annotations

import secrets
from urllib.parse import urlparse

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
from deps import require_accounts_manageable
from storage import accounts as accounts_store
from storage import activity as activity_store
from storage import auth_context

router = APIRouter(prefix="/api/auth/google", tags=["auth"])

# localhost and 127.0.0.1 are the same machine but different cookie hosts.
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _loopback_host_mismatch(request: Request) -> str | None:
    """Refuse a flow that cannot come back to the host holding the session.

    Session cookies are scoped per host, so a flow started on 127.0.0.1 can never
    finish on a localhost callback (nor the reverse): the cookie is not sent, and
    the callback only sees a locked vault. Caught here instead of after consent.
    Only fires when a loopback host is involved, so proxied prod hosts are unaffected.
    """
    expected = (urlparse(GOOGLE_REDIRECT_URI).hostname or "").lower()
    actual = (request.url.hostname or "").lower()
    if not expected or not actual or expected == actual:
        return None
    if expected not in _LOOPBACK_HOSTS and actual not in _LOOPBACK_HOSTS:
        return None
    return (
        f"Google returns to '{expected}', but this request came from '{actual}', "
        f"and session cookies are not shared between the two hosts. "
        f"Open the app at {FRONTEND_URL} and retry."
    )


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
def google_start(request: Request, account_id: str | None = None) -> RedirectResponse:
    mismatch = _loopback_host_mismatch(request)
    if mismatch:
        raise HTTPException(status_code=400, detail=mismatch)
    require_accounts_manageable(request)
    if not google_configured():
        raise HTTPException(
            status_code=503,
            detail="Google OAuth is not configured (set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)",
        )
    state = secrets.token_urlsafe(24)
    flow = _flow()
    # Reconnect: preselect the account whose grant expired so the user cannot
    # consent with a different address (which would connect a second account).
    login_hint: str | None = None
    if account_id:
        existing = accounts_store.get_account(account_id)
        if existing is not None and existing["provider"] == "gdrive":
            login_hint = str(existing.get("label") or "") or None
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        state=state,
        **({"login_hint": login_hint} if login_hint else {}),
    )
    # PKCE: authorization_url may auto-generate code_verifier; persist for callback.
    request.session["oauth_state"] = state
    request.session["oauth_code_verifier"] = flow.code_verifier
    return RedirectResponse(auth_url)


@router.get("/callback")
def google_callback(request: Request, code: str | None = None, state: str | None = None) -> RedirectResponse:
    # Explain a host mismatch before the generic "Vault is locked" from the
    # session check, which is what a cross-host callback would otherwise report.
    mismatch = _loopback_host_mismatch(request)
    if mismatch:
        raise HTTPException(status_code=400, detail=mismatch)
    # Session must still be unlocked so we can encrypt stored credentials.
    # Also refuse to finish connecting while the user has a running transfer.
    require_accounts_manageable(request)
    user_id = auth_context.require_user_id()
    if not google_configured():
        raise HTTPException(status_code=503, detail="Google OAuth is not configured")
    if not code or not state or state != request.session.get("oauth_state"):
        activity_store.record(
            user_id,
            "account_connect",
            "error",
            detail="Invalid OAuth state",
            meta={"provider": "gdrive"},
        )
        raise HTTPException(status_code=400, detail="Invalid OAuth state")

    code_verifier = request.session.pop("oauth_code_verifier", None)
    request.session.pop("oauth_state", None)

    flow = _flow()
    if code_verifier:
        flow.code_verifier = code_verifier
    try:
        flow.fetch_token(code=code)
    except Exception as exc:
        detail = f"Google token exchange failed: {exc}"
        activity_store.record(
            user_id,
            "account_connect",
            "error",
            detail=detail,
            meta={"provider": "gdrive"},
        )
        raise HTTPException(status_code=400, detail=detail) from exc

    creds = flow.credentials

    service = build("oauth2", "v2", credentials=creds, cache_discovery=False)
    info = service.userinfo().get().execute()
    email = info.get("email") or "gdrive"

    expiry = creds.expiry.isoformat() if creds.expiry is not None else None
    if not creds.refresh_token:
        # Without a refresh token nothing can be renewed later; make it loud now.
        detail = (
            "Google did not return a refresh token. Revoke the app under "
            "Google Account → Data & privacy → Third-party apps, then reconnect."
        )
        activity_store.record(
            user_id,
            "account_connect",
            "error",
            path=email,
            detail=detail,
            meta={"provider": "gdrive", "label": email},
        )
        raise HTTPException(status_code=400, detail=detail)

    reconnected = accounts_store.find_id_by_label("gdrive", email) is not None
    account = accounts_store.add_account(
        provider="gdrive",
        label=email,
        credentials={
            "token": creds.token,
            "refresh_token": creds.refresh_token,
            "expiry": expiry,
        },
    )
    activity_store.record(
        user_id,
        "account_reconnect" if reconnected else "account_connect",
        "success",
        path=email,
        meta={
            "provider": "gdrive",
            "account_id": account.get("id"),
            "label": email,
        },
    )
    connected = "gdrive-reconnected" if reconnected else "gdrive"
    return RedirectResponse(f"{FRONTEND_URL.rstrip('/')}/?connected={connected}")