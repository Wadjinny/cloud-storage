from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Literal

from services.jobs import jobs


@dataclass
class DownloadSession:
    job_id: str
    user_id: str
    master_key: bytes
    filename: str
    size: int | None
    kind: Literal["file", "zip"] = "file"
    file_id: str | None = None
    zip_members: list[dict[str, Any]] | None = None
    zip_empty_dirs: list[str] | None = None
    cancel_requested: threading.Event = field(default_factory=threading.Event)
    stream_claimed: bool = False


class DownloadSessionHub:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, DownloadSession] = {}

    def start(
        self,
        *,
        job_id: str,
        user_id: str,
        master_key: bytes,
        filename: str,
        size: int | None,
        file_id: str | None = None,
        kind: Literal["file", "zip"] = "file",
        zip_members: list[dict[str, Any]] | None = None,
        zip_empty_dirs: list[str] | None = None,
    ) -> DownloadSession:
        session = DownloadSession(
            job_id=job_id,
            user_id=user_id,
            master_key=master_key,
            filename=filename,
            size=size,
            kind=kind,
            file_id=file_id,
            zip_members=zip_members,
            zip_empty_dirs=zip_empty_dirs,
        )
        with self._lock:
            self._sessions[job_id] = session
        return session

    def claim_stream(self, job_id: str) -> DownloadSession | None:
        """Hand the session to the stream generator (once)."""
        with self._lock:
            session = self._sessions.get(job_id)
            if session is None or session.stream_claimed:
                return None
            session.stream_claimed = True
            return session

    def get(self, job_id: str) -> DownloadSession | None:
        with self._lock:
            return self._sessions.get(job_id)

    def has_for_user(self, user_id: str) -> bool:
        with self._lock:
            return any(s.user_id == user_id for s in self._sessions.values())

    def finish(self, job_id: str) -> None:
        with self._lock:
            self._sessions.pop(job_id, None)

    def abort(self, job_id: str, message: str = "Download cancelled") -> bool:
        """Cancel a pending or in-flight download. Returns False if unknown."""
        with self._lock:
            session = self._sessions.get(job_id)
            known = session is not None
            if session is not None:
                session.cancel_requested.set()
                if not session.stream_claimed:
                    self._sessions.pop(job_id, None)
        job = jobs.get(job_id)
        if job is None and not known:
            return False
        jobs.request_cancel(job_id, message)
        return True


download_sessions = DownloadSessionHub()
