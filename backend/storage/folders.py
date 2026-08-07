from __future__ import annotations

import secrets
import time
from typing import Any

from storage import auth_context
from storage.db import connect


def normalize_dir(path: str) -> str:
    if not path or path == "/":
        return "/"
    parts = [p for p in path.strip("/").split("/") if p]
    if not parts:
        return "/"
    return "/" + "/".join(parts)


def validate_name(name: str) -> str:
    name = name.strip()
    if not name:
        raise ValueError("Name is required")
    if name in (".", ".."):
        raise ValueError("Invalid name")
    if "/" in name or "\\" in name or "\0" in name:
        raise ValueError("Name cannot contain path separators")
    if len(name) > 255:
        raise ValueError("Name is too long")
    return name


def join_path(dir_path: str, name: str) -> str:
    base = normalize_dir(dir_path)
    clean = validate_name(name)
    if base == "/":
        return f"/{clean}"
    return f"{base}/{clean}"


def _get_folder_row(conn: Any, uid: str, path: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT id, user_id, path, mtime, created_at, updated_at
        FROM folders WHERE user_id = ? AND path = ?
        """,
        (uid, path),
    ).fetchone()
    return dict(row) if row else None


def _ensure_folder_conn(conn: Any, uid: str, path: str) -> dict[str, Any]:
    norm = normalize_dir(path)
    if norm == "/":
        return {
            "id": "dir:/",
            "user_id": uid,
            "path": "/",
            "mtime": None,
            "created_at": None,
            "updated_at": None,
        }

    parts = [p for p in norm.strip("/").split("/") if p]
    current = ""
    leaf: dict[str, Any] | None = None
    now = int(time.time())
    for part in parts:
        validate_name(part)
        current = f"{current}/{part}"
        row = _get_folder_row(conn, uid, current)
        if row is None:
            folder_id = f"dir_{secrets.token_hex(8)}"
            conn.execute(
                """
                INSERT INTO folders (id, user_id, path, mtime, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (folder_id, uid, current, now, now, now),
            )
            leaf = {
                "id": folder_id,
                "user_id": uid,
                "path": current,
                "mtime": now,
                "created_at": now,
                "updated_at": now,
            }
        else:
            leaf = row
    assert leaf is not None
    return leaf


def get_folder(path: str, *, user_id: str | None = None) -> dict[str, Any] | None:
    uid = user_id or auth_context.require_user_id()
    norm = normalize_dir(path)
    if norm == "/":
        return {
            "id": "dir:/",
            "user_id": uid,
            "path": "/",
            "mtime": None,
            "created_at": None,
            "updated_at": None,
        }
    with connect() as conn:
        return _get_folder_row(conn, uid, norm)


def list_child_names(base: str, *, user_id: str | None = None) -> list[dict[str, Any]]:
    """Folders whose path is exactly one level under ``base``."""
    uid = user_id or auth_context.require_user_id()
    base_norm = normalize_dir(base)
    prefix = "" if base_norm == "/" else base_norm
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, path, mtime, created_at, updated_at
            FROM folders WHERE user_id = ? ORDER BY path
            """,
            (uid,),
        ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        path = row["path"]
        if prefix:
            if not path.startswith(prefix + "/"):
                continue
            rest = path[len(prefix) + 1 :]
        else:
            rest = path.lstrip("/")
        if not rest or "/" in rest:
            continue
        out.append(
            {
                "id": row["id"],
                "name": rest,
                "path": path,
                "mtime": row["mtime"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
        )
    return out


def ensure_folder(path: str, *, user_id: str | None = None) -> dict[str, Any]:
    """Create ``path`` and any missing parents. Returns the leaf folder."""
    uid = user_id or auth_context.require_user_id()
    with connect() as conn:
        return _ensure_folder_conn(conn, uid, path)


def mkdir(parent: str, name: str, *, user_id: str | None = None) -> dict[str, Any]:
    uid = user_id or auth_context.require_user_id()
    parent_norm = normalize_dir(parent)
    path = join_path(parent_norm, name)
    with connect() as conn:
        if parent_norm != "/" and _get_folder_row(conn, uid, parent_norm) is None:
            _ensure_folder_conn(conn, uid, parent_norm)
        existing = _get_folder_row(conn, uid, path)
        if existing is not None:
            raise ValueError("Folder already exists")
        return _ensure_folder_conn(conn, uid, path)


def rename_folder_tree(
    old_path: str,
    new_path: str,
    *,
    user_id: str | None = None,
) -> int:
    """Rename a folder and all descendant folder rows. Returns rows updated."""
    uid = user_id or auth_context.require_user_id()
    old_norm = normalize_dir(old_path)
    new_norm = normalize_dir(new_path)
    if old_norm == "/":
        raise ValueError("Cannot rename root")
    if new_norm == "/":
        raise ValueError("Invalid destination")
    if new_norm == old_norm:
        return 0
    if new_norm.startswith(old_norm + "/"):
        raise ValueError("Cannot move a folder into itself")

    now = int(time.time())
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, path FROM folders
            WHERE user_id = ? AND (path = ? OR path LIKE ?)
            ORDER BY length(path) DESC
            """,
            (uid, old_norm, old_norm + "/%"),
        ).fetchall()
        updated = 0
        for row in rows:
            suffix = row["path"][len(old_norm) :]
            dest = new_norm + suffix
            conflict = conn.execute(
                "SELECT id FROM folders WHERE user_id = ? AND path = ? AND id != ?",
                (uid, dest, row["id"]),
            ).fetchone()
            if conflict is not None:
                raise ValueError(f"Destination folder already exists: {dest}")
            conn.execute(
                """
                UPDATE folders
                SET path = ?, mtime = ?, updated_at = ?
                WHERE id = ?
                """,
                (dest, now, now, row["id"]),
            )
            updated += 1
        if updated == 0:
            _ensure_folder_conn(conn, uid, new_norm)
        else:
            # Ensure intermediate parents of the new path exist.
            parent = new_norm.rsplit("/", 1)[0] or "/"
            if parent != "/":
                _ensure_folder_conn(conn, uid, parent)
        return updated


def delete_folder_tree(path: str, *, user_id: str | None = None) -> int:
    """Delete folder row and all descendant folder rows. Returns count deleted."""
    uid = user_id or auth_context.require_user_id()
    norm = normalize_dir(path)
    if norm == "/":
        raise ValueError("Cannot delete root")
    with connect() as conn:
        cur = conn.execute(
            """
            DELETE FROM folders
            WHERE user_id = ? AND (path = ? OR path LIKE ?)
            """,
            (uid, norm, norm + "/%"),
        )
        return cur.rowcount
