from __future__ import annotations

import time
from typing import Any

from storage import auth_context
from storage.db import connect


def resume_from_byte(session: dict[str, Any]) -> int:
    """First missing contiguous shard index × chunk_size (gaps are re-uploaded)."""
    chunk_size = int(session["chunk_size"])
    size = session.get("size")
    if not size:
        return 0
    expected = max(1, (int(size) + chunk_size - 1) // chunk_size)
    done = {int(s["i"]) for s in session.get("shards", [])}
    i = 0
    while i < expected and i in done:
        i += 1
    return min(i * chunk_size, int(size))


def _session_from_rows(
    row: Any,
    shard_rows: list[Any],
    folder_rows: list[Any],
) -> dict[str, Any]:
    keys = row.keys() if hasattr(row, "keys") else ()
    remote_vault_dir_id = (
        row["remote_vault_dir_id"] if "remote_vault_dir_id" in keys else None
    )
    return {
        "version": 1,
        "id": row["id"],
        "user_id": row["user_id"],
        "status": row["status"],
        "path": row["path"],
        "filename": row["filename"],
        "size": row["size"],
        "chunk_size": row["chunk_size"],
        "crypto": {
            "alg": row["crypto_alg"],
            "wrapped_file_key": row["wrapped_file_key"],
        },
        "remote_folders": {
            r["account_id"]: r["remote_folder_id"] for r in folder_rows
        },
        "remote_vault_dir_id": remote_vault_dir_id,
        "shards": [
            {
                "i": r["i"],
                "account_id": r["account_id"],
                "remote_id": r["remote_id"],
                "sha256": r["sha256"],
                "nonce": r["nonce"],
            }
            for r in shard_rows
        ],
        "error": row["error"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def save(session: dict[str, Any]) -> None:
    uid = session.get("user_id") or auth_context.require_user_id()
    crypto = session.get("crypto") or {}
    now = int(time.time())
    session["user_id"] = uid
    session["updated_at"] = now
    with connect() as conn:
        existing = conn.execute(
            "SELECT created_at FROM upload_sessions WHERE id = ?",
            (session["id"],),
        ).fetchone()
        created_at = (
            int(existing["created_at"])
            if existing is not None
            else int(session.get("created_at") or now)
        )
        session["created_at"] = created_at
        conn.execute(
            """
            INSERT INTO upload_sessions (
              id, user_id, status, path, filename, size, chunk_size,
              crypto_alg, wrapped_file_key, error, remote_vault_dir_id,
              created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              user_id = excluded.user_id,
              status = excluded.status,
              path = excluded.path,
              filename = excluded.filename,
              size = excluded.size,
              chunk_size = excluded.chunk_size,
              crypto_alg = excluded.crypto_alg,
              wrapped_file_key = excluded.wrapped_file_key,
              error = excluded.error,
              remote_vault_dir_id = excluded.remote_vault_dir_id,
              updated_at = excluded.updated_at
            """,
            (
                session["id"],
                uid,
                session.get("status") or "active",
                session.get("path") or "/",
                session["filename"],
                session.get("size"),
                int(session["chunk_size"]),
                crypto.get("alg") or "AES-256-GCM-CHUNK",
                crypto["wrapped_file_key"],
                session.get("error"),
                session.get("remote_vault_dir_id"),
                created_at,
                now,
            ),
        )
        conn.execute(
            "DELETE FROM upload_session_shards WHERE session_id = ?",
            (session["id"],),
        )
        conn.execute(
            "DELETE FROM upload_session_remote_folders WHERE session_id = ?",
            (session["id"],),
        )
        for shard in session.get("shards") or []:
            conn.execute(
                """
                INSERT INTO upload_session_shards
                  (session_id, i, account_id, remote_id, sha256, nonce)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    session["id"],
                    int(shard["i"]),
                    shard["account_id"],
                    shard["remote_id"],
                    shard["sha256"],
                    shard["nonce"],
                ),
            )
        for account_id, remote_folder_id in (session.get("remote_folders") or {}).items():
            conn.execute(
                """
                INSERT INTO upload_session_remote_folders
                  (session_id, account_id, remote_folder_id)
                VALUES (?, ?, ?)
                """,
                (session["id"], account_id, remote_folder_id),
            )


def load(
    session_id: str,
    *,
    user_id: str | None = None,
) -> dict[str, Any] | None:
    uid = user_id or auth_context.get_user_id()
    with connect() as conn:
        if uid:
            row = conn.execute(
                "SELECT * FROM upload_sessions WHERE id = ? AND user_id = ?",
                (session_id, uid),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM upload_sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        shard_rows = conn.execute(
            """
            SELECT i, account_id, remote_id, sha256, nonce
            FROM upload_session_shards
            WHERE session_id = ?
            ORDER BY i
            """,
            (session_id,),
        ).fetchall()
        folder_rows = conn.execute(
            """
            SELECT account_id, remote_folder_id
            FROM upload_session_remote_folders
            WHERE session_id = ?
            """,
            (session_id,),
        ).fetchall()
    return _session_from_rows(row, shard_rows, folder_rows)


def delete(session_id: str, *, user_id: str | None = None) -> None:
    uid = user_id or auth_context.get_user_id()
    with connect() as conn:
        if uid:
            conn.execute(
                "DELETE FROM upload_sessions WHERE id = ? AND user_id = ?",
                (session_id, uid),
            )
        else:
            conn.execute("DELETE FROM upload_sessions WHERE id = ?", (session_id,))


def list_for_account(
    account_id: str,
    *,
    status: str | None = None,
    user_id: str | None = None,
) -> list[dict[str, Any]]:
    """Sessions that have a remote folder on ``account_id`` (optionally filtered by status)."""
    uid = user_id or auth_context.require_user_id()
    sql = """
        SELECT us.id, us.status, us.filename, usrf.remote_folder_id
        FROM upload_sessions us
        JOIN upload_session_remote_folders usrf ON usrf.session_id = us.id
        WHERE us.user_id = ? AND usrf.account_id = ?
    """
    params: list[Any] = [uid, account_id]
    if status is not None:
        sql += " AND us.status = ?"
        params.append(status)
    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [
        {
            "id": row["id"],
            "status": row["status"],
            "filename": row["filename"],
            "remote_folder_id": row["remote_folder_id"],
        }
        for row in rows
    ]


def mark_failed(session_id: str, error: str) -> dict[str, Any] | None:
    session = load(session_id)
    if session is None:
        return None
    session["status"] = "failed"
    session["error"] = error
    save(session)
    return session


def public_resume_info(session: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_id": session["id"],
        "filename": session["filename"],
        "path": session["path"],
        "size": session.get("size"),
        "chunk_size": session["chunk_size"],
        "resume_from_byte": resume_from_byte(session),
        "done_shards": len(session.get("shards", [])),
    }
