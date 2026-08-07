from __future__ import annotations

import secrets
import time
from typing import Any, Literal

from services import crypto
from storage import auth_context
from storage.db import connect

ProviderName = Literal["gdrive", "mega", "memory"]


def _row_enabled(row: Any) -> bool:
    try:
        return int(row["enabled"]) != 0
    except (KeyError, IndexError, TypeError, ValueError):
        return True


def public_account(account: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": account["id"],
        "provider": account["provider"],
        "label": account["label"],
        "enabled": bool(account.get("enabled", True)),
    }


def _decrypt_row(row: Any, master_key: bytes) -> dict[str, Any]:
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "provider": row["provider"],
        "label": row["label"],
        "enabled": _row_enabled(row),
        "credentials": crypto.open_json(master_key, row["credentials_enc"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


_ACCOUNT_COLS = (
    "id, user_id, provider, label, credentials_enc, enabled, created_at, updated_at"
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
    """Accounts that may receive new upload shards."""
    return [a for a in load_accounts(user_id=user_id, master_key=master_key) if a.get("enabled", True)]


def require_enabled_accounts(
    *,
    user_id: str | None = None,
    master_key: bytes | None = None,
) -> list[dict[str, Any]]:
    """Enabled accounts, or raise ValueError explaining what the user must do first."""
    enabled = load_enabled_accounts(user_id=user_id, master_key=master_key)
    if enabled:
        return enabled
    if count_accounts(user_id=user_id) > 0:
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


def add_account(
    provider: ProviderName,
    label: str,
    credentials: dict[str, Any],
    *,
    user_id: str | None = None,
    master_key: bytes | None = None,
    enabled: bool = True,
) -> dict[str, Any]:
    uid = user_id or auth_context.require_user_id()
    key = master_key or auth_context.require_master_key()
    account_id = f"acc_{secrets.token_hex(8)}"
    sealed = crypto.seal_json(key, credentials)
    now = int(time.time())
    with connect() as conn:
        conn.execute(
            "DELETE FROM accounts WHERE user_id = ? AND provider = ? AND label = ?",
            (uid, provider, label),
        )
        conn.execute(
            """
            INSERT INTO accounts (
              id, user_id, provider, label, credentials_enc, enabled, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (account_id, uid, provider, label, sealed, 1 if enabled else 0, now, now),
        )
    return {
        "id": account_id,
        "user_id": uid,
        "provider": provider,
        "label": label,
        "enabled": enabled,
        "credentials": credentials,
        "created_at": now,
        "updated_at": now,
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


def count_accounts(*, user_id: str | None = None) -> int:
    uid = user_id or auth_context.require_user_id()
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM accounts WHERE user_id = ?",
            (uid,),
        ).fetchone()
    return int(row["n"]) if row else 0
