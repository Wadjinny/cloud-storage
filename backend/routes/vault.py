from __future__ import annotations

import asyncio
import json
import queue
import threading

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from config import CHUNK_SIZE
from deps import require_unlocked
from services import crypto
from services import vault as vault_service
from services.download_session import download_sessions
from services.jobs import jobs
from services.upload_pipeline import UploadCancelled, upload_pipelines
from storage import accounts as accounts_store
from storage import vault_index
from pathlib import PurePosixPath

router = APIRouter(prefix="/api/vault", tags=["vault"])


class UnlockBody(BaseModel):
    passphrase: str = Field(min_length=1)


class UploadStartBody(BaseModel):
    filename: str = Field(min_length=1)
    size: int | None = Field(default=None, ge=0)


@router.get("/status")
def vault_status(request: Request) -> dict[str, bool | int]:
    index = vault_index.load_index()
    return {
        "unlocked": bool(request.session.get("vault_unlocked")),
        "initialized": bool(index.get("kdf_salt")),
        "file_count": len(index.get("files", [])),
        "account_count": len(accounts_store.load_accounts()),
    }


@router.post("/unlock")
def vault_unlock(request: Request, body: UnlockBody) -> dict[str, bool]:
    try:
        master_key = vault_service.unlock(body.passphrase)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    request.session["vault_unlocked"] = True
    request.session["master_key"] = crypto.b64e(master_key)
    return {"ok": True}


@router.post("/lock")
def vault_lock(request: Request) -> dict[str, bool]:
    request.session.pop("vault_unlocked", None)
    request.session.pop("master_key", None)
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


def _run_delete_job(job_id: str, file_id: str) -> None:
    def on_progress(phase: str, current: int, total: int, message: str) -> None:
        jobs.update(job_id, phase=phase, current=current, total=total, message=message)

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
    if not accounts_store.load_accounts():
        raise HTTPException(status_code=400, detail="Connect at least one account before uploading")

    job = jobs.create("upload")
    total = max(1, body.size) + 1 if body.size is not None else 1
    jobs.update(job.id, phase="queued", message="Waiting for file stream…", current=0, total=total)
    upload_pipelines.start(
        job_id=job.id,
        master_key=master_key,
        path=path,
        filename=body.filename,
        size=body.size,
    )
    return {"job_id": job.id}


@router.post("/upload/{job_id}/cancel")
def cancel_upload(request: Request, job_id: str) -> dict[str, bool]:
    require_unlocked(request)
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Upload job not found")
    if job.kind != "upload":
        raise HTTPException(status_code=400, detail="Not an upload job")
    upload_pipelines.abort(job_id, "Upload cancelled")
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
    buf = bytearray()
    try:
        async for block in request.stream():
            if pipeline.cancel_requested.is_set() or jobs.is_cancel_requested(job_id):
                upload_pipelines.abort(job_id, "Upload cancelled")
                raise HTTPException(status_code=409, detail="Upload cancelled")
            if await request.is_disconnected():
                upload_pipelines.abort(job_id, "Client disconnected")
                raise HTTPException(status_code=400, detail="Client disconnected")
            buf.extend(block)
            while len(buf) >= CHUNK_SIZE:
                piece = bytes(buf[:CHUNK_SIZE])
                del buf[:CHUNK_SIZE]
                await asyncio.to_thread(upload_pipelines.put_chunk, job_id, piece)
        if buf:
            await asyncio.to_thread(upload_pipelines.put_chunk, job_id, bytes(buf))
        await asyncio.to_thread(upload_pipelines.finish, job_id)
    except HTTPException:
        raise
    except UploadCancelled as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        upload_pipelines.abort(job_id, str(exc))
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    job = jobs.get(job_id)
    if job is not None and job.status == "error":
        raise HTTPException(status_code=502, detail=job.error or "Upload failed")
    return {"ok": True}


@router.post("/download")
async def start_download(request: Request, id: str = Query(...)) -> dict:
    master_key = require_unlocked(request)
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
        master_key=master_key,
        filename=filename,
        size=size if isinstance(size, int) else None,
    )
    return {"job_id": job.id, "filename": filename, "size": size}


@router.post("/download/{job_id}/cancel")
def cancel_download(request: Request, job_id: str) -> dict[str, bool]:
    require_unlocked(request)
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Download job not found")
    download_sessions.abort(job_id, "Download cancelled")
    return {"ok": True}


@router.get("/download/{job_id}/stream")
def download_stream(request: Request, job_id: str) -> StreamingResponse:
    require_unlocked(request)
    session = download_sessions.take(job_id)
    if session is None:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Download job not found")
        if job.status == "error":
            raise HTTPException(status_code=502, detail=job.error or "Download failed")
        raise HTTPException(status_code=409, detail="Download stream already started or expired")

    def on_progress(phase: str, current: int, total: int, message: str) -> None:
        jobs.update(job_id, phase=phase, current=current, total=total, message=message)

    def generate():
        try:
            for chunk in vault_service.iter_download(
                session.master_key,
                session.file_id,
                on_progress,
            ):
                yield chunk
            jobs.complete(job_id, result_name=session.filename)
        except GeneratorExit:
            jobs.fail(job_id, "Client disconnected")
            raise
        except Exception as exc:
            jobs.fail(job_id, str(exc))
            raise

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
    require_unlocked(request)
    index = vault_index.load_index()
    if vault_index.find_file(index, file_id) is None:
        raise HTTPException(status_code=404, detail="File not found")

    job = jobs.create("delete")
    jobs.update(job.id, phase="queued", message="Starting delete…", current=0, total=1)
    thread = threading.Thread(
        target=_run_delete_job,
        args=(job.id, file_id),
        daemon=True,
    )
    thread.start()
    return {"job_id": job.id}
