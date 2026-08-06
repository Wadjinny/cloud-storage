from __future__ import annotations

import threading
from dataclasses import dataclass, field

from services.jobs import jobs


@dataclass
class DownloadSession:
    job_id: str
    file_id: str
    master_key: bytes
    filename: str
    size: int | None
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
        file_id: str,
        master_key: bytes,
        filename: str,
        size: int | None,
    ) -> DownloadSession:
        session = DownloadSession(
            job_id=job_id,
            file_id=file_id,
            master_key=master_key,
            filename=filename,
            size=size,
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
