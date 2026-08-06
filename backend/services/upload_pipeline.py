from __future__ import annotations

import queue
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from config import UPLOAD_QUEUE_DEPTH
from services import vault as vault_service
from services.jobs import jobs

_SENTINEL = object()


class UploadCancelled(RuntimeError):
    pass


@dataclass
class UploadPipeline:
    job_id: str
    master_key: bytes
    path: str
    filename: str
    size: int | None
    chunk_queue: queue.Queue[Any] = field(
        default_factory=lambda: queue.Queue(maxsize=max(1, UPLOAD_QUEUE_DEPTH))
    )
    done: threading.Event = field(default_factory=threading.Event)
    cancel_requested: threading.Event = field(default_factory=threading.Event)
    error: str | None = None


class UploadPipelineHub:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pipelines: dict[str, UploadPipeline] = {}

    def start(
        self,
        *,
        job_id: str,
        master_key: bytes,
        path: str,
        filename: str,
        size: int | None,
    ) -> UploadPipeline:
        pipeline = UploadPipeline(
            job_id=job_id,
            master_key=master_key,
            path=path,
            filename=filename,
            size=size,
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
        return pipeline

    def get(self, job_id: str) -> UploadPipeline | None:
        with self._lock:
            return self._pipelines.get(job_id)

    def _drop(self, job_id: str) -> None:
        with self._lock:
            self._pipelines.pop(job_id, None)

    def _chunk_iter(self, pipeline: UploadPipeline) -> Iterator[bytes]:
        saw = False
        while True:
            if pipeline.cancel_requested.is_set():
                raise UploadCancelled("Upload cancelled")
            item = pipeline.chunk_queue.get()
            if pipeline.cancel_requested.is_set():
                raise UploadCancelled("Upload cancelled")
            if item is _SENTINEL:
                if not saw:
                    yield b""
                break
            saw = True
            yield item

    def _worker(self, pipeline: UploadPipeline) -> None:
        def on_progress(phase: str, current: int, total: int, message: str) -> None:
            if pipeline.cancel_requested.is_set():
                return
            jobs.update(
                pipeline.job_id,
                phase=phase,
                current=current,
                total=total,
                message=message,
            )

        try:
            entry = vault_service.upload_file(
                pipeline.master_key,
                pipeline.path,
                pipeline.filename,
                self._chunk_iter(pipeline),
                size=pipeline.size,
                on_progress=on_progress,
                should_abort=pipeline.cancel_requested.is_set,
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
        except UploadCancelled as exc:
            pipeline.error = str(exc)
            jobs.request_cancel(pipeline.job_id, str(exc))
            self._drain(pipeline)
        except Exception as exc:
            pipeline.error = str(exc)
            job = jobs.get(pipeline.job_id)
            if job is None or job.status == "running":
                jobs.fail(pipeline.job_id, str(exc))
            self._drain(pipeline)
        finally:
            pipeline.done.set()

    def _drain(self, pipeline: UploadPipeline) -> None:
        while True:
            try:
                pipeline.chunk_queue.get_nowait()
            except queue.Empty:
                break

    def put_chunk(self, job_id: str, data: bytes) -> None:
        pipeline = self.get(job_id)
        if pipeline is None:
            raise RuntimeError("Upload pipeline not found")
        if pipeline.cancel_requested.is_set() or pipeline.done.is_set():
            raise UploadCancelled(pipeline.error or "Upload cancelled")
        pipeline.chunk_queue.put(data)

    def finish(self, job_id: str) -> None:
        pipeline = self.get(job_id)
        if pipeline is None:
            job = jobs.get(job_id)
            if job is not None and job.status == "error":
                raise RuntimeError(job.error or "Upload failed")
            return
        if pipeline.cancel_requested.is_set():
            pipeline.done.wait(timeout=120)
            self._drop(job_id)
            raise UploadCancelled(pipeline.error or "Upload cancelled")
        pipeline.chunk_queue.put(_SENTINEL)
        pipeline.done.wait()
        err = pipeline.error
        self._drop(job_id)
        if err:
            raise RuntimeError(err)

    def abort(self, job_id: str, message: str = "Upload cancelled") -> None:
        pipeline = self.get(job_id)
        if pipeline is None:
            jobs.request_cancel(job_id, message)
            return
        pipeline.cancel_requested.set()
        pipeline.error = message
        jobs.request_cancel(job_id, message)
        self._drain(pipeline)
        try:
            pipeline.chunk_queue.put_nowait(_SENTINEL)
        except queue.Full:
            pass
        pipeline.done.wait(timeout=120)
        self._drop(job_id)


upload_pipelines = UploadPipelineHub()
