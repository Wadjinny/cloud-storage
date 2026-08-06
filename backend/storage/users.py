from __future__ import annotations

import logging
import secrets
import time
from typing import Any

import config
from services import crypto
from storage.db import connect, init_db

log = logging.getLogger(__name__)


def create_user(username: str, passphrase: str) -> dict[str, Any]:
    username = username.strip()
    if not username:
        raise ValueError("Username is required")
    if not passphrase:
        raise ValueError("Passphrase is required")
    salt = crypto.new_salt()
    master_key = crypto.derive_master_key(passphrase, salt)
    user = {
        "id": f"usr_{secrets.token_hex(8)}",
        "username": username,
        "kdf_salt": crypto.b64e(salt),
        "verifier": crypto.make_verifier(master_key),
        "created_at": int(time.time()),
    }
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO users (id, username, kdf_salt, verifier, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                user["id"],
                user["username"],
                user["kdf_salt"],
                user["verifier"],
                user["created_at"],
            ),
        )
    return user


def get_by_username(username: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT id, username, kdf_salt, verifier, created_at FROM users WHERE username = ?",
            (username.strip(),),
        ).fetchone()
    return dict(row) if row else None


def get_by_id(user_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT id, username, kdf_salt, verifier, created_at FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
    return dict(row) if row else None


def user_count() -> int:
    with connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()
    return int(row["n"]) if row else 0


def verify_login(username: str, passphrase: str) -> tuple[dict[str, Any], bytes]:
    """Return (user, master_key) or raise ValueError."""
    user = get_by_username(username)
    if user is None:
        raise ValueError("Invalid username or passphrase")
    master_key = crypto.derive_master_key(passphrase, crypto.b64d(user["kdf_salt"]))
    if not crypto.check_verifier(master_key, user["verifier"]):
        raise ValueError("Invalid username or passphrase")
    return user, master_key


def ensure_init_user() -> None:
    """Create the bootstrap user from env if configured and missing."""
    init_db()
    username = (config.INIT_USERNAME or "").strip()
    passphrase = config.INIT_PASSPHRASE or ""
    if not username or not passphrase:
        log.warning(
            "INIT_USERNAME / INIT_PASSPHRASE not set; no bootstrap user will be created"
        )
        return
    if get_by_username(username) is not None:
        return
    create_user(username, passphrase)
    log.info("Created init user %r", username)
