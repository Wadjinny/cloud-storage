from __future__ import annotations

import time
from typing import Any

from storage import auth_context
from storage.db import connect, escape_like


def _thumb_from_row(file_row: Any) -> dict[str, Any]:
    keys = file_row.keys() if hasattr(file_row, "keys") else ()
    status = file_row["thumb_status"] if "thumb_status" in keys else "none"
    return {
        "status": status or "none",
        "account_id": file_row["thumb_account_id"] if "thumb_account_id" in keys else None,
        "remote_id": file_row["thumb_remote_id"] if "thumb_remote_id" in keys else None,
        "wrapped_key": file_row["thumb_wrapped_key"] if "thumb_wrapped_key" in keys else None,
        "mime": file_row["thumb_mime"] if "thumb_mime" in keys else None,
        "bytes": file_row["thumb_bytes"] if "thumb_bytes" in keys else None,
        "error": file_row["thumb_error"] if "thumb_error" in keys else None,
    }


def _entry_from_rows(
    file_row: Any,
    shard_rows: list[Any],
    folder_rows: list[Any],
) -> dict[str, Any]:
    thumb = _thumb_from_row(file_row)
    keys = file_row.keys() if hasattr(file_row, "keys") else ()
    remote_vault_dir_id = (
        file_row["remote_vault_dir_id"] if "remote_vault_dir_id" in keys else None
    )
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
        "remote_vault_dir_id": remote_vault_dir_id,
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
        "created_at": file_row["created_at"],
        "updated_at": file_row["updated_at"],
        "thumb_status": thumb["status"],
        "thumb": thumb,
    }


def _load_entry(conn: Any, file_id: str, user_id: str) -> dict[str, Any] | None:
    file_row = conn.execute(
        """
        SELECT id, path, size, mtime, chunk_size, crypto_alg, wrapped_file_key,
               created_at, updated_at,
               thumb_status, thumb_account_id, thumb_remote_id, thumb_wrapped_key,
               thumb_mime, thumb_bytes, thumb_error, remote_vault_dir_id
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
            SELECT id, path, size, mtime, chunk_size, crypto_alg, wrapped_file_key,
                   created_at, updated_at,
                   thumb_status, thumb_account_id, thumb_remote_id, thumb_wrapped_key,
                   thumb_mime, thumb_bytes, thumb_error
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


def list_health_objects(*, user_id: str | None = None) -> dict[str, Any]:
    """Slim checklist for shard health: shards + ready thumbnails only."""
    uid = user_id or auth_context.require_user_id()
    with connect() as conn:
        file_count = conn.execute(
            "SELECT COUNT(*) AS n FROM files WHERE user_id = ?",
            (uid,),
        ).fetchone()["n"]
        rows = conn.execute(
            """
            SELECT f.id AS file_id, f.path AS path, 'shard' AS kind, s.i AS i,
                   s.account_id AS account_id, s.remote_id AS remote_id
            FROM shards s
            JOIN files f ON f.id = s.file_id
            WHERE f.user_id = ?
            UNION ALL
            SELECT f.id AS file_id, f.path AS path, 'thumb' AS kind, NULL AS i,
                   f.thumb_account_id AS account_id, f.thumb_remote_id AS remote_id
            FROM files f
            WHERE f.user_id = ?
              AND f.thumb_status = 'ready'
              AND f.thumb_account_id IS NOT NULL
              AND f.thumb_remote_id IS NOT NULL
            ORDER BY path, kind, i
            """,
            (uid, uid),
        ).fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        items.append(
            {
                "kind": row["kind"],
                "file_id": row["file_id"],
                "path": row["path"] or row["file_id"],
                "i": None if row["i"] is None else int(row["i"]),
                "account_id": row["account_id"],
                "remote_id": row["remote_id"],
            }
        )
    return {"file_count": int(file_count), "items": items}


def id_path_map(*, user_id: str | None = None) -> dict[str, str]:
    """All file id → path mappings for the user."""
    uid = user_id or auth_context.require_user_id()
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, path FROM files WHERE user_id = ?",
            (uid,),
        ).fetchall()
    return {str(row["id"]): str(row["path"]) for row in rows}


def estimated_vault_bytes_by_account(
    *, user_id: str | None = None
) -> dict[str, int]:
    """Estimated ciphertext bytes per account (shards + ready thumbs)."""
    from services.capacity import estimate_shard_cipher_bytes

    uid = user_id or auth_context.require_user_id()
    totals: dict[str, int] = {}
    with connect() as conn:
        for row in conn.execute(
            """
            SELECT s.account_id AS account_id, f.size AS size, f.chunk_size AS chunk_size,
                   s.i AS i
            FROM shards s
            JOIN files f ON f.id = s.file_id
            WHERE f.user_id = ?
            """,
            (uid,),
        ).fetchall():
            aid = str(row["account_id"])
            totals[aid] = totals.get(aid, 0) + estimate_shard_cipher_bytes(
                int(row["size"] or 0),
                int(row["chunk_size"] or 1),
                int(row["i"]),
            )
        for row in conn.execute(
            """
            SELECT thumb_account_id AS account_id, COALESCE(thumb_bytes, 0) AS bytes
            FROM files
            WHERE user_id = ?
              AND thumb_account_id IS NOT NULL
              AND thumb_remote_id IS NOT NULL
            """,
            (uid,),
        ).fetchall():
            aid = str(row["account_id"])
            totals[aid] = totals.get(aid, 0) + max(0, int(row["bytes"] or 0))
    return totals


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


def usage_for_account(
    account_id: str,
    *,
    user_id: str | None = None,
) -> dict[str, Any]:
    """How many indexed shards/thumbs still reference ``account_id``."""
    uid = user_id or auth_context.require_user_id()
    with connect() as conn:
        shard_row = conn.execute(
            """
            SELECT COUNT(*) AS n, COUNT(DISTINCT s.file_id) AS files
            FROM shards s
            JOIN files f ON f.id = s.file_id
            WHERE f.user_id = ? AND s.account_id = ?
            """,
            (uid, account_id),
        ).fetchone()
        thumb_row = conn.execute(
            """
            SELECT COUNT(*) AS n
            FROM files
            WHERE user_id = ? AND thumb_account_id = ? AND thumb_remote_id IS NOT NULL
            """,
            (uid, account_id),
        ).fetchone()
        paths = [
            row["path"]
            for row in conn.execute(
                """
                SELECT DISTINCT f.path
                FROM shards s
                JOIN files f ON f.id = s.file_id
                WHERE f.user_id = ? AND s.account_id = ?
                ORDER BY f.path
                LIMIT 5
                """,
                (uid, account_id),
            ).fetchall()
        ]
        if not paths:
            paths = [
                row["path"]
                for row in conn.execute(
                    """
                    SELECT path FROM files
                    WHERE user_id = ? AND thumb_account_id = ? AND thumb_remote_id IS NOT NULL
                    ORDER BY path
                    LIMIT 5
                    """,
                    (uid, account_id),
                ).fetchall()
            ]
    shard_count = int(shard_row["n"]) if shard_row else 0
    file_count = int(shard_row["files"]) if shard_row else 0
    thumb_count = int(thumb_row["n"]) if thumb_row else 0
    return {
        "shard_count": shard_count,
        "file_count": file_count,
        "thumb_count": thumb_count,
        "sample_paths": paths,
        "in_use": shard_count > 0 or thumb_count > 0,
    }


def list_objects_for_account(
    account_id: str,
    *,
    user_id: str | None = None,
) -> dict[str, Any]:
    """Shards, thumbs, and per-file remote folders still on ``account_id``."""
    uid = user_id or auth_context.require_user_id()
    with connect() as conn:
        shard_rows = conn.execute(
            """
            SELECT f.id AS file_id, f.path AS path, f.size AS size, f.chunk_size AS chunk_size,
                   f.remote_vault_dir_id AS remote_vault_dir_id,
                   s.i AS i, s.remote_id AS remote_id, s.sha256 AS sha256, s.nonce AS nonce
            FROM shards s
            JOIN files f ON f.id = s.file_id
            WHERE f.user_id = ? AND s.account_id = ?
            ORDER BY f.path, s.i
            """,
            (uid, account_id),
        ).fetchall()
        thumb_rows = conn.execute(
            """
            SELECT id AS file_id, path, remote_vault_dir_id,
                   thumb_remote_id AS remote_id, thumb_wrapped_key AS wrapped_key,
                   thumb_mime AS mime, thumb_bytes AS bytes
            FROM files
            WHERE user_id = ? AND thumb_account_id = ? AND thumb_remote_id IS NOT NULL
            ORDER BY path
            """,
            (uid, account_id),
        ).fetchall()
        folder_rows = conn.execute(
            """
            SELECT frf.file_id AS file_id, frf.remote_folder_id AS remote_folder_id, f.path AS path
            FROM file_remote_folders frf
            JOIN files f ON f.id = frf.file_id
            WHERE f.user_id = ? AND frf.account_id = ?
            """,
            (uid, account_id),
        ).fetchall()
    return {
        "shards": [
            {
                "kind": "shard",
                "file_id": row["file_id"],
                "path": row["path"] or row["file_id"],
                "size": int(row["size"] or 0),
                "chunk_size": int(row["chunk_size"] or 1),
                "remote_vault_dir_id": row["remote_vault_dir_id"],
                "i": int(row["i"]),
                "remote_id": row["remote_id"],
                "sha256": row["sha256"],
                "nonce": row["nonce"],
            }
            for row in shard_rows
        ],
        "thumbs": [
            {
                "kind": "thumb",
                "file_id": row["file_id"],
                "path": row["path"] or row["file_id"],
                "remote_vault_dir_id": row["remote_vault_dir_id"],
                "remote_id": row["remote_id"],
                "wrapped_key": row["wrapped_key"],
                "mime": row["mime"],
                "bytes": row["bytes"],
            }
            for row in thumb_rows
        ],
        "source_folders": [
            {
                "file_id": row["file_id"],
                "path": row["path"] or row["file_id"],
                "remote_folder_id": row["remote_folder_id"],
            }
            for row in folder_rows
        ],
    }


def update_shard_placement(
    file_id: str,
    shard_i: int,
    *,
    account_id: str,
    remote_id: str,
    user_id: str | None = None,
) -> bool:
    """Point one shard at a new account/remote. Returns False if missing."""
    uid = user_id or auth_context.require_user_id()
    with connect() as conn:
        owned = conn.execute(
            "SELECT 1 FROM files WHERE id = ? AND user_id = ?",
            (file_id, uid),
        ).fetchone()
        if owned is None:
            return False
        cur = conn.execute(
            """
            UPDATE shards
            SET account_id = ?, remote_id = ?
            WHERE file_id = ? AND i = ?
            """,
            (account_id, remote_id, file_id, int(shard_i)),
        )
        if cur.rowcount:
            conn.execute(
                "UPDATE files SET updated_at = ? WHERE id = ? AND user_id = ?",
                (int(time.time()), file_id, uid),
            )
        return cur.rowcount > 0


def set_file_remote_folder(
    file_id: str,
    account_id: str,
    remote_folder_id: str,
    *,
    user_id: str | None = None,
) -> None:
    uid = user_id or auth_context.require_user_id()
    with connect() as conn:
        owned = conn.execute(
            "SELECT 1 FROM files WHERE id = ? AND user_id = ?",
            (file_id, uid),
        ).fetchone()
        if owned is None:
            raise FileNotFoundError("File not found")
        conn.execute(
            """
            INSERT INTO file_remote_folders (file_id, account_id, remote_folder_id)
            VALUES (?, ?, ?)
            ON CONFLICT(file_id, account_id) DO UPDATE SET
              remote_folder_id = excluded.remote_folder_id
            """,
            (file_id, account_id, remote_folder_id),
        )
        conn.execute(
            "UPDATE files SET updated_at = ? WHERE id = ? AND user_id = ?",
            (int(time.time()), file_id, uid),
        )


def clear_file_remote_folder(
    file_id: str,
    account_id: str,
    *,
    user_id: str | None = None,
) -> None:
    uid = user_id or auth_context.require_user_id()
    with connect() as conn:
        conn.execute(
            """
            DELETE FROM file_remote_folders
            WHERE file_id = ? AND account_id = ?
              AND EXISTS (
                SELECT 1 FROM files WHERE id = ? AND user_id = ?
              )
            """,
            (file_id, account_id, file_id, uid),
        )


def remote_folder_used_by_other_account(
    remote_folder_id: str,
    account_id: str,
    *,
    user_id: str | None = None,
) -> bool:
    """True when another account still maps a file to this remote folder id."""
    uid = user_id or auth_context.require_user_id()
    with connect() as conn:
        row = conn.execute(
            """
            SELECT 1
            FROM file_remote_folders frf
            JOIN files f ON f.id = frf.file_id
            WHERE f.user_id = ?
              AND frf.remote_folder_id = ?
              AND frf.account_id != ?
            LIMIT 1
            """,
            (uid, remote_folder_id, account_id),
        ).fetchone()
    return row is not None


def keep_refs_for_account(
    account_id: str,
    *,
    user_id: str | None = None,
) -> dict[str, set[str]]:
    """Names (file ids) and remote ids that prune must keep for ``account_id``."""
    uid = user_id or auth_context.require_user_id()
    names: set[str] = set()
    folder_ids: set[str] = set()
    shard_remote_ids: set[str] = set()
    with connect() as conn:
        for row in conn.execute(
            """
            SELECT frf.file_id, frf.remote_folder_id
            FROM file_remote_folders frf
            JOIN files f ON f.id = frf.file_id
            WHERE f.user_id = ? AND frf.account_id = ?
            """,
            (uid, account_id),
        ).fetchall():
            names.add(row["file_id"])
            folder_ids.add(row["remote_folder_id"])
        for row in conn.execute(
            """
            SELECT s.file_id, s.remote_id
            FROM shards s
            JOIN files f ON f.id = s.file_id
            WHERE f.user_id = ? AND s.account_id = ?
            """,
            (uid, account_id),
        ).fetchall():
            names.add(row["file_id"])
            shard_remote_ids.add(row["remote_id"])
        for row in conn.execute(
            """
            SELECT id, thumb_remote_id
            FROM files
            WHERE user_id = ? AND thumb_account_id = ? AND thumb_remote_id IS NOT NULL
            """,
            (uid, account_id),
        ).fetchall():
            names.add(f"thumb_{row['id']}.webp.enc")
            shard_remote_ids.add(row["thumb_remote_id"])
        for row in conn.execute(
            """
            SELECT fr.remote_folder_id, f.id AS folder_row_id
            FROM folder_remote_folders fr
            JOIN folders f ON f.id = fr.folder_id
            WHERE f.user_id = ? AND fr.account_id = ?
            """,
            (uid, account_id),
        ).fetchall():
            folder_ids.add(row["remote_folder_id"])
            names.add(row["folder_row_id"])
    return {
        "names": names,
        "folder_ids": folder_ids,
        "shard_remote_ids": shard_remote_ids,
    }


def upsert_file(
    index: dict[str, Any] | None,
    entry: dict[str, Any],
    *,
    user_id: str | None = None,
) -> dict[str, Any] | None:
    """Persist a file entry (and optional in-memory index update)."""
    uid = user_id or auth_context.require_user_id()
    crypto = entry.get("crypto") or {}
    now = int(time.time())
    mtime = int(entry.get("mtime") or now)
    with connect() as conn:
        existing = conn.execute(
            "SELECT created_at FROM files WHERE id = ? AND user_id = ?",
            (entry["id"], uid),
        ).fetchone()
        created_at = (
            int(existing["created_at"])
            if existing is not None
            else int(entry.get("created_at") or now)
        )
        entry["mtime"] = mtime
        entry["created_at"] = created_at
        entry["updated_at"] = now
        # Content replace clears thumb until regenerate (unless entry already has one).
        thumb_status = entry.get("thumb_status") or "none"
        entry["thumb_status"] = thumb_status
        # Thumb metadata may arrive nested (a "thumb" dict) or as flat "thumb_*" keys.
        thumb_dict = entry["thumb"] if isinstance(entry.get("thumb"), dict) else None

        def _thumb_col(key: str, flat: str) -> Any:
            return thumb_dict.get(key) if thumb_dict is not None else entry.get(flat)

        # Replace any other file at the same path for this user.
        conn.execute(
            "DELETE FROM files WHERE user_id = ? AND path = ? AND id != ?",
            (uid, entry["path"], entry["id"]),
        )
        conn.execute(
            """
            INSERT INTO files (
              id, user_id, path, size, mtime, chunk_size, crypto_alg, wrapped_file_key,
              created_at, updated_at,
              thumb_status, thumb_account_id, thumb_remote_id, thumb_wrapped_key,
              thumb_mime, thumb_bytes, thumb_error, remote_vault_dir_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              path = excluded.path,
              size = excluded.size,
              mtime = excluded.mtime,
              chunk_size = excluded.chunk_size,
              crypto_alg = excluded.crypto_alg,
              wrapped_file_key = excluded.wrapped_file_key,
              updated_at = excluded.updated_at,
              thumb_status = excluded.thumb_status,
              thumb_account_id = excluded.thumb_account_id,
              thumb_remote_id = excluded.thumb_remote_id,
              thumb_wrapped_key = excluded.thumb_wrapped_key,
              thumb_mime = excluded.thumb_mime,
              thumb_bytes = excluded.thumb_bytes,
              thumb_error = excluded.thumb_error,
              remote_vault_dir_id = excluded.remote_vault_dir_id
            """,
            (
                entry["id"],
                uid,
                entry["path"],
                int(entry["size"]),
                mtime,
                int(entry["chunk_size"]),
                crypto.get("alg") or "AES-256-GCM-CHUNK",
                crypto["wrapped_file_key"],
                created_at,
                now,
                thumb_status,
                _thumb_col("account_id", "thumb_account_id"),
                _thumb_col("remote_id", "thumb_remote_id"),
                _thumb_col("wrapped_key", "thumb_wrapped_key"),
                _thumb_col("mime", "thumb_mime"),
                _thumb_col("bytes", "thumb_bytes"),
                _thumb_col("error", "thumb_error"),
                entry.get("remote_vault_dir_id"),
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


def find_file_by_path(path: str, *, user_id: str | None = None) -> dict[str, Any] | None:
    uid = user_id or auth_context.require_user_id()
    with connect() as conn:
        row = conn.execute(
            "SELECT id FROM files WHERE user_id = ? AND path = ?",
            (uid, path),
        ).fetchone()
        if row is None:
            return None
        return _load_entry(conn, row["id"], uid)


def list_files_under_prefix(
    prefix: str,
    *,
    user_id: str | None = None,
) -> list[dict[str, Any]]:
    """Files at ``prefix`` itself or under ``prefix/``."""
    uid = user_id or auth_context.require_user_id()
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id FROM files
            WHERE user_id = ? AND (path = ? OR path LIKE ? ESCAPE '\\')
            ORDER BY path
            """,
            (uid, prefix, escape_like(prefix) + "/%"),
        ).fetchall()
        return [
            entry
            for row in rows
            if (entry := _load_entry(conn, row["id"], uid)) is not None
        ]


def rename_file_path(
    file_id: str,
    new_path: str,
    *,
    user_id: str | None = None,
) -> dict[str, Any] | None:
    uid = user_id or auth_context.require_user_id()
    with connect() as conn:
        entry = _load_entry(conn, file_id, uid)
        if entry is None:
            return None
        conflict = conn.execute(
            "SELECT id FROM files WHERE user_id = ? AND path = ? AND id != ?",
            (uid, new_path, file_id),
        ).fetchone()
        if conflict is not None:
            raise ValueError(f"A file already exists at {new_path}")
        now = int(time.time())
        conn.execute(
            """
            UPDATE files
            SET path = ?, mtime = ?, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (new_path, now, now, file_id, uid),
        )
        entry["path"] = new_path
        entry["mtime"] = now
        entry["updated_at"] = now
        return entry


def update_thumbnail(
    file_id: str,
    *,
    status: str,
    account_id: str | None = None,
    remote_id: str | None = None,
    wrapped_key: str | None = None,
    mime: str | None = None,
    size_bytes: int | None = None,
    error: str | None = None,
    user_id: str | None = None,
) -> None:
    """Persist thumbnail metadata without touching shards."""
    uid = user_id or auth_context.require_user_id()
    with connect() as conn:
        conn.execute(
            """
            UPDATE files
            SET thumb_status = ?,
                thumb_account_id = ?,
                thumb_remote_id = ?,
                thumb_wrapped_key = ?,
                thumb_mime = ?,
                thumb_bytes = ?,
                thumb_error = ?,
                updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (
                status,
                account_id,
                remote_id,
                wrapped_key,
                mime,
                size_bytes,
                error,
                int(time.time()),
                file_id,
                uid,
            ),
        )


def rename_files_under_prefix(
    old_prefix: str,
    new_prefix: str,
    *,
    user_id: str | None = None,
) -> int:
    uid = user_id or auth_context.require_user_id()
    now = int(time.time())
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, path FROM files
            WHERE user_id = ? AND (path = ? OR path LIKE ? ESCAPE '\\')
            ORDER BY length(path) DESC
            """,
            (uid, old_prefix, escape_like(old_prefix) + "/%"),
        ).fetchall()
        updated = 0
        for row in rows:
            suffix = row["path"][len(old_prefix) :]
            dest = new_prefix + suffix
            conflict = conn.execute(
                "SELECT id FROM files WHERE user_id = ? AND path = ? AND id != ?",
                (uid, dest, row["id"]),
            ).fetchone()
            if conflict is not None:
                raise ValueError(f"A file already exists at {dest}")
            conn.execute(
                """
                UPDATE files
                SET path = ?, mtime = ?, updated_at = ?
                WHERE id = ?
                """,
                (dest, now, now, row["id"]),
            )
            updated += 1
        return updated
