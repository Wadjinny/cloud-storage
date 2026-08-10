from __future__ import annotations

from typing import Any

from storage import auth_context
from storage.db import connect


def get(folder_id: str, account_id: str) -> str | None:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT remote_folder_id FROM folder_remote_folders
            WHERE folder_id = ? AND account_id = ?
            """,
            (folder_id, account_id),
        ).fetchone()
    return str(row["remote_folder_id"]) if row is not None else None


def put(folder_id: str, account_id: str, remote_folder_id: str) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO folder_remote_folders (folder_id, account_id, remote_folder_id)
            VALUES (?, ?, ?)
            ON CONFLICT(folder_id, account_id) DO UPDATE SET
              remote_folder_id = excluded.remote_folder_id
            """,
            (folder_id, account_id, remote_folder_id),
        )


def load_for_folder(folder_id: str) -> dict[str, str]:
    """account_id → remote_folder_id for one vault folder."""
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT account_id, remote_folder_id
            FROM folder_remote_folders WHERE folder_id = ?
            """,
            (folder_id,),
        ).fetchall()
    return {str(r["account_id"]): str(r["remote_folder_id"]) for r in rows}


def remote_ids_for_account(account_id: str, *, user_id: str | None = None) -> set[str]:
    """Provider remote folder ids for vault dirs belonging to ``user_id``."""
    uid = user_id or auth_context.require_user_id()
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT fr.remote_folder_id
            FROM folder_remote_folders fr
            JOIN folders f ON f.id = fr.folder_id
            WHERE f.user_id = ? AND fr.account_id = ?
            """,
            (uid, account_id),
        ).fetchall()
    return {str(r["remote_folder_id"]) for r in rows}


def delete_for_folders(folder_ids: list[str] | set[str]) -> None:
    ids = list(folder_ids)
    if not ids:
        return
    with connect() as conn:
        conn.executemany(
            "DELETE FROM folder_remote_folders WHERE folder_id = ?",
            [(fid,) for fid in ids],
        )


def delete_for_account(account_id: str, *, user_id: str | None = None) -> int:
    """Drop vault-dir remote mappings for ``account_id``. Returns rows deleted."""
    uid = user_id or auth_context.require_user_id()
    with connect() as conn:
        cur = conn.execute(
            """
            DELETE FROM folder_remote_folders
            WHERE account_id = ?
              AND folder_id IN (SELECT id FROM folders WHERE user_id = ?)
            """,
            (account_id, uid),
        )
        return int(cur.rowcount or 0)


def list_files_placed_under(
    dir_ids: set[str],
    *,
    user_id: str | None = None,
) -> list[dict[str, Any]]:
    """Files whose remotes were placed under one of ``dir_ids`` (by upload path)."""
    if not dir_ids:
        return []
    uid = user_id or auth_context.require_user_id()
    placeholders = ",".join("?" for _ in dir_ids)
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT id, path, remote_vault_dir_id
            FROM files
            WHERE user_id = ? AND remote_vault_dir_id IN ({placeholders})
            """,
            (uid, *dir_ids),
        ).fetchall()
    return [dict(r) for r in rows]
