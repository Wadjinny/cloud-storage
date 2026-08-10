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


def _filter_clauses(
    user_id: str,
    *,
    before: int | None = None,
    since: int | None = None,
    until: int | None = None,
    action: str | None = None,
    status: str | None = None,
) -> tuple[str, list[Any]]:
    clauses = ["user_id = ?"]
    params: list[Any] = [user_id]
    if before is not None:
        clauses.append("created_at < ?")
        params.append(int(before))
    if since is not None:
        clauses.append("created_at >= ?")
        params.append(int(since))
    if until is not None:
        clauses.append("created_at <= ?")
        params.append(int(until))
    if action:
        clauses.append("action = ?")
        params.append(action)
    if status:
        if status not in VALID_STATUSES:
            raise ValueError(f"Invalid activity status: {status}")
        clauses.append("status = ?")
        params.append(status)
    return " AND ".join(clauses), params


def list_events(
    user_id: str,
    *,
    limit: int = 50,
    before: int | None = None,
    since: int | None = None,
    until: int | None = None,
    action: str | None = None,
    status: str | None = None,
) -> list[dict[str, Any]]:
    """Newest-first page for one user. Prunes expired rows first."""
    prune_expired()
    limit = max(1, min(int(limit), 200))
    where, params = _filter_clauses(
        user_id,
        before=before,
        since=since,
        until=until,
        action=action,
        status=status,
    )
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


def summarize_events(
    user_id: str,
    *,
    since: int | None = None,
    until: int | None = None,
    action: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    """Aggregate activity for metrics (counts + transfer bytes from meta)."""
    prune_expired()
    where, params = _filter_clauses(
        user_id,
        since=since,
        until=until,
        action=action,
        status=status,
    )
    with connect() as conn:
        by_status_rows = conn.execute(
            f"""
            SELECT status, COUNT(*) AS n
            FROM activity_events
            WHERE {where}
            GROUP BY status
            """,
            params,
        ).fetchall()
        by_action_rows = conn.execute(
            f"""
            SELECT action, COUNT(*) AS n
            FROM activity_events
            WHERE {where}
            GROUP BY action
            ORDER BY n DESC, action ASC
            """,
            params,
        ).fetchall()
        # Transfer volume from job meta (upload/download size, evacuate moved_bytes).
        byte_row = conn.execute(
            f"""
            SELECT
              COALESCE(SUM(
                CASE WHEN action = 'upload' AND status = 'success'
                  THEN COALESCE(CAST(json_extract(meta_json, '$.size') AS INTEGER), 0)
                  ELSE 0 END
              ), 0) AS upload_bytes,
              COALESCE(SUM(
                CASE WHEN action IN ('download', 'download_zip') AND status = 'success'
                  THEN COALESCE(CAST(json_extract(meta_json, '$.size') AS INTEGER), 0)
                  ELSE 0 END
              ), 0) AS download_bytes,
              COALESCE(SUM(
                CASE WHEN action = 'account_evacuate' AND status = 'success'
                  THEN COALESCE(
                    CAST(json_extract(meta_json, '$.moved_bytes') AS INTEGER), 0
                  )
                  ELSE 0 END
              ), 0) AS evacuated_bytes,
              COALESCE(SUM(
                CASE WHEN action = 'upload' AND status = 'success' THEN 1 ELSE 0 END
              ), 0) AS upload_ok,
              COALESCE(SUM(
                CASE WHEN action IN ('download', 'download_zip') AND status = 'success'
                  THEN 1 ELSE 0 END
              ), 0) AS download_ok,
              COALESCE(SUM(
                CASE WHEN action = 'account_evacuate' AND status = 'success'
                  THEN 1 ELSE 0 END
              ), 0) AS evacuate_ok
            FROM activity_events
            WHERE {where}
            """,
            params,
        ).fetchone()

    by_status = {s: 0 for s in ("success", "error", "cancelled")}
    total = 0
    for row in by_status_rows:
        key = str(row["status"])
        n = int(row["n"] or 0)
        by_status[key] = n
        total += n

    by_action = [
        {"action": str(row["action"]), "count": int(row["n"] or 0)}
        for row in by_action_rows
    ]

    return {
        "events": total,
        "by_status": by_status,
        "by_action": by_action,
        "upload_bytes": int(byte_row["upload_bytes"] or 0) if byte_row else 0,
        "download_bytes": int(byte_row["download_bytes"] or 0) if byte_row else 0,
        "evacuated_bytes": int(byte_row["evacuated_bytes"] or 0) if byte_row else 0,
        "upload_ok": int(byte_row["upload_ok"] or 0) if byte_row else 0,
        "download_ok": int(byte_row["download_ok"] or 0) if byte_row else 0,
        "evacuate_ok": int(byte_row["evacuate_ok"] or 0) if byte_row else 0,
        "since": since,
        "until": until,
        "retention_days": HISTORY_RETENTION_DAYS,
    }
