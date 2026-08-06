from __future__ import annotations

import threading
from dataclasses import dataclass

from services.jobs import jobs


@dataclass
class DownloadSession:
    job_id: str
    file_id: str
    master_key: bytes
    filename: str
    size: int | None


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

    def take(self, job_id: str) -> DownloadSession | None:
        with self._lock:
            return self._sessions.pop(job_id, None)

    def get(self, job_id: str) -> DownloadSession | None:
        with self._lock:
            return self._sessions.get(job_id)

    def abort(self, job_id: str, message: str = "Download aborted") -> None:
        with self._lock:
            session = self._sessions.pop(job_id, None)
        # Only fail the job if the stream never started (session still pending).
        if session is None:
            return
        job = jobs.get(job_id)
        if job is not None and job.status not in ("done", "error"):
            jobs.fail(job_id, message)


download_sessions = DownloadSessionHub()
