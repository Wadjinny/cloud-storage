from __future__ import annotations

import asyncio
import json
import logging
import queue
import threading
import time
from pathlib import PurePosixPath
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from starlette.requests import ClientDisconnect

from config import choose_chunk_size
from deps import require_unlocked
from services import vault as vault_service
from services.download_session import download_sessions
from services.jobs import jobs
from services.upload_pipeline import UploadCancelled, upload_pipelines
from storage import accounts as accounts_store
from storage import auth_context
from storage import upload_sessions
from storage import users as users_store
from storage import vault_index

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/vault", tags=["vault"])


class UploadStartBody(BaseModel):
    filename: str = Field(min_length=1)
    size: int | None = Field(default=None, ge=0)


class UploadResumeBody(BaseModel):
    session_id: str = Field(min_length=1)


@router.get("/status")
def vault_status(request: Request) -> dict[str, bool | int | str | None]:
    from services import crypto

    unlocked = bool(request.session.get("vault_unlocked"))
    user_id = request.session.get("user_id")
    raw_key = request.session.get("master_key")
    if unlocked and user_id and raw_key:
        try:
            master_key = crypto.b64d(raw_key)
        except Exception:
            pass
        else:
            with auth_context.auth_scope(str(user_id), master_key):
                return {
                    "unlocked": True,
                    "username": request.session.get("username"),
                    "initialized": users_store.user_count() > 0,
                    "file_count": vault_index.count_files(),
                    "account_count": accounts_store.count_accounts(),
                }
    return {
        "unlocked": False,
        "username": None,
        "initialized": users_store.user_count() > 0,
        "file_count": 0,
        "account_count": 0,
    }


@router.post("/lock")
def vault_lock(request: Request) -> dict[str, bool]:
    """Alias for logout (clears the unlocked session)."""
    request.session.pop("vault_unlocked", None)
    request.session.pop("user_id", None)
    request.session.pop("username", None)
    request.session.pop("master_key", None)
    auth_context.clear_auth()
    return {"ok": True}


@router.get("/files")
def list_files(
    request: Request,
    path: str = Query(default="/"),
) -> list[dict]:
    require_unlocked(request)
    return vault_service.list_entries(path)


@router.get("/files/{file_id}")
def get_file(
    request: Request,
    file_id: str,
) -> dict:
    require_unlocked(request)
    try:
        return vault_service.get_file_detail(file_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/jobs/{job_id}")
def get_job(request: Request, job_id: str) -> dict:
    require_unlocked(request)
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job.to_public()


@router.get("/jobs/{job_id}/events")
async def job_events(request: Request, job_id: str) -> StreamingResponse:
    require_unlocked(request)
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    q = jobs.subscribe(job_id)

    async def event_stream():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.to_thread(q.get, True, 15.0)
                except queue.Empty:
                    yield ": keepalive\n\n"
                    continue
                if payload is None:
                    break
                yield f"data: {json.dumps(payload)}\n\n"
                if payload.get("status") in ("done", "error"):
                    break
        finally:
            jobs.unsubscribe(job_id, q)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _run_delete_job(job_id: str, file_id: str, user_id: str, master_key: bytes) -> None:
    def on_progress(phase: str, current: int, total: int, message: str) -> None:
        jobs.update(job_id, phase=phase, current=current, total=total, message=message)

    with auth_context.auth_scope(user_id, master_key):
        try:
            vault_service.delete_file(file_id, on_progress)
            jobs.complete(job_id)
        except Exception as exc:
            jobs.fail(job_id, str(exc))


@router.post("/upload/start")
async def upload_start(
    request: Request,
    body: UploadStartBody,
    path: str = Query(default="/"),
) -> dict:
    master_key = require_unlocked(request)
    user_id = auth_context.require_user_id()
    if not accounts_store.load_accounts():
        raise HTTPException(status_code=400, detail="Connect at least one account before uploading")

    job = jobs.create("upload")
    total = max(1, body.size) + 1 if body.size is not None else 1
    chunk_size = choose_chunk_size(body.size)
    jobs.update(job.id, phase="queued", message="Waiting for file stream…", current=0, total=total)
    log.info(
        "upload start job_id=%s path=%s filename=%s size=%s chunk_size=%s",
        job.id,
        path,
        body.filename,
        body.size,
        chunk_size,
    )
    pipeline = upload_pipelines.start(
        job_id=job.id,
        user_id=user_id,
        master_key=master_key,
        path=path,
        filename=body.filename,
        size=body.size,
        chunk_size=chunk_size,
    )
    return {
        "job_id": job.id,
        "chunk_size": chunk_size,
        "session_id": pipeline.session_id,
        "resume_from_byte": 0,
    }


@router.post("/upload/resume")
async def upload_resume(request: Request, body: UploadResumeBody) -> dict:
    """Continue a failed upload session (skips shards already on providers)."""
    master_key = require_unlocked(request)
    user_id = auth_context.require_user_id()
    if not accounts_store.load_accounts():
        raise HTTPException(status_code=400, detail="Connect at least one account before uploading")

    session = upload_sessions.load(body.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Upload session not found")
    if session.get("status") == "complete":
        raise HTTPException(status_code=409, detail="Upload session already completed")

    resume_from = upload_sessions.resume_from_byte(session)
    size = session.get("size")
    chunk_size = int(session["chunk_size"])
    job = jobs.create("upload")
    total = max(1, int(size)) + 1 if size is not None else 1
    jobs.update(
        job.id,
        phase="queued",
        message=f"Resuming from byte {resume_from}…",
        current=resume_from,
        total=total,
    )
    log.info(
        "upload resume job_id=%s session_id=%s resume_from_byte=%s size=%s",
        job.id,
        body.session_id,
        resume_from,
        size,
    )
    pipeline = upload_pipelines.start(
        job_id=job.id,
        user_id=user_id,
        master_key=master_key,
        path=session.get("path") or "/",
        filename=session["filename"],
        size=size,
        chunk_size=chunk_size,
        session_id=body.session_id,
        resume_from_byte=resume_from,
    )
    return {
        "job_id": job.id,
        "chunk_size": chunk_size,
        "session_id": pipeline.session_id,
        "resume_from_byte": resume_from,
        "size": size,
        "filename": session["filename"],
        "path": session.get("path") or "/",
        "done_shards": len(session.get("shards", [])),
    }


@router.post("/upload/{job_id}/cancel")
def cancel_upload(request: Request, job_id: str) -> dict[str, bool]:
    require_unlocked(request)
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Upload job not found")
    if job.kind != "upload":
        raise HTTPException(status_code=400, detail="Not an upload job")
    pipeline = upload_pipelines.get(job_id)
    session_id = pipeline.session_id if pipeline is not None else None
    upload_pipelines.abort(job_id, "Upload cancelled")
    # Worker cancel path deletes remotes+session; if it already exited, finish cleanup.
    if session_id:
        vault_service.discard_upload_session(session_id)
    return {"ok": True}


@router.put("/upload/{job_id}")
async def upload_stream(request: Request, job_id: str) -> dict:
    require_unlocked(request)
    pipeline = upload_pipelines.get(job_id)
    if pipeline is None:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Upload job not found")
        if job.status == "error":
            raise HTTPException(status_code=502, detail=job.error or "Upload failed")
        if job.status == "done":
            return {"ok": True}
        raise HTTPException(status_code=409, detail="Upload pipeline not ready")

    jobs.update(job_id, phase="upload_shard", message="Receiving and uploading…")
    # At most one incomplete shard here; put_chunk back-pressures on live-slot cap.
    buf = bytearray()
    received = 0
    queued_chunks = 0
    offset = pipeline.resume_from_byte
    # Body should cover the file from offset to end (or the whole file if offset=0).
    expected_body = (
        None if pipeline.size is None else max(0, int(pipeline.size) - offset)
    )
    shard_size = pipeline.chunk_size
    t0 = time.monotonic()
    log.info(
        "upload stream open job_id=%s session_id=%s offset=%s expected_body=%s "
        "shard_size=%s",
        job_id,
        pipeline.session_id,
        offset,
        expected_body,
        shard_size,
    )

    async def _queue_piece(piece: bytes) -> None:
        nonlocal queued_chunks
        queued_chunks += 1
        wait_t0 = time.monotonic()
        await asyncio.to_thread(upload_pipelines.put_chunk, job_id, piece)
        wait_s = time.monotonic() - wait_t0
        log.info(
            "upload stream queued job_id=%s chunk=%s bytes=%s received=%s "
            "put_wait=%.2fs",
            job_id,
            queued_chunks,
            len(piece),
            received,
            wait_s,
        )

    try:
        # Do NOT call request.is_disconnected() while streaming: in Starlette it
        # shares the ASGI receive channel with stream() and can steal/drop body
        # chunks (or the final more_body=False), which truncates or wedges the PUT.
        async for block in request.stream():
            if pipeline.cancel_requested.is_set() or jobs.is_cancel_requested(job_id):
                upload_pipelines.abort(job_id, "Upload cancelled")
                raise HTTPException(status_code=409, detail="Upload cancelled")
            view = memoryview(block)
            while len(view) > 0:
                take = min(len(view), shard_size - len(buf))
                buf.extend(view[:take])
                view = view[take:]
                if len(buf) >= shard_size:
                    piece = bytes(buf)
                    buf.clear()
                    received += len(piece)
                    await _queue_piece(piece)
        if buf:
            received += len(buf)
            await _queue_piece(bytes(buf))
            buf.clear()
        log.info(
            "upload stream body done job_id=%s received=%s chunks=%s elapsed=%.2fs",
            job_id,
            received,
            queued_chunks,
            time.monotonic() - t0,
        )
        # Abort/cancel closes the PUT early; prefer "cancelled" over "truncated".
        if pipeline.cancel_requested.is_set() or jobs.is_cancel_requested(job_id):
            upload_pipelines.abort(job_id, "Upload cancelled")
            raise HTTPException(status_code=409, detail="Upload cancelled")
        if expected_body is not None and received != expected_body:
            detail = (
                f"Upload truncated or oversized: received {received} bytes, "
                f"expected {expected_body} (offset {offset})"
            )
            log.error(
                "upload stream %s job_id=%s (client closed early or stalled "
                "while a shard upload was blocking the reader)",
                detail,
                job_id,
            )
            # End the worker without cancel-wipe so the session stays resumable.
            try:
                await asyncio.to_thread(upload_pipelines.finish, job_id)
            except Exception:
                pass
            raise HTTPException(status_code=400, detail=detail)
        await asyncio.to_thread(upload_pipelines.finish, job_id)
    except HTTPException:
        raise
    except ClientDisconnect:
        log.info(
            "upload stream client disconnect job_id=%s received=%s expected_body=%s",
            job_id,
            received,
            expected_body,
        )
        # Keep remote progress for resume (do not treat disconnect as cancel wipe).
        try:
            await asyncio.to_thread(upload_pipelines.finish, job_id)
        except Exception:
            pass
        raise HTTPException(status_code=400, detail="Client disconnected") from None
    except UploadCancelled as exc:
        log.info("upload stream cancelled job_id=%s: %s", job_id, exc)
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        log.exception("upload stream failed job_id=%s", job_id)
        # Prefer persisting resume state over cancel-wipe on unexpected errors.
        try:
            await asyncio.to_thread(upload_pipelines.finish, job_id)
        except Exception:
            upload_pipelines.abort(job_id, str(exc))
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    job = jobs.get(job_id)
    if job is not None and job.status == "error":
        raise HTTPException(status_code=502, detail=job.error or "Upload failed")
    log.info(
        "upload stream complete job_id=%s received=%s elapsed=%.2fs",
        job_id,
        received,
        time.monotonic() - t0,
    )
    return {"ok": True}


@router.post("/download")
async def start_download(request: Request, id: str = Query(...)) -> dict:
    master_key = require_unlocked(request)
    user_id = auth_context.require_user_id()
    index = vault_index.load_index()
    entry = vault_index.find_file(index, id)
    if entry is None:
        raise HTTPException(status_code=404, detail="File not found")

    filename = PurePosixPath(entry["path"]).name
    size = entry.get("size")
    shard_count = len(entry.get("shards", []))
    job = jobs.create("download")
    jobs.update(
        job.id,
        phase="queued",
        message="Waiting for download stream…",
        current=0,
        total=max(1, shard_count),
    )
    download_sessions.start(
        job_id=job.id,
        file_id=id,
        user_id=user_id,
        master_key=master_key,
        filename=filename,
        size=size if isinstance(size, int) else None,
    )
    return {"job_id": job.id, "filename": filename, "size": size}


@router.post("/download/{job_id}/cancel")
def cancel_download(request: Request, job_id: str) -> dict[str, bool]:
    require_unlocked(request)
    job = jobs.get(job_id)
    if job is None and download_sessions.get(job_id) is None:
        raise HTTPException(status_code=404, detail="Download job not found")
    if not download_sessions.abort(job_id, "Download cancelled"):
        # Job may still exist after stream finished claiming the session.
        if job is None:
            raise HTTPException(status_code=404, detail="Download job not found")
        jobs.request_cancel(job_id, "Download cancelled")
    return {"ok": True}


@router.get("/download/{job_id}/stream")
def download_stream(request: Request, job_id: str) -> StreamingResponse:
    require_unlocked(request)
    session = download_sessions.claim_stream(job_id)
    if session is None:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Download job not found")
        if job.status == "error":
            raise HTTPException(status_code=502, detail=job.error or "Download failed")
        raise HTTPException(status_code=409, detail="Download stream already started or expired")

    def on_progress(
        phase: str,
        current: int,
        total: int,
        message: str,
        stats: dict[str, Any] | None = None,
    ) -> None:
        if session.cancel_requested.is_set() or jobs.is_cancel_requested(job_id):
            return
        jobs.update(
            job_id,
            phase=phase,
            current=current,
            total=total,
            message=message,
            stats=stats,
        )

    def generate():
        with auth_context.auth_scope(session.user_id, session.master_key):
            try:
                if session.cancel_requested.is_set():
                    jobs.request_cancel(job_id, "Download cancelled")
                    return
                for chunk in vault_service.iter_download(
                    session.master_key,
                    session.file_id,
                    on_progress,
                    should_abort=lambda: (
                        session.cancel_requested.is_set()
                        or jobs.is_cancel_requested(job_id)
                    ),
                ):
                    if session.cancel_requested.is_set() or jobs.is_cancel_requested(job_id):
                        jobs.request_cancel(job_id, "Download cancelled")
                        log.info("download cancelled job_id=%s", job_id)
                        return
                    yield chunk
                if session.cancel_requested.is_set() or jobs.is_cancel_requested(job_id):
                    jobs.request_cancel(job_id, "Download cancelled")
                    log.info("download cancelled job_id=%s", job_id)
                    return
                jobs.complete(job_id, result_name=session.filename)
            except GeneratorExit:
                session.cancel_requested.set()
                jobs.request_cancel(job_id, "Client disconnected")
                raise
            except vault_service.DownloadCancelled:
                jobs.request_cancel(job_id, "Download cancelled")
                log.info("download cancelled job_id=%s", job_id)
                return
            except Exception as exc:
                if session.cancel_requested.is_set() or jobs.is_cancel_requested(job_id):
                    jobs.request_cancel(job_id, "Download cancelled")
                    log.info("download cancelled job_id=%s", job_id)
                    return
                job = jobs.get(job_id)
                if job is None or job.status == "running":
                    jobs.fail(job_id, str(exc))
                raise
            finally:
                download_sessions.finish(job_id)

    headers = {
        "Content-Disposition": f'attachment; filename="{session.filename}"',
        "Cache-Control": "no-store",
    }
    if session.size is not None and session.size >= 0:
        headers["Content-Length"] = str(session.size)

    return StreamingResponse(
        generate(),
        media_type="application/octet-stream",
        headers=headers,
    )


@router.delete("/files/{file_id}")
async def delete_file(request: Request, file_id: str) -> dict:
    master_key = require_unlocked(request)
    user_id = auth_context.require_user_id()
    index = vault_index.load_index()
    if vault_index.find_file(index, file_id) is None:
        raise HTTPException(status_code=404, detail="File not found")

    job = jobs.create("delete")
    jobs.update(job.id, phase="queued", message="Starting delete…", current=0, total=1)
    thread = threading.Thread(
        target=_run_delete_job,
        args=(job.id, file_id, user_id, master_key),
        daemon=True,
    )
    thread.start()
    return {"job_id": job.id}
