from __future__ import annotations

import secrets
from typing import Any, Literal

from services import crypto
from storage import auth_context
from storage.db import connect

ProviderName = Literal["gdrive", "mega", "memory"]


def public_account(account: dict[str, Any]) -> dict[str, str]:
    return {
        "id": account["id"],
        "provider": account["provider"],
        "label": account["label"],
    }


def _decrypt_row(row: Any, master_key: bytes) -> dict[str, Any]:
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "provider": row["provider"],
        "label": row["label"],
        "credentials": crypto.open_json(master_key, row["credentials_enc"]),
    }


def load_accounts(
    *,
    user_id: str | None = None,
    master_key: bytes | None = None,
) -> list[dict[str, Any]]:
    uid = user_id or auth_context.require_user_id()
    key = master_key or auth_context.require_master_key()
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, user_id, provider, label, credentials_enc
            FROM accounts
            WHERE user_id = ?
            ORDER BY provider, label
            """,
            (uid,),
        ).fetchall()
    return [_decrypt_row(row, key) for row in rows]


def list_public(
    *,
    user_id: str | None = None,
    master_key: bytes | None = None,
) -> list[dict[str, str]]:
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
            """
            SELECT id, user_id, provider, label, credentials_enc
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
) -> dict[str, Any]:
    uid = user_id or auth_context.require_user_id()
    key = master_key or auth_context.require_master_key()
    account_id = f"acc_{secrets.token_hex(8)}"
    sealed = crypto.seal_json(key, credentials)
    with connect() as conn:
        conn.execute(
            "DELETE FROM accounts WHERE user_id = ? AND provider = ? AND label = ?",
            (uid, provider, label),
        )
        conn.execute(
            """
            INSERT INTO accounts (id, user_id, provider, label, credentials_enc)
            VALUES (?, ?, ?, ?, ?)
            """,
            (account_id, uid, provider, label, sealed),
        )
    return {
        "id": account_id,
        "user_id": uid,
        "provider": provider,
        "label": label,
        "credentials": credentials,
    }


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
            "UPDATE accounts SET credentials_enc = ? WHERE id = ? AND user_id = ?",
            (crypto.seal_json(key, merged), account_id, uid),
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
