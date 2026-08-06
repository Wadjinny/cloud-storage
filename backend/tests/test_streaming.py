"""Large-file streaming tests with step logging and hard timeouts.

These exercise the same routes the browser uses:
  POST /upload/start -> PUT /upload/{id} (chunked body)
  GET  /jobs/{id}/events (SSE)
  POST /download -> GET /download/{id}/stream
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from config import choose_chunk_size
from services import vault as services_vault
from tests.helpers import wait_job
from tests.payload import (
    HTTP_FRAME,
    HUGE_FILE_SIZE,
    LARGE_FILE_SIZE,
    iter_pattern_bytes,
    sha256_of_iter,
)

log = logging.getLogger("tests.streaming")

pytestmark = [
    pytest.mark.timeout(180),
]


def _step(msg: str, **fields: object) -> None:
    extra = " ".join(f"{k}={v!r}" for k, v in fields.items())
    log.info("%s %s", msg, extra if extra else "")


def _start_upload(client: TestClient, *, filename: str, size: int, path: str = "/") -> str:
    _step("upload.start", filename=filename, size=size, path=path, chunk_size=choose_chunk_size(size))
    start = client.post(
        "/api/vault/upload/start",
        params={"path": path},
        json={"filename": filename, "size": size},
    )
    assert start.status_code == 200, start.text
    payload = start.json()
    job_id = payload["job_id"]
    assert payload["chunk_size"] == choose_chunk_size(size)
    _step("upload.started", job_id=job_id, chunk_size=payload["chunk_size"])
    return job_id


def _file_id(client: TestClient, *, filename: str, path: str = "/") -> str:
    listing = client.get("/api/vault/files", params={"path": path})
    assert listing.status_code == 200, listing.text
    match = next(e for e in listing.json() if e["kind"] == "file" and e["name"] == filename)
    return match["id"]


def _logged_put(
    client: TestClient,
    job_id: str,
    body: Iterator[bytes] | bytes,
    *,
    content_length: int | None = None,
) -> None:
    t0 = time.monotonic()
    _step("upload.put.begin", job_id=job_id, content_length=content_length)
    headers = {}
    if content_length is not None:
        # Without Content-Length, httpx buffers the whole iterator (OOM on large files).
        headers["content-length"] = str(content_length)
    put = client.put(f"/api/vault/upload/{job_id}", content=body, headers=headers)
    _step(
        "upload.put.end",
        job_id=job_id,
        status=put.status_code,
        elapsed_s=round(time.monotonic() - t0, 2),
        body=put.text[:300],
    )
    assert put.status_code == 200, put.text


def _iter_pattern_and_hash(size: int, frame: int = HTTP_FRAME) -> tuple[Iterator[bytes], list[str]]:
    """Stream body once; hex digest is written to ``digest_out[0]`` after iteration."""
    digest_out: list[str] = []
    hasher = hashlib.sha256()

    def _gen() -> Iterator[bytes]:
        for chunk in iter_pattern_bytes(size, frame=frame):
            hasher.update(chunk)
            yield chunk
        digest_out.append(hasher.hexdigest())

    return _gen(), digest_out


def _assert_chunked_roundtrip(
    client: TestClient,
    *,
    size: int,
    filename: str,
    progress_every: int = 16 * 1024 * 1024,
    job_timeout: float = 120,
    http_download: bool = True,
) -> None:
    chunk_size = choose_chunk_size(size)
    expected_shards = (size + chunk_size - 1) // chunk_size
    body, digest_out = _iter_pattern_and_hash(size)
    _step(
        "roundtrip.begin",
        filename=filename,
        size=size,
        shards=expected_shards,
        chunk_size=chunk_size,
    )

    job_id = _start_upload(client, filename=filename, size=size)
    _logged_put(client, job_id, body, content_length=size)
    assert digest_out, "upload iterator did not finish"
    expected = digest_out[0]

    _step("upload.wait_job", job_id=job_id)
    job = wait_job(client, job_id, timeout=job_timeout)
    _step("upload.job", **job)
    assert job["status"] == "done", job

    file_id = _file_id(client, filename=filename)
    detail = client.get(f"/api/vault/files/{file_id}").json()
    _step("upload.detail", file_id=file_id, size=detail["size"], shards=detail["shard_count"])
    assert detail["size"] == size
    assert detail["shard_count"] == expected_shards

    t0 = time.monotonic()
    if http_download:
        _step("download.start", file_id=file_id, via="http")
        dl = client.post("/api/vault/download", params={"id": file_id})
        assert dl.status_code == 200, dl.text
        dl_job = dl.json()["job_id"]
        _step("download.started", job_id=dl_job, size=dl.json().get("size"))

        with client.stream("GET", f"/api/vault/download/{dl_job}/stream") as resp:
            _step(
                "download.stream.open",
                status=resp.status_code,
                cl=resp.headers.get("content-length"),
            )
            assert resp.status_code == 200
            assert resp.headers.get("content-length") == str(size)
            received = 0

            def _counting() -> Iterator[bytes]:
                nonlocal received
                for chunk in resp.iter_bytes(chunk_size=HTTP_FRAME):
                    received += len(chunk)
                    if received == len(chunk) or received % progress_every < HTTP_FRAME:
                        _step("download.stream.progress", received=received)
                    yield chunk

            digest, total = sha256_of_iter(_counting())

        dl_job_final = wait_job(client, dl_job, timeout=job_timeout)
        _step("download.job", **dl_job_final)
        assert dl_job_final["status"] == "done"
    else:
        # Avoid TestClient response buffering on huge bodies (WSL OOM).
        _step("download.start", file_id=file_id, via="service")
        from services import crypto
        from storage import auth_context
        from storage import users as users_store
        from tests.conftest import TEST_PASSPHRASE, TEST_USERNAME

        user = users_store.get_by_username(TEST_USERNAME)
        assert user is not None
        master_key = crypto.derive_master_key(
            TEST_PASSPHRASE, crypto.b64d(user["kdf_salt"])
        )
        received = 0

        def _counting() -> Iterator[bytes]:
            nonlocal received
            with auth_context.auth_scope(user["id"], master_key):
                for chunk in services_vault.iter_download(master_key, file_id):
                    received += len(chunk)
                    if received == len(chunk) or received % progress_every < HTTP_FRAME:
                        _step("download.stream.progress", received=received)
                    yield chunk

        digest, total = sha256_of_iter(_counting())

    _step(
        "download.stream.end",
        total=total,
        elapsed_s=round(time.monotonic() - t0, 2),
        digest_ok=digest == expected,
    )
    assert total == size
    assert digest == expected
    _step("roundtrip.ok", filename=filename, size=size)


def test_chunked_upload_download_stream_100mb(unlocked_stream_client: TestClient) -> None:
    _assert_chunked_roundtrip(
        unlocked_stream_client,
        size=LARGE_FILE_SIZE,
        filename="big-100mb.bin",
    )


@pytest.mark.timeout(900)
def test_chunked_upload_download_stream_1gb(unlocked_disk_stream_client: TestClient) -> None:
    """1 GiB chunked PUT (HTTP) + streamed download via vault service.

    Download uses the service iterator instead of TestClient.stream: httpx/ASGI
    still tends to buffer huge responses and OOMs on tight WSL hosts.
    """
    _assert_chunked_roundtrip(
        unlocked_disk_stream_client,
        size=HUGE_FILE_SIZE,
        filename="big-1gb.bin",
        progress_every=64 * 1024 * 1024,
        job_timeout=780,
        http_download=False,
    )


def test_upload_sse_events_100mb(unlocked_stream_client: TestClient) -> None:
    """SSE in a side thread (own client); PUT on main thread — mirrors the browser."""
    from main import app

    client = unlocked_stream_client
    filename = "sse-100mb.bin"
    job_id = _start_upload(client, filename=filename, size=LARGE_FILE_SIZE)

    events: list[dict] = []
    sse_error: list[BaseException] = []
    sse_done = threading.Event()

    def _sse_reader() -> None:
        try:
            with TestClient(app) as sse_client:
                sse_client.cookies.update(client.cookies)
                _step("sse.open", job_id=job_id)
                deadline = time.monotonic() + 150
                with sse_client.stream("GET", f"/api/vault/jobs/{job_id}/events") as resp:
                    assert resp.status_code == 200, resp.read()
                    for line in resp.iter_lines():
                        if time.monotonic() > deadline:
                            raise TimeoutError(f"SSE timed out; events={events!r}")
                        if not line or line.startswith(":") or not line.startswith("data: "):
                            continue
                        payload = json.loads(line[6:])
                        events.append(payload)
                        _step(
                            "sse.event",
                            status=payload.get("status"),
                            phase=payload.get("phase"),
                            current=payload.get("current"),
                            total=payload.get("total"),
                            message=payload.get("message"),
                        )
                        if payload.get("status") in ("done", "error"):
                            break
        except BaseException as exc:  # noqa: BLE001
            sse_error.append(exc)
            _step("sse.error", err=repr(exc))
        finally:
            sse_done.set()
            _step("sse.closed", event_count=len(events))

    reader = threading.Thread(target=_sse_reader, name="sse-reader", daemon=True)
    reader.start()
    # Give SSE a moment to subscribe before PUT (browser also races these).
    time.sleep(0.1)

    try:
        _logged_put(client, job_id, iter_pattern_bytes(LARGE_FILE_SIZE, frame=HTTP_FRAME))
    finally:
        if not sse_done.wait(timeout=60):
            _step("sse.reader_stuck")
        reader.join(timeout=5)

    if sse_error:
        raise sse_error[0]
    assert not reader.is_alive(), "SSE reader thread stuck"
    assert events, "expected at least one SSE event"
    assert events[-1]["status"] == "done", events[-1]
    assert any(e.get("phase") == "upload_shard" for e in events)
    currents = [e.get("current", 0) for e in events if isinstance(e.get("current"), int)]
    assert max(currents) > 0
    _step("sse.ok", events=len(events))


def test_cancel_mid_chunked_upload_100mb(unlocked_stream_client: TestClient) -> None:
    from main import app

    client = unlocked_stream_client
    filename = "cancel-100mb.bin"
    job_id = _start_upload(client, filename=filename, size=LARGE_FILE_SIZE)

    reached = threading.Event()
    stop = threading.Event()
    put_status: list[int] = []
    put_error: list[BaseException] = []

    def _gen() -> Iterator[bytes]:
        sent = 0
        for chunk in iter_pattern_bytes(LARGE_FILE_SIZE, frame=512 * 1024):
            if stop.is_set():
                _step("cancel.gen.stop", sent=sent)
                return
            yield chunk
            sent += len(chunk)
            if sent >= 8 * 1024 * 1024:
                _step("cancel.gen.reached_8mb", sent=sent)
                reached.set()
                stop.wait(timeout=30)
                _step("cancel.gen.ending_body", sent=sent)
                return

    def _upload() -> None:
        try:
            with TestClient(app) as upload_client:
                upload_client.cookies.update(client.cookies)
                _step("cancel.put.begin", job_id=job_id)
                t0 = time.monotonic()
                resp = upload_client.put(f"/api/vault/upload/{job_id}", content=_gen())
                put_status.append(resp.status_code)
                _step(
                    "cancel.put.end",
                    status=resp.status_code,
                    elapsed_s=round(time.monotonic() - t0, 2),
                    body=resp.text[:300],
                )
        except BaseException as exc:  # noqa: BLE001
            put_error.append(exc)
            _step("cancel.put.error", err=repr(exc))

    uploader = threading.Thread(target=_upload, name="cancel-upload", daemon=True)
    uploader.start()
    assert reached.wait(timeout=60), "upload never reached 8 MiB cancel point"

    _step("cancel.request", job_id=job_id)
    cancel = client.post(f"/api/vault/upload/{job_id}/cancel")
    _step("cancel.response", status=cancel.status_code, body=cancel.text)
    assert cancel.status_code == 200, cancel.text
    stop.set()

    uploader.join(timeout=45)
    if uploader.is_alive():
        _step("cancel.uploader_stuck", put_status=put_status, put_error=list(map(repr, put_error)))
        pytest.fail("upload thread stuck after cancel")
    if put_error:
        raise put_error[0]

    assert put_status and put_status[0] in (409, 502), put_status
    job = wait_job(client, job_id, timeout=30)
    _step("cancel.job", **job)
    assert job["status"] == "error", job

    listing = client.get("/api/vault/files").json()
    _step("cancel.listing", listing=listing)
    assert listing == []
