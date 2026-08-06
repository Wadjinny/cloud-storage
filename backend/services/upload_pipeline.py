from __future__ import annotations

import logging
import queue
import secrets
import threading
import time
from collections.abc import Iterator
from concurrent.futures import CancelledError
from dataclasses import dataclass, field
from typing import Any

from config import SHARD_UPLOAD_CONCURRENCY, UPLOAD_QUEUE_DEPTH, choose_chunk_size
from services import vault as vault_service
from services.jobs import jobs
from storage import auth_context
from storage import upload_sessions

_SENTINEL = object()
log = logging.getLogger(__name__)


class UploadCancelled(RuntimeError):
    pass


def _live_shard_slots() -> int:
    """Max plaintext chunks that may exist in queue + encrypt workers at once."""
    return max(1, SHARD_UPLOAD_CONCURRENCY + max(0, UPLOAD_QUEUE_DEPTH))


@dataclass
class UploadPipeline:
    job_id: str
    user_id: str
    master_key: bytes
    path: str
    filename: str
    size: int | None
    chunk_size: int
    session_id: str
    resume_from_byte: int = 0
    chunk_queue: queue.Queue[Any] = field(init=False)
    # One permit per live plaintext shard (acquired in put_chunk, released after upload).
    shard_slots: threading.BoundedSemaphore = field(init=False)
    done: threading.Event = field(default_factory=threading.Event)
    cancel_requested: threading.Event = field(default_factory=threading.Event)
    error: str | None = None

    def __post_init__(self) -> None:
        slots = _live_shard_slots()
        self.chunk_queue = queue.Queue(maxsize=slots)
        self.shard_slots = threading.BoundedSemaphore(slots)


class UploadPipelineHub:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pipelines: dict[str, UploadPipeline] = {}

    def start(
        self,
        *,
        job_id: str,
        user_id: str,
        master_key: bytes,
        path: str,
        filename: str,
        size: int | None,
        chunk_size: int | None = None,
        session_id: str | None = None,
        resume_from_byte: int = 0,
    ) -> UploadPipeline:
        shard_size = chunk_size if chunk_size is not None else choose_chunk_size(size)
        sid = session_id or f"f_{secrets.token_hex(8)}"
        pipeline = UploadPipeline(
            job_id=job_id,
            user_id=user_id,
            master_key=master_key,
            path=path,
            filename=filename,
            size=size,
            chunk_size=shard_size,
            session_id=sid,
            resume_from_byte=max(0, resume_from_byte),
        )
        with self._lock:
            self._pipelines[job_id] = pipeline

        thread = threading.Thread(
            target=self._worker,
            args=(pipeline,),
            name=f"upload-pipeline-{job_id}",
            daemon=True,
        )
        thread.start()
        log.info(
            "upload pipeline started job_id=%s session_id=%s size=%s chunk_size=%s "
            "resume_from_byte=%s live_slots=%s",
            job_id,
            sid,
            size,
            shard_size,
            pipeline.resume_from_byte,
            _live_shard_slots(),
        )
        return pipeline

    def get(self, job_id: str) -> UploadPipeline | None:
        with self._lock:
            return self._pipelines.get(job_id)

    def _drop(self, job_id: str) -> None:
        with self._lock:
            self._pipelines.pop(job_id, None)

    def _release_slot(self, pipeline: UploadPipeline) -> None:
        try:
            pipeline.shard_slots.release()
        except ValueError:
            # Extra release; ignore so cancel/drain stays idempotent.
            pass

    def _resume_meta(self, pipeline: UploadPipeline) -> dict[str, Any] | None:
        session = upload_sessions.load(pipeline.session_id)
        if session is None:
            return None
        return {"resume": upload_sessions.public_resume_info(session)}

    def _chunk_iter(self, pipeline: UploadPipeline) -> Iterator[bytes]:
        saw = False
        while True:
            if pipeline.cancel_requested.is_set():
                raise UploadCancelled("Upload cancelled")
            try:
                item = pipeline.chunk_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if item is _SENTINEL:
                if not saw:
                    # Empty file: no put_chunk acquired a slot.
                    yield b""
                break
            if pipeline.cancel_requested.is_set():
                self._release_slot(pipeline)
                raise UploadCancelled("Upload cancelled")
            saw = True
            # Slot stays held until upload_file's on_chunk_done after encrypt/upload.
            yield item

    def _worker(self, pipeline: UploadPipeline) -> None:
        with auth_context.auth_scope(pipeline.user_id, pipeline.master_key):
            self._worker_inner(pipeline)

    def _worker_inner(self, pipeline: UploadPipeline) -> None:
        def on_progress(
            phase: str,
            current: int,
            total: int,
            message: str,
            stats: dict[str, Any] | None = None,
        ) -> None:
            if pipeline.cancel_requested.is_set():
                return
            jobs.update(
                pipeline.job_id,
                phase=phase,
                current=current,
                total=total,
                message=message,
                stats=stats,
            )

        def on_chunk_done() -> None:
            self._release_slot(pipeline)

        try:
            entry = vault_service.upload_file(
                pipeline.master_key,
                pipeline.path,
                pipeline.filename,
                self._chunk_iter(pipeline),
                size=pipeline.size,
                chunk_size=pipeline.chunk_size,
                on_progress=on_progress,
                should_abort=pipeline.cancel_requested.is_set,
                on_chunk_done=on_chunk_done,
                session_id=pipeline.session_id,
                resume_from_byte=pipeline.resume_from_byte,
            )
            if pipeline.cancel_requested.is_set():
                raise UploadCancelled("Upload cancelled")
            jobs.complete(
                pipeline.job_id,
                result_meta={
                    "id": entry["id"],
                    "path": entry["path"],
                    "size": entry["size"],
                    "shard_count": len(entry["shards"]),
                },
            )
            log.info(
                "upload pipeline done job_id=%s file_id=%s shards=%s",
                pipeline.job_id,
                entry["id"],
                len(entry["shards"]),
            )
        except UploadCancelled as exc:
            pipeline.error = str(exc)
            jobs.request_cancel(pipeline.job_id, str(exc))
            self._drain(pipeline)
            log.info("upload pipeline cancelled job_id=%s", pipeline.job_id)
        except Exception as exc:
            # Cancel races surface as RuntimeError("Upload cancelled") from workers.
            if str(exc) == "Upload cancelled" or pipeline.cancel_requested.is_set():
                pipeline.error = "Upload cancelled"
                jobs.request_cancel(pipeline.job_id, "Upload cancelled")
                self._drain(pipeline)
                log.info("upload pipeline cancelled job_id=%s", pipeline.job_id)
            elif isinstance(exc, CancelledError) or type(exc).__name__ == "CancelledError":
                pipeline.error = (
                    "Provider upload interrupted; remote shards were kept for retry"
                )
                job = jobs.get(pipeline.job_id)
                if job is None or job.status == "running":
                    jobs.fail(
                        pipeline.job_id,
                        pipeline.error,
                        result_meta=self._resume_meta(pipeline),
                    )
                self._drain(pipeline)
                log.exception("upload pipeline interrupted job_id=%s", pipeline.job_id)
            else:
                pipeline.error = str(exc) or type(exc).__name__
                job = jobs.get(pipeline.job_id)
                if job is None or job.status == "running":
                    jobs.fail(
                        pipeline.job_id,
                        pipeline.error,
                        result_meta=self._resume_meta(pipeline),
                    )
                self._drain(pipeline)
                log.exception("upload pipeline failed job_id=%s", pipeline.job_id)
        finally:
            pipeline.done.set()

    def _drain(self, pipeline: UploadPipeline) -> None:
        while True:
            try:
                item = pipeline.chunk_queue.get_nowait()
            except queue.Empty:
                break
            if item is not _SENTINEL:
                self._release_slot(pipeline)

    def put_chunk(self, job_id: str, data: bytes) -> None:
        pipeline = self.get(job_id)
        if pipeline is None:
            raise RuntimeError("Upload pipeline not found")
        # Acquire a live-shard slot before retaining plaintext (bounds RAM by concurrency).
        wait_t0 = time.monotonic()
        last_wait_log = wait_t0
        while True:
            if pipeline.cancel_requested.is_set() or pipeline.done.is_set():
                raise UploadCancelled(pipeline.error or "Upload cancelled")
            if pipeline.shard_slots.acquire(timeout=0.2):
                break
            now = time.monotonic()
            if now - last_wait_log >= 5.0:
                log.info(
                    "upload put_chunk waiting for slot job_id=%s bytes=%s waited=%.1fs",
                    job_id,
                    len(data),
                    now - wait_t0,
                )
                last_wait_log = now
            pipeline = self.get(job_id)
            if pipeline is None:
                raise UploadCancelled("Upload cancelled") from None

        try:
            while True:
                if pipeline.cancel_requested.is_set() or pipeline.done.is_set():
                    raise UploadCancelled(pipeline.error or "Upload cancelled")
                try:
                    pipeline.chunk_queue.put(data, timeout=0.2)
                    return
                except queue.Full:
                    # Do not hold a live-shard slot while the queue is full — that
                    # stranded permits and made put_wait spike even with free workers.
                    self._release_slot(pipeline)
                    now = time.monotonic()
                    if now - last_wait_log >= 5.0:
                        log.info(
                            "upload put_chunk queue full job_id=%s bytes=%s waited=%.1fs",
                            job_id,
                            len(data),
                            now - wait_t0,
                        )
                        last_wait_log = now
                    pipeline = self.get(job_id)
                    if pipeline is None:
                        raise UploadCancelled("Upload cancelled") from None
                    # Re-acquire before retrying the put.
                    while True:
                        if pipeline.cancel_requested.is_set() or pipeline.done.is_set():
                            raise UploadCancelled(pipeline.error or "Upload cancelled")
                        if pipeline.shard_slots.acquire(timeout=0.2):
                            break
                        pipeline = self.get(job_id)
                        if pipeline is None:
                            raise UploadCancelled("Upload cancelled") from None
        except Exception:
            self._release_slot(pipeline)
            raise

    def _offer_sentinel(self, pipeline: UploadPipeline, *, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if pipeline.done.is_set():
                return
            try:
                pipeline.chunk_queue.put(_SENTINEL, timeout=0.2)
                return
            except queue.Full:
                if pipeline.cancel_requested.is_set():
                    self._drain(pipeline)

    def finish(self, job_id: str) -> None:
        pipeline = self.get(job_id)
        if pipeline is None:
            job = jobs.get(job_id)
            if job is not None and job.status == "error":
                raise RuntimeError(job.error or "Upload failed")
            return
        if pipeline.cancel_requested.is_set():
            if not pipeline.done.wait(timeout=60):
                log.error("upload finish: cancel wait timed out job_id=%s", job_id)
            self._drop(job_id)
            raise UploadCancelled(pipeline.error or "Upload cancelled")

        log.info("upload finish: offering sentinel job_id=%s", job_id)
        self._offer_sentinel(pipeline, timeout=60)
        if not pipeline.done.wait(timeout=120):
            pipeline.error = pipeline.error or "Upload worker timed out"
            jobs.fail(
                job_id,
                pipeline.error,
                result_meta=self._resume_meta(pipeline),
            )
            self._drop(job_id)
            raise RuntimeError(pipeline.error)

        err = pipeline.error
        self._drop(job_id)
        if err:
            raise RuntimeError(err)

    def abort(self, job_id: str, message: str = "Upload cancelled") -> None:
        pipeline = self.get(job_id)
        if pipeline is None:
            jobs.request_cancel(job_id, message)
            return
        log.info("upload abort job_id=%s message=%s", job_id, message)
        pipeline.cancel_requested.set()
        pipeline.error = message
        jobs.request_cancel(job_id, message)
        self._drain(pipeline)
        self._offer_sentinel(pipeline, timeout=5)
        if not pipeline.done.wait(timeout=60):
            log.error("upload abort: worker did not stop job_id=%s", job_id)
            if not pipeline.done.is_set():
                pipeline.done.set()
        self._drop(job_id)


upload_pipelines = UploadPipelineHub()
