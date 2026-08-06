from __future__ import annotations

import json
import threading
import time
from typing import Any

from config import DATA_DIR, UPLOAD_SESSIONS_DIR

_lock = threading.Lock()


def _path(session_id: str) -> Any:
    return UPLOAD_SESSIONS_DIR / f"{session_id}.json"


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


def save(session: dict[str, Any]) -> None:
    session = dict(session)
    session["updated_at"] = int(time.time())
    with _lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        UPLOAD_SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        path = _path(session["id"])
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(session, indent=2), encoding="utf-8")
        tmp.replace(path)


def load(session_id: str) -> dict[str, Any] | None:
    with _lock:
        path = _path(session_id)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))


def delete(session_id: str) -> None:
    with _lock:
        path = _path(session_id)
        path.unlink(missing_ok=True)


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
