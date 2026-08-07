from __future__ import annotations

import queue
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

ProgressCb = Callable[[str, int, int, str], None]


@dataclass
class Job:
    id: str
    kind: str
    user_id: str | None = None
    status: str = "running"  # running | done | error
    phase: str = ""
    message: str = ""
    current: int = 0
    total: int = 0
    error: str | None = None
    result_meta: dict[str, Any] | None = None
    result_name: str | None = None
    # Live transfer breakdown (e.g. per-account upload shard counts).
    stats: dict[str, Any] | None = None
    cancel_requested: bool = False
    created_at: float = field(default_factory=time.time)

    def to_public(self) -> dict[str, Any]:
        percent = 0
        if self.total > 0:
            percent = min(100, int(round(100 * self.current / self.total)))
        elif self.status == "done":
            percent = 100
        return {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "phase": self.phase,
            "message": self.message,
            "current": self.current,
            "total": self.total,
            "percent": percent,
            "error": self.error,
            "result": self.result_meta,
            "filename": self.result_name,
            "stats": self.stats,
            "cancel_requested": self.cancel_requested,
        }


class JobStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._subs: dict[str, list[queue.Queue[dict[str, Any] | None]]] = {}

    def create(self, kind: str, *, user_id: str | None = None) -> Job:
        job = Job(id=f"job_{secrets.token_hex(8)}", kind=kind, user_id=user_id)
        with self._lock:
            self._jobs[job.id] = job
            self._prune_locked()
        self._notify(job.id)
        return job

    def run_in_thread(
        self,
        kind: str,
        *,
        user_id: str,
        master_key: bytes,
        work: Callable[[ProgressCb], dict[str, Any] | None],
        queued_message: str,
    ) -> str:
        """Create a job and run ``work(on_progress)`` in a daemon thread.

        The worker runs inside the user's auth scope; its return value (if any)
        is stored as the job's ``result_meta``, and any exception fails the job.
        Returns the new job id.
        """
        # Imported here to avoid a storage->services import at module load.
        from storage import auth_context

        job = self.create(kind, user_id=user_id)
        self.update(job.id, phase="queued", message=queued_message, current=0, total=1)

        def _runner() -> None:
            def on_progress(phase: str, current: int, total: int, message: str) -> None:
                self.update(
                    job.id, phase=phase, current=current, total=total, message=message
                )

            with auth_context.auth_scope(user_id, master_key):
                try:
                    self.complete(job.id, result_meta=work(on_progress))
                except Exception as exc:
                    self.fail(job.id, str(exc))

        threading.Thread(target=_runner, daemon=True).start()
        return job.id

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def has_running_for_user(self, user_id: str) -> bool:
        with self._lock:
            return any(
                j.user_id == user_id and j.status == "running" for j in self._jobs.values()
            )

    def update(
        self,
        job_id: str,
        *,
        phase: str | None = None,
        message: str | None = None,
        current: int | None = None,
        total: int | None = None,
        stats: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            if phase is not None:
                job.phase = phase
            if message is not None:
                job.message = message
            if current is not None:
                job.current = current
            if total is not None:
                job.total = total
            if stats is not None:
                job.stats = stats
        self._notify(job_id)

    def complete(
        self,
        job_id: str,
        *,
        result_meta: dict[str, Any] | None = None,
        result_name: str | None = None,
    ) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            if job.status != "running":
                return
            job.status = "done"
            job.phase = "done"
            job.message = "Done"
            job.current = job.total or job.current
            job.result_meta = result_meta
            job.result_name = result_name
        self._notify(job_id)

    def fail(
        self,
        job_id: str,
        error: str,
        *,
        result_meta: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            if job.status != "running":
                return
            job.status = "error"
            job.phase = "error"
            job.message = error
            job.error = error
            if result_meta is not None:
                job.result_meta = result_meta
        self._notify(job_id)

    def request_cancel(self, job_id: str, message: str = "Cancelled") -> bool:
        """Mark job cancelled and fail it if still running. Returns False if missing."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            job.cancel_requested = True
            if job.status == "running":
                job.status = "error"
                job.phase = "error"
                job.message = message
                job.error = message
        self._notify(job_id)
        return True

    def is_cancel_requested(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            return bool(job and job.cancel_requested)

    def subscribe(self, job_id: str) -> queue.Queue[dict[str, Any] | None]:
        q: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=64)
        with self._lock:
            self._subs.setdefault(job_id, []).append(q)
            job = self._jobs.get(job_id)
            snapshot = job.to_public() if job is not None else None
        if snapshot is not None:
            try:
                q.put_nowait(snapshot)
            except queue.Full:
                pass
        return q

    def unsubscribe(self, job_id: str, q: queue.Queue[dict[str, Any] | None]) -> None:
        with self._lock:
            subs = self._subs.get(job_id)
            if not subs:
                return
            try:
                subs.remove(q)
            except ValueError:
                pass
            if not subs:
                self._subs.pop(job_id, None)

    def _notify(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            payload = job.to_public()
            subs = list(self._subs.get(job_id, []))
        for q in subs:
            try:
                q.put_nowait(payload)
            except queue.Full:
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    q.put_nowait(payload)
                except queue.Full:
                    pass

    def _prune_locked(self) -> None:
        cutoff = time.time() - 3600
        stale = [jid for jid, j in self._jobs.items() if j.created_at < cutoff]
        for jid in stale:
            self._subs.pop(jid, None)
            del self._jobs[jid]


jobs = JobStore()
