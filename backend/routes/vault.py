from __future__ import annotations

import asyncio
import json
import logging
import queue
import time
from pathlib import PurePosixPath
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field
from starlette.requests import ClientDisconnect

from config import THUMB_CACHE_MAX_AGE, choose_chunk_size
from deps import require_unlocked
from services import vault as vault_service
from services import zip_download as zip_download_service
from services.account_lock import accounts_manageable
from services.download_session import download_sessions
from services.jobs import jobs
from services.thumbnails import load_thumbnail_bytes
from services.upload_pipeline import UploadCancelled, upload_pipelines
from storage import accounts as accounts_store
from storage import activity as activity_store
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


class MkdirBody(BaseModel):
    parent: str = Field(default="/")
    name: str = Field(min_length=1, max_length=255)


class MoveBody(BaseModel):
    src_path: str = Field(min_length=1)
    dest_path: str = Field(min_length=1)
    kind: str | None = Field(default=None, description="'file' or 'dir'")


class ZipItemBody(BaseModel):
    kind: str = Field(description="'file' or 'dir'")
    path: str = Field(min_length=1)
    id: str | None = Field(default=None, description="File id when kind=file")


class ZipDownloadBody(BaseModel):
    items: list[ZipItemBody] = Field(min_length=1)


class DeleteDirBody(BaseModel):
    path: str = Field(min_length=1)


@router.get("/status")
def vault_status(request: Request) -> dict[str, bool | int | str | None]:
    from config import PREVIEW_MAX_BYTES, SHARD_UPLOAD_CONCURRENCY
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
                    "accounts_locked": not accounts_manageable(str(user_id)),
                    "shard_upload_concurrency": SHARD_UPLOAD_CONCURRENCY,
                    "preview_max_bytes": PREVIEW_MAX_BYTES,
                }
    return {
        "unlocked": False,
        "username": None,
        "initialized": users_store.user_count() > 0,
        "file_count": 0,
        "account_count": 0,
        "accounts_locked": False,
        "shard_upload_concurrency": SHARD_UPLOAD_CONCURRENCY,
        "preview_max_bytes": PREVIEW_MAX_BYTES,
    }


@router.post("/lock")
def vault_lock(request: Request) -> dict[str, bool]:
    """Alias for logout (clears the unlocked session)."""
    user_id = request.session.get("user_id")
    request.session.pop("vault_unlocked", None)
    request.session.pop("user_id", None)
    request.session.pop("username", None)
    request.session.pop("master_key", None)
    auth_context.clear_auth()
    if user_id:
        activity_store.record(str(user_id), "lock", "success")
    return {"ok": True}


@router.get("/files")
def list_files(
    request: Request,
    path: str = Query(default="/"),
) -> list[dict]:
    require_unlocked(request)
    return vault_service.list_entries(path)


@router.get("/search")
def search_files(
    request: Request,
    q: str = Query(default="", min_length=0),
    scope: str = Query(default="folder", description="'folder' or 'global'"),
    path: str = Query(default="/"),
    kind: str = Query(default="all", description="'all', 'file', or 'dir'"),
    regex: bool = Query(default=False),
) -> dict[str, Any]:
    """Search entry names under the vault (or current folder tree)."""
    require_unlocked(request)
    try:
        results = vault_service.search_entries(
            q,
            scope=scope,
            path=path,
            kind=kind,
            regex=regex,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "query": q.strip(),
        "scope": scope,
        "path": path,
        "kind": kind,
        "regex": regex,
        "truncated": len(results) >= vault_service.SEARCH_RESULT_LIMIT,
        "results": results,
    }


@router.post("/mkdir")
def mkdir(request: Request, body: MkdirBody) -> dict:
    require_unlocked(request)
    user_id = auth_context.require_user_id()
    try:
        entry = vault_service.mkdir(body.parent, body.name)
    except ValueError as exc:
        activity_store.record(
            user_id,
            "mkdir",
            "error",
            path=body.name,
            detail=str(exc),
            meta={"parent": body.parent},
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    activity_store.record(
        user_id,
        "mkdir",
        "success",
        path=entry.get("path"),
        meta={"parent": body.parent, "name": body.name},
    )
    return entry


@router.post("/move")
def move_entry(request: Request, body: MoveBody) -> dict:
    require_unlocked(request)
    user_id = auth_context.require_user_id()
    try:
        entry = vault_service.move_entry(
            src_path=body.src_path,
            dest_path=body.dest_path,
            kind=body.kind,
        )
    except FileNotFoundError as exc:
        activity_store.record(
            user_id,
            "move",
            "error",
            path=body.src_path,
            detail=str(exc),
            meta={"dest_path": body.dest_path, "kind": body.kind},
        )
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        activity_store.record(
            user_id,
            "move",
            "error",
            path=body.src_path,
            detail=str(exc),
            meta={"dest_path": body.dest_path, "kind": body.kind},
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    activity_store.record(
        user_id,
        "move",
        "success",
        path=body.src_path,
        detail=f"→ {body.dest_path}",
        meta={"dest_path": body.dest_path, "kind": body.kind},
    )
    return entry


@router.post("/dirs/delete")
def delete_dir(request: Request, body: DeleteDirBody) -> dict:
    from storage import folders as folders_store

    master_key = require_unlocked(request)
    user_id = auth_context.require_user_id()
    norm = folders_store.normalize_dir(body.path)
    if norm == "/":
        raise HTTPException(status_code=400, detail="Cannot delete root")
    if (
        folders_store.get_folder(norm) is None
        and not vault_index.list_files_under_prefix(norm)
        and not folders_store.list_child_names(norm)
    ):
        raise HTTPException(status_code=404, detail="Folder not found")

    job_id = jobs.run_in_thread(
        "delete_dir",
        user_id=user_id,
        master_key=master_key,
        work=lambda on_progress: vault_service.delete_directory(body.path, on_progress),
        queued_message="Starting folder delete…",
    )
    return {"job_id": job_id}


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


@router.get("/files/{file_id}/thumbnail")
def get_file_thumbnail(request: Request, file_id: str) -> Response:
    master_key = require_unlocked(request)
    entry = vault_index.find_file(None, file_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="File not found")
    status = entry.get("thumb_status") or "none"
    if status != "ready":
        raise HTTPException(status_code=404, detail="Thumbnail not available")
    try:
        data = load_thumbnail_bytes(entry, master_key)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        log.exception("thumbnail load failed file=%s", file_id)
        raise HTTPException(status_code=502, detail="Thumbnail fetch failed") from exc

    thumb = entry.get("thumb") or {}
    remote_id = thumb.get("remote_id") or ""
    etag = f'"{remote_id}:{thumb.get("bytes") or len(data)}"'
    return Response(
        content=data,
        media_type=thumb.get("mime") or "image/webp",
        headers={
            "Cache-Control": f"private, max-age={THUMB_CACHE_MAX_AGE}",
            "ETag": etag,
        },
    )


@router.get("/jobs/{job_id}")
def get_job(request: Request, job_id: str) -> dict:
    require_unlocked(request)
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job.to_public()


_CANCELABLE_JOB_KINDS = frozenset(
    {"health", "delete_file", "delete_dir", "evacuate"}
)


@router.post("/jobs/{job_id}/cancel")
def cancel_job(request: Request, job_id: str) -> dict[str, bool]:
    """Cancel a running health, delete, or evacuate job."""
    require_unlocked(request)
    user_id = auth_context.require_user_id()
    job = jobs.get(job_id)
    if job is None or job.user_id != user_id:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.kind not in _CANCELABLE_JOB_KINDS:
        raise HTTPException(status_code=400, detail="Job cannot be cancelled")
    if job.status != "running" and not job.cancel_requested:
        return {"ok": True}
    message = {
        "health": "Health check cancelled",
        "delete_file": "Delete cancelled",
        "delete_dir": "Delete cancelled",
        "evacuate": "Evacuate cancelled",
    }.get(job.kind, "Cancelled")
    jobs.request_cancel(job_id, message)
    return {"ok": True}


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


@router.post("/upload/start")
async def upload_start(
    request: Request,
    body: UploadStartBody,
    path: str = Query(default="/"),
) -> dict:
    master_key = require_unlocked(request)
    user_id = auth_context.require_user_id()
    try:
        accounts_store.require_enabled_accounts()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    job = jobs.create("upload", user_id=user_id)
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
    try:
        accounts_store.require_enabled_accounts()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    session = upload_sessions.load(body.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Upload session not found")
    if session.get("status") == "complete":
        raise HTTPException(status_code=409, detail="Upload session already completed")

    resume_from = upload_sessions.resume_from_byte(session)
    size = session.get("size")
    chunk_size = int(session["chunk_size"])
    job = jobs.create("upload", user_id=user_id)
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
    job = jobs.create("download", user_id=user_id)
    job.result_name = filename
    job.result_meta = {"path": entry["path"], "filename": filename, "file_id": id, "kind": "file"}
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
        kind="file",
    )
    return {"job_id": job.id, "filename": filename, "size": size}


@router.post("/download/zip")
async def start_zip_download(request: Request, body: ZipDownloadBody) -> dict:
    """Stream a multi-file/folder selection as a light (level-0) ZIP."""
    master_key = require_unlocked(request)
    user_id = auth_context.require_user_id()
    try:
        zip_filename, members, empty_dirs = zip_download_service.resolve_zip_selection(
            [item.model_dump() for item in body.items]
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    job = jobs.create("download_zip", user_id=user_id)
    job.result_name = zip_filename
    job.result_meta = {
        "path": zip_filename,
        "filename": zip_filename,
        "kind": "zip",
        "file_count": len(members),
    }
    jobs.update(
        job.id,
        phase="queued",
        message="Waiting for zip stream…",
        current=0,
        total=max(1, len(members)),
    )
    download_sessions.start(
        job_id=job.id,
        user_id=user_id,
        master_key=master_key,
        filename=zip_filename,
        size=None,
        kind="zip",
        zip_members=members,
        zip_empty_dirs=empty_dirs,
    )
    return {
        "job_id": job.id,
        "filename": zip_filename,
        "file_count": len(members),
        "size": None,
    }


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

    def _aborted() -> bool:
        return session.cancel_requested.is_set() or jobs.is_cancel_requested(job_id)

    def generate():
        with auth_context.auth_scope(session.user_id, session.master_key):
            try:
                if _aborted():
                    jobs.request_cancel(job_id, "Download cancelled")
                    return
                if session.kind == "zip":
                    chunks = zip_download_service.iter_zip_download(
                        session.master_key,
                        session.zip_members or [],
                        session.zip_empty_dirs or [],
                        on_progress,
                        should_abort=_aborted,
                        user_id=session.user_id,
                    )
                else:
                    if not session.file_id:
                        raise RuntimeError("Download session missing file_id")
                    chunks = vault_service.iter_download(
                        session.master_key,
                        session.file_id,
                        on_progress,
                        should_abort=_aborted,
                    )
                for chunk in chunks:
                    if _aborted():
                        jobs.request_cancel(job_id, "Download cancelled")
                        log.info("download cancelled job_id=%s", job_id)
                        return
                    yield chunk
                if _aborted():
                    jobs.request_cancel(job_id, "Download cancelled")
                    log.info("download cancelled job_id=%s", job_id)
                    return
                jobs.complete(
                    job_id,
                    result_name=session.filename,
                    result_meta={
                        "path": session.filename,
                        "filename": session.filename,
                        "size": session.size,
                        "file_id": session.file_id,
                        "kind": session.kind,
                    },
                )
            except GeneratorExit:
                session.cancel_requested.set()
                jobs.request_cancel(job_id, "Client disconnected")
                raise
            except vault_service.DownloadCancelled:
                jobs.request_cancel(job_id, "Download cancelled")
                log.info("download cancelled job_id=%s", job_id)
                return
            except Exception as exc:
                if _aborted():
                    jobs.request_cancel(job_id, "Download cancelled")
                    log.info("download cancelled job_id=%s", job_id)
                    return
                job = jobs.get(job_id)
                if job is None or job.status == "running":
                    jobs.fail(
                        job_id,
                        str(exc),
                        result_meta={
                            "path": session.filename,
                            "filename": session.filename,
                            "file_id": session.file_id,
                            "kind": session.kind,
                        },
                    )
                raise
            finally:
                download_sessions.finish(job_id)

    media = "application/zip" if session.kind == "zip" else "application/octet-stream"
    headers = {
        "Content-Disposition": f'attachment; filename="{session.filename}"',
        "Cache-Control": "no-store",
    }
    if session.size is not None and session.size >= 0:
        headers["Content-Length"] = str(session.size)

    return StreamingResponse(
        generate(),
        media_type=media,
        headers=headers,
    )


@router.delete("/files/{file_id}")
async def delete_file(request: Request, file_id: str) -> dict:
    master_key = require_unlocked(request)
    user_id = auth_context.require_user_id()
    index = vault_index.load_index()
    if vault_index.find_file(index, file_id) is None:
        raise HTTPException(status_code=404, detail="File not found")

    job_id = jobs.run_in_thread(
        "delete_file",
        user_id=user_id,
        master_key=master_key,
        work=lambda on_progress: vault_service.delete_file(file_id, on_progress),
        queued_message="Starting delete…",
    )
    return {"job_id": job_id}
