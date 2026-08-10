from __future__ import annotations

import json
import logging
import secrets
import time
from typing import Any

from config import HISTORY_RETENTION_DAYS
from storage.db import connect

log = logging.getLogger(__name__)

VALID_STATUSES = frozenset({"success", "error", "cancelled"})


def prune_expired(*, now: int | None = None) -> int:
    """Delete activity rows older than HISTORY_RETENTION_DAYS. Returns deleted count."""
    cutoff = (now if now is not None else int(time.time())) - HISTORY_RETENTION_DAYS * 86400
    with connect() as conn:
        cur = conn.execute("DELETE FROM activity_events WHERE created_at < ?", (cutoff,))
        return int(cur.rowcount or 0)


def record(
    user_id: str | None,
    action: str,
    status: str,
    *,
    path: str | None = None,
    detail: str | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append one activity event and prune expired rows."""
    if status not in VALID_STATUSES:
        raise ValueError(f"Invalid activity status: {status}")
    action = (action or "").strip()
    if not action:
        raise ValueError("action is required")
    now = int(time.time())
    event = {
        "id": f"act_{secrets.token_hex(8)}",
        "user_id": user_id,
        "action": action,
        "status": status,
        "path": path,
        "detail": detail,
        "meta": meta,
        "created_at": now,
    }
    meta_json = json.dumps(meta, separators=(",", ":")) if meta is not None else None
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO activity_events
              (id, user_id, action, status, path, detail, meta_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event["id"],
                user_id,
                action,
                status,
                path,
                detail,
                meta_json,
                now,
            ),
        )
        cutoff = now - HISTORY_RETENTION_DAYS * 86400
        conn.execute("DELETE FROM activity_events WHERE created_at < ?", (cutoff,))
    return event


def list_events(
    user_id: str,
    *,
    limit: int = 50,
    before: int | None = None,
    action: str | None = None,
    status: str | None = None,
) -> list[dict[str, Any]]:
    """Newest-first page for one user. Prunes expired rows first."""
    prune_expired()
    limit = max(1, min(int(limit), 200))
    clauses = ["user_id = ?"]
    params: list[Any] = [user_id]
    if before is not None:
        clauses.append("created_at < ?")
        params.append(int(before))
    if action:
        clauses.append("action = ?")
        params.append(action)
    if status:
        if status not in VALID_STATUSES:
            raise ValueError(f"Invalid activity status: {status}")
        clauses.append("status = ?")
        params.append(status)
    where = " AND ".join(clauses)
    params.append(limit)
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT id, user_id, action, status, path, detail, meta_json, created_at
            FROM activity_events
            WHERE {where}
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        meta = None
        raw = row["meta_json"]
        if raw:
            try:
                meta = json.loads(raw)
            except json.JSONDecodeError:
                meta = None
        out.append(
            {
                "id": row["id"],
                "user_id": row["user_id"],
                "action": row["action"],
                "status": row["status"],
                "path": row["path"],
                "detail": row["detail"],
                "meta": meta,
                "created_at": int(row["created_at"]),
            }
        )
    return out
