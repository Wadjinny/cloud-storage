from __future__ import annotations

from typing import Any

from storage import auth_context
from storage.db import connect


def _entry_from_rows(
    file_row: Any,
    shard_rows: list[Any],
    folder_rows: list[Any],
) -> dict[str, Any]:
    return {
        "id": file_row["id"],
        "path": file_row["path"],
        "size": file_row["size"],
        "mtime": file_row["mtime"],
        "chunk_size": file_row["chunk_size"],
        "crypto": {
            "alg": file_row["crypto_alg"],
            "wrapped_file_key": file_row["wrapped_file_key"],
        },
        "remote_folders": {
            row["account_id"]: row["remote_folder_id"] for row in folder_rows
        },
        "shards": [
            {
                "i": row["i"],
                "account_id": row["account_id"],
                "remote_id": row["remote_id"],
                "sha256": row["sha256"],
                "nonce": row["nonce"],
            }
            for row in shard_rows
        ],
    }


def _load_entry(conn: Any, file_id: str, user_id: str) -> dict[str, Any] | None:
    file_row = conn.execute(
        """
        SELECT id, path, size, mtime, chunk_size, crypto_alg, wrapped_file_key
        FROM files
        WHERE id = ? AND user_id = ?
        """,
        (file_id, user_id),
    ).fetchone()
    if file_row is None:
        return None
    shard_rows = conn.execute(
        """
        SELECT i, account_id, remote_id, sha256, nonce
        FROM shards
        WHERE file_id = ?
        ORDER BY i
        """,
        (file_id,),
    ).fetchall()
    folder_rows = conn.execute(
        """
        SELECT account_id, remote_folder_id
        FROM file_remote_folders
        WHERE file_id = ?
        """,
        (file_id,),
    ).fetchall()
    return _entry_from_rows(file_row, shard_rows, folder_rows)


def load_index(*, user_id: str | None = None) -> dict[str, Any]:
    """Return a vault-index-shaped dict for the current (or given) user."""
    uid = user_id or auth_context.require_user_id()
    with connect() as conn:
        file_rows = conn.execute(
            """
            SELECT id, path, size, mtime, chunk_size, crypto_alg, wrapped_file_key
            FROM files
            WHERE user_id = ?
            ORDER BY path
            """,
            (uid,),
        ).fetchall()
        files: list[dict[str, Any]] = []
        for file_row in file_rows:
            entry = _load_entry(conn, file_row["id"], uid)
            if entry is not None:
                files.append(entry)
    return {"version": 1, "files": files}


def find_file(
    index: dict[str, Any] | None,
    file_id: str,
    *,
    user_id: str | None = None,
) -> dict[str, Any] | None:
    if index is not None:
        for entry in index.get("files", []):
            if entry["id"] == file_id:
                return entry
    uid = user_id or auth_context.require_user_id()
    with connect() as conn:
        return _load_entry(conn, file_id, uid)


def count_files(*, user_id: str | None = None) -> int:
    uid = user_id or auth_context.require_user_id()
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM files WHERE user_id = ?",
            (uid,),
        ).fetchone()
    return int(row["n"]) if row else 0


def upsert_file(
    index: dict[str, Any] | None,
    entry: dict[str, Any],
    *,
    user_id: str | None = None,
) -> dict[str, Any] | None:
    """Persist a file entry (and optional in-memory index update)."""
    uid = user_id or auth_context.require_user_id()
    crypto = entry.get("crypto") or {}
    with connect() as conn:
        # Replace any other file at the same path for this user.
        conn.execute(
            "DELETE FROM files WHERE user_id = ? AND path = ? AND id != ?",
            (uid, entry["path"], entry["id"]),
        )
        conn.execute(
            """
            INSERT INTO files (
              id, user_id, path, size, mtime, chunk_size, crypto_alg, wrapped_file_key
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              path = excluded.path,
              size = excluded.size,
              mtime = excluded.mtime,
              chunk_size = excluded.chunk_size,
              crypto_alg = excluded.crypto_alg,
              wrapped_file_key = excluded.wrapped_file_key
            """,
            (
                entry["id"],
                uid,
                entry["path"],
                int(entry["size"]),
                int(entry["mtime"]),
                int(entry["chunk_size"]),
                crypto.get("alg") or "AES-256-GCM-CHUNK",
                crypto["wrapped_file_key"],
            ),
        )
        conn.execute("DELETE FROM shards WHERE file_id = ?", (entry["id"],))
        conn.execute(
            "DELETE FROM file_remote_folders WHERE file_id = ?",
            (entry["id"],),
        )
        for shard in entry.get("shards") or []:
            conn.execute(
                """
                INSERT INTO shards (file_id, i, account_id, remote_id, sha256, nonce)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    entry["id"],
                    int(shard["i"]),
                    shard["account_id"],
                    shard["remote_id"],
                    shard["sha256"],
                    shard["nonce"],
                ),
            )
        for account_id, remote_folder_id in (entry.get("remote_folders") or {}).items():
            conn.execute(
                """
                INSERT INTO file_remote_folders (file_id, account_id, remote_folder_id)
                VALUES (?, ?, ?)
                """,
                (entry["id"], account_id, remote_folder_id),
            )

    if index is not None:
        index["files"] = [
            f
            for f in index.get("files", [])
            if f["id"] != entry["id"] and f.get("path") != entry["path"]
        ]
        index["files"].append(entry)
    return index


def remove_file(
    index: dict[str, Any] | None,
    file_id: str,
    *,
    user_id: str | None = None,
) -> dict[str, Any] | None:
    uid = user_id or auth_context.require_user_id()
    with connect() as conn:
        entry = _load_entry(conn, file_id, uid)
        if entry is None:
            return None
        conn.execute("DELETE FROM files WHERE id = ? AND user_id = ?", (file_id, uid))

    if index is not None:
        files = index.get("files", [])
        index["files"] = [f for f in files if f["id"] != file_id]
    return entry


def save_index(index: dict[str, Any]) -> None:
    """No-op write-through compatibility: mutations already persist via upsert/remove."""
    _ = index
