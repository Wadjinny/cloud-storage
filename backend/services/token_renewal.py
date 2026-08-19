"""Automatic OAuth token renewal for connected provider accounts.

Access tokens live about an hour. They are already refreshed on demand, but a
refresh that only happens when a transfer needs it means (a) jobs pay the
refresh mid-upload, and (b) a saved Google grant that nobody uses for weeks can
be expired or revoked server-side without the app noticing until the next
upload fails with ``invalid_grant``.

This sweeps every unlocked user's accounts on a timer: tokens expiring within
``TOKEN_RENEWAL_SKEW_SECONDS`` are refreshed ahead of time, and accounts whose
refresh token Google rejected are flagged ``auth_state = 'expired'`` so the UI
can ask for a one-click reconnect instead of showing a raw OAuth payload. A
dead grant is never retried by the sweep (only re-consent revives it); the
explicit "renew now" call passes ``force=True`` to retry those too.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from config import (
    TOKEN_RENEWAL_ENABLED,
    TOKEN_RENEWAL_INTERVAL_SECONDS,
    TOKEN_RENEWAL_SKEW_SECONDS,
)
from providers import gdrive
from providers.errors import AccountAuthError
from services import session_keys
from storage import accounts as accounts_store
from storage import activity as activity_store
from storage import auth_context

log = logging.getLogger(__name__)

# Providers with a renewable grant (MEGA re-logs in per session instead).
RENEWABLE_PROVIDERS = ("gdrive",)

_task: asyncio.Task[None] | None = None


def _row(account: dict[str, Any], state: str, *, error: str | None = None) -> dict[str, Any]:
    return {
        "account_id": account["id"],
        "label": account.get("label"),
        "provider": account["provider"],
        "state": state,
        "renewed": state == "renewed",
        "error": error,
    }


def _renew_one(
    account: dict[str, Any],
    *,
    user_id: str,
    master_key: bytes,
    force: bool,
    within_seconds: int,
) -> dict[str, Any]:
    was_expired = accounts_store.needs_reauth(account)
    if was_expired and not force:
        # Only a reconnect can fix a rejected grant; do not hammer Google for it.
        return _row(account, "expired", error=account.get("auth_error"))
    credentials = account.get("credentials") or {}
    if not force and not gdrive.credentials_expire_within(credentials, within_seconds):
        return _row(account, "fresh")

    try:
        gdrive.renew_credentials(account, user_id=user_id, master_key=master_key)
    except AccountAuthError as exc:
        if not was_expired:
            log.warning("account needs reconnect: %s", exc)
            activity_store.record(
                user_id,
                "account_token_expired",
                "error",
                path=account.get("label"),
                detail=str(exc),
                meta={
                    "provider": account.get("provider"),
                    "account_id": account["id"],
                    "label": account.get("label"),
                },
            )
        return _row(account, "expired", error=str(exc))
    except Exception as exc:
        # Transient (network / 5xx): leave the account alone and retry next sweep.
        log.warning("token renewal failed account=%s: %s", account["id"], exc)
        return _row(account, "error", error=str(exc))

    log.info("renewed access token account=%s label=%s", account["id"], account.get("label"))
    if was_expired:
        activity_store.record(
            user_id,
            "account_token_renewed",
            "success",
            path=account.get("label"),
            meta={
                "provider": account.get("provider"),
                "account_id": account["id"],
                "label": account.get("label"),
            },
        )
    return _row(account, "renewed")


def renew_user_accounts(
    user_id: str,
    master_key: bytes,
    *,
    force: bool = False,
    within_seconds: int | None = None,
) -> list[dict[str, Any]]:
    """Renew one user's provider tokens; one result row per renewable account."""
    window = TOKEN_RENEWAL_SKEW_SECONDS if within_seconds is None else within_seconds
    rows: list[dict[str, Any]] = []
    with auth_context.auth_scope(user_id, master_key):
        accounts = [
            a
            for a in accounts_store.load_accounts(user_id=user_id, master_key=master_key)
            if a["provider"] in RENEWABLE_PROVIDERS
        ]
        for account in accounts:
            rows.append(
                _renew_one(
                    account,
                    user_id=user_id,
                    master_key=master_key,
                    force=force,
                    within_seconds=window,
                )
            )
    return rows


def sweep_once() -> int:
    """Renew soon-to-expire tokens for every unlocked user. Returns accounts renewed."""
    renewed = 0
    for user_id, master_key in session_keys.active():
        try:
            rows = renew_user_accounts(user_id, master_key)
        except Exception:
            log.exception("token renewal sweep failed user=%s", user_id)
            continue
        renewed += sum(1 for row in rows if row["renewed"])
    return renewed


async def _loop() -> None:
    while True:
        await asyncio.sleep(TOKEN_RENEWAL_INTERVAL_SECONDS)
        try:
            renewed = await asyncio.to_thread(sweep_once)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("token renewal loop error")
            continue
        if renewed:
            log.info("token renewal: refreshed %s account(s)", renewed)


def start() -> None:
    """Start the renewal loop (no-op when disabled or already running).

    The task is tied to the loop that ran the app lifespan; a leftover task from
    an earlier lifespan (one app object, several loops in tests) is dropped.
    """
    global _task
    if not TOKEN_RENEWAL_ENABLED:
        return
    loop = asyncio.get_running_loop()
    if _task is not None:
        if not _task.done() and _task.get_loop() is loop:
            return
        _task.cancel()
    _task = asyncio.create_task(_loop(), name="token-renewal")
    log.info(
        "token renewal every %ss (renew when expiring within %ss)",
        TOKEN_RENEWAL_INTERVAL_SECONDS,
        TOKEN_RENEWAL_SKEW_SECONDS,
    )


async def stop() -> None:
    global _task
    task, _task = _task, None
    if task is None:
        return
    task.cancel()
    if task.get_loop() is not asyncio.get_running_loop():
        # Loop already gone (test client teardown); cancelling is all we can do.
        return
    with contextlib.suppress(asyncio.CancelledError):
        await task
