from __future__ import annotations

import logging
import queue
import secrets
import threading
import time
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

# phase, current, total, message, optional stats dict
ProgressCb = Callable[..., None]

log = logging.getLogger(__name__)

# Set while a run_in_thread worker executes so services can poll cancel.
_job_should_abort: ContextVar[Callable[[], bool] | None] = ContextVar(
    "job_should_abort", default=None
)


class JobCancelled(Exception):
    """Raised by job workers when the user cancels a running job."""


def should_abort() -> bool:
    fn = _job_should_abort.get()
    return bool(fn and fn())


def raise_if_cancelled(message: str = "Cancelled") -> None:
    if should_abort():
        raise JobCancelled(message)

# Map Job.kind → activity action (unknown kinds pass through unchanged).
_JOB_ACTION_MAP: dict[str, str] = {
    "upload": "upload",
    "download": "download",
    "download_zip": "download_zip",
    "delete": "delete_file",
    "delete_file": "delete_file",
    "delete_dir": "delete_dir",
    "health": "health",
    "prune": "prune",
    "prune_analyze": "prune_analyze",
    "evacuate": "account_evacuate",
    "copy": "copy",
}


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
            def on_progress(
                phase: str,
                current: int,
                total: int,
                message: str,
                stats: dict[str, Any] | None = None,
            ) -> None:
                if self.is_cancel_requested(job.id):
                    raise JobCancelled(job.message or job.error or "Cancelled")
                self.update(
                    job.id,
                    phase=phase,
                    current=current,
                    total=total,
                    message=message,
                    stats=stats,
                )

            abort_token = _job_should_abort.set(
                lambda: self.is_cancel_requested(job.id)
            )
            try:
                with auth_context.auth_scope(user_id, master_key):
                    try:
                        result = work(on_progress)
                        if self.is_cancel_requested(job.id):
                            self.request_cancel(
                                job.id, "Cancelled", finalize=True
                            )
                            return
                        self.complete(job.id, result_meta=result)
                    except JobCancelled as exc:
                        msg = str(exc) or "Cancelled"
                        self.request_cancel(job.id, msg, finalize=True)
                    except Exception as exc:
                        self.fail(job.id, str(exc))
            finally:
                _job_should_abort.reset(abort_token)

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
        snapshot: Job | None = None
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
            snapshot = job
        self._notify(job_id)
        self._record_activity(snapshot, "success")

    def fail(
        self,
        job_id: str,
        error: str,
        *,
        result_meta: dict[str, Any] | None = None,
    ) -> None:
        snapshot: Job | None = None
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
            snapshot = job
        self._notify(job_id)
        self._record_activity(snapshot, "error")

    def request_cancel(
        self,
        job_id: str,
        message: str = "Cancelled",
        *,
        finalize: bool = True,
    ) -> bool:
        """Request cancel. With ``finalize=False``, only signals the worker so it
        can drain in-flight work; the worker must finalize later.
        """
        snapshot: Job | None = None
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            job.cancel_requested = True
            if job.status == "running":
                if finalize:
                    job.status = "error"
                    job.phase = "error"
                    job.message = message
                    job.error = message
                    snapshot = job
                else:
                    job.message = message
            elif finalize and job.status == "error" and message:
                job.message = message
                job.error = message
        self._notify(job_id)
        if snapshot is not None:
            self._record_activity(snapshot, "cancelled")
        return True

    def _record_activity(self, job: Job, status: str) -> None:
        if not job.user_id:
            return
        try:
            from storage import activity as activity_store

            meta = dict(job.result_meta) if isinstance(job.result_meta, dict) else {}
            meta["job_id"] = job.id
            meta["job_kind"] = job.kind
            path = None
            if isinstance(job.result_meta, dict):
                raw_path = job.result_meta.get("path")
                if isinstance(raw_path, str) and raw_path:
                    path = raw_path
            if path is None and job.result_name:
                path = job.result_name
            detail = None
            if status == "success":
                detail = job.message if job.message and job.message != "Done" else None
            else:
                detail = job.error or job.message
            activity_store.record(
                job.user_id,
                _JOB_ACTION_MAP.get(job.kind, job.kind),
                status,
                path=path,
                detail=detail,
                meta=meta,
            )
        except Exception:
            log.exception("activity record failed job_id=%s", job.id)

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
