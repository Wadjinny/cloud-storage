from __future__ import annotations

import secrets
import time
from typing import Any, Literal

from services import crypto
from storage import auth_context
from storage.db import connect

ProviderName = Literal["gdrive", "mega", "memory"]
# "expired": the provider rejected the stored grant; only a reconnect fixes it.
AuthState = Literal["ok", "expired"]


def _row_enabled(row: Any) -> bool:
    try:
        return int(row["enabled"]) != 0
    except (KeyError, IndexError, TypeError, ValueError):
        return True


def _row_value(row: Any, column: str, default: Any = None) -> Any:
    """Read an optional column (pre-migration rows / partial SELECTs lack it)."""
    try:
        value = row[column]
    except (KeyError, IndexError, TypeError):
        return default
    return default if value is None else value


def auth_state(account: dict[str, Any]) -> str:
    return str(account.get("auth_state") or "ok")


def needs_reauth(account: dict[str, Any]) -> bool:
    return auth_state(account) != "ok"


def public_account(account: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": account["id"],
        "provider": account["provider"],
        "label": account["label"],
        "enabled": bool(account.get("enabled", True)),
        "auth_state": auth_state(account),
        "auth_error": account.get("auth_error"),
        "auth_renewed_at": account.get("auth_renewed_at"),
    }


def _decrypt_row(row: Any, master_key: bytes) -> dict[str, Any]:
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "provider": row["provider"],
        "label": row["label"],
        "enabled": _row_enabled(row),
        "auth_state": str(_row_value(row, "auth_state", "ok")),
        "auth_error": _row_value(row, "auth_error"),
        "auth_renewed_at": _row_value(row, "auth_renewed_at"),
        "credentials": crypto.open_json(master_key, row["credentials_enc"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


_ACCOUNT_COLS = (
    "id, user_id, provider, label, credentials_enc, enabled, "
    "auth_state, auth_error, auth_renewed_at, created_at, updated_at"
)


def load_accounts(
    *,
    user_id: str | None = None,
    master_key: bytes | None = None,
) -> list[dict[str, Any]]:
    uid = user_id or auth_context.require_user_id()
    key = master_key or auth_context.require_master_key()
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT {_ACCOUNT_COLS}
            FROM accounts
            WHERE user_id = ?
            ORDER BY provider, label
            """,
            (uid,),
        ).fetchall()
    return [_decrypt_row(row, key) for row in rows]


def load_enabled_accounts(
    *,
    user_id: str | None = None,
    master_key: bytes | None = None,
) -> list[dict[str, Any]]:
    """Accounts that may receive new upload shards.

    Accounts whose credentials the provider rejected are skipped too: placement
    would only fail mid-upload until the user reconnects them.
    """
    return [
        a
        for a in load_accounts(user_id=user_id, master_key=master_key)
        if a.get("enabled", True) and not needs_reauth(a)
    ]


def require_enabled_accounts(
    *,
    user_id: str | None = None,
    master_key: bytes | None = None,
) -> list[dict[str, Any]]:
    """Enabled accounts, or raise ValueError explaining what the user must do first."""
    accounts = load_accounts(user_id=user_id, master_key=master_key)
    enabled = [a for a in accounts if a.get("enabled", True) and not needs_reauth(a)]
    if enabled:
        return enabled
    stale = [a for a in accounts if a.get("enabled", True) and needs_reauth(a)]
    if stale:
        labels = ", ".join(str(a.get("label") or a["id"]) for a in stale)
        raise ValueError(f"Reconnect {labels} before uploading (credentials expired)")
    if accounts:
        raise ValueError("Enable at least one account before uploading")
    raise ValueError("Connect at least one account before uploading")


def list_public(
    *,
    user_id: str | None = None,
    master_key: bytes | None = None,
) -> list[dict[str, Any]]:
    return [public_account(a) for a in load_accounts(user_id=user_id, master_key=master_key)]


def get_account(
    account_id: str,
    *,
    user_id: str | None = None,
    master_key: bytes | None = None,
) -> dict[str, Any] | None:
    uid = user_id or auth_context.require_user_id()
    key = master_key or auth_context.require_master_key()
    with connect() as conn:
        row = conn.execute(
            f"""
            SELECT {_ACCOUNT_COLS}
            FROM accounts
            WHERE id = ? AND user_id = ?
            """,
            (account_id, uid),
        ).fetchone()
    if row is None:
        return None
    return _decrypt_row(row, key)


def find_id_by_label(
    provider: ProviderName,
    label: str,
    *,
    user_id: str | None = None,
) -> str | None:
    """Account id for this provider + label, without decrypting credentials."""
    uid = user_id or auth_context.require_user_id()
    with connect() as conn:
        row = conn.execute(
            "SELECT id FROM accounts WHERE user_id = ? AND provider = ? AND label = ?",
            (uid, provider, label),
        ).fetchone()
    return str(row["id"]) if row is not None else None


def add_account(
    provider: ProviderName,
    label: str,
    credentials: dict[str, Any],
    *,
    user_id: str | None = None,
    master_key: bytes | None = None,
    enabled: bool = True,
) -> dict[str, Any]:
    """Connect an account, or replace the credentials of an existing one.

    Reconnecting the same provider + label keeps the existing account id (and its
    On/Off state) so indexed shards stay attached to it, and clears any expired
    credential flag.
    """
    uid = user_id or auth_context.require_user_id()
    key = master_key or auth_context.require_master_key()
    sealed = crypto.seal_json(key, credentials)
    now = int(time.time())
    with connect() as conn:
        existing = conn.execute(
            "SELECT id, enabled, created_at FROM accounts WHERE user_id = ? AND provider = ? AND label = ?",
            (uid, provider, label),
        ).fetchone()
        if existing is not None:
            account_id = str(existing["id"])
            enabled_flag = _row_enabled(existing)
            created_at = int(existing["created_at"] or now)
            conn.execute(
                """
                UPDATE accounts
                SET credentials_enc = ?,
                    auth_state = 'ok',
                    auth_error = NULL,
                    auth_renewed_at = ?,
                    updated_at = ?
                WHERE id = ? AND user_id = ?
                """,
                (sealed, now, now, account_id, uid),
            )
        else:
            account_id = f"acc_{secrets.token_hex(8)}"
            enabled_flag = enabled
            created_at = now
            conn.execute(
                """
                INSERT INTO accounts (
                  id, user_id, provider, label, credentials_enc, enabled,
                  auth_state, auth_error, auth_renewed_at, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, 'ok', NULL, ?, ?, ?)
                """,
                (account_id, uid, provider, label, sealed, 1 if enabled else 0, now, now, now),
            )
    return {
        "id": account_id,
        "user_id": uid,
        "provider": provider,
        "label": label,
        "enabled": enabled_flag,
        "auth_state": "ok",
        "auth_error": None,
        "auth_renewed_at": now,
        "credentials": credentials,
        "created_at": created_at,
        "updated_at": now,
        "reconnected": existing is not None,
    }


def set_enabled(
    account_id: str,
    enabled: bool,
    *,
    user_id: str | None = None,
) -> dict[str, Any] | None:
    uid = user_id or auth_context.require_user_id()
    now = int(time.time())
    with connect() as conn:
        cur = conn.execute(
            """
            UPDATE accounts
            SET enabled = ?, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (1 if enabled else 0, now, account_id, uid),
        )
        if cur.rowcount <= 0:
            return None
    account = get_account(account_id, user_id=uid)
    return public_account(account) if account is not None else None


def delete_account(
    account_id: str,
    *,
    user_id: str | None = None,
) -> bool:
    uid = user_id or auth_context.require_user_id()
    with connect() as conn:
        cur = conn.execute(
            "DELETE FROM accounts WHERE id = ? AND user_id = ?",
            (account_id, uid),
        )
        return cur.rowcount > 0


def update_credentials(
    account_id: str,
    credentials: dict[str, Any],
    *,
    user_id: str | None = None,
    master_key: bytes | None = None,
) -> bool:
    """Merge credentials into an existing account (e.g. refreshed OAuth tokens)."""
    uid = user_id or auth_context.require_user_id()
    key = master_key or auth_context.require_master_key()
    with connect() as conn:
        row = conn.execute(
            """
            SELECT credentials_enc FROM accounts
            WHERE id = ? AND user_id = ?
            """,
            (account_id, uid),
        ).fetchone()
        if row is None:
            return False
        merged = dict(crypto.open_json(key, row["credentials_enc"]) or {})
        merged.update(credentials)
        conn.execute(
            """
            UPDATE accounts
            SET credentials_enc = ?, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (crypto.seal_json(key, merged), int(time.time()), account_id, uid),
        )
    return True


def _set_auth_state(
    account_id: str,
    state: AuthState,
    error: str | None,
    *,
    user_id: str | None = None,
    touch_renewed: bool = False,
) -> bool:
    uid = user_id or auth_context.require_user_id()
    now = int(time.time())
    with connect() as conn:
        cur = conn.execute(
            """
            UPDATE accounts
            SET auth_state = ?,
                auth_error = ?,
                auth_renewed_at = CASE WHEN ? THEN ? ELSE auth_renewed_at END,
                updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (state, error, 1 if touch_renewed else 0, now, now, account_id, uid),
        )
        return cur.rowcount > 0


def mark_auth_expired(
    account_id: str,
    error: str,
    *,
    user_id: str | None = None,
) -> bool:
    """Flag credentials the provider rejected; the user must reconnect the account."""
    return _set_auth_state(account_id, "expired", error, user_id=user_id)


def mark_auth_ok(account_id: str, *, user_id: str | None = None) -> bool:
    """Clear the reconnect flag after a successful token renewal."""
    return _set_auth_state(account_id, "ok", None, user_id=user_id, touch_renewed=True)


def count_accounts(*, user_id: str | None = None) -> int:
    uid = user_id or auth_context.require_user_id()
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM accounts WHERE user_id = ?",
            (uid,),
        ).fetchone()
    return int(row["n"]) if row else 0
