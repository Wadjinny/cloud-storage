"""Upload must keep at most concurrency live plaintext shards, for any file size."""

from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

from tests.conftest import TEST_PASSPHRASE, TEST_USERNAME
from tests.payload import iter_pattern_bytes


@pytest.fixture()
def bounded_client(data_dir, real_chunks, memory_account, monkeypatch: pytest.MonkeyPatch):
    """64 KiB shards + concurrency=2 so over-buffering is obvious."""
    import config
    import services.upload_pipeline as upload_pipeline
    import services.vault as services_vault
    from main import app
    from services.download_session import download_sessions
    from services.jobs import jobs
    from services.upload_pipeline import upload_pipelines

    chunk = 64 * 1024
    monkeypatch.setattr(config, "CHUNK_SIZE", chunk)
    monkeypatch.setattr(config, "CHUNK_SIZE_LARGE", chunk)
    monkeypatch.setattr(config, "CHUNK_SIZE_LARGE_THRESHOLD", 2**62)
    monkeypatch.setattr(config, "SHARD_UPLOAD_CONCURRENCY", 2)
    monkeypatch.setattr(config, "UPLOAD_QUEUE_DEPTH", 0)
    monkeypatch.setattr(upload_pipeline, "SHARD_UPLOAD_CONCURRENCY", 2)
    monkeypatch.setattr(upload_pipeline, "UPLOAD_QUEUE_DEPTH", 0)
    monkeypatch.setattr(services_vault, "SHARD_UPLOAD_CONCURRENCY", 2)

    jobs._jobs.clear()
    jobs._subs.clear()
    upload_pipelines.reset_live_slots()
    download_sessions._sessions.clear()

    with TestClient(app) as client:
        login = client.post(
            "/api/auth/login",
            json={"username": TEST_USERNAME, "passphrase": TEST_PASSPHRASE},
        )
        assert login.status_code == 200, login.text
        yield client


def test_live_plaintext_shards_capped_by_concurrency(bounded_client: TestClient, monkeypatch):
    import providers.memory as memory_mod
    import services.upload_pipeline as up

    lock = threading.Lock()
    live_plain = 0
    peak_plain = 0
    live_uploads = 0
    peak_uploads = 0

    original_put = up.upload_pipelines.put_chunk
    original_upload = memory_mod.MemoryProvider.upload

    def tracking_put(job_id: str, data: bytes) -> None:
        nonlocal live_plain, peak_plain
        original_put(job_id, data)
        with lock:
            live_plain += 1
            peak_plain = max(peak_plain, live_plain)

    def tracking_upload(
        self,
        remote_id: str,
        data: bytes,
        *,
        parent_id: str | None = None,
    ) -> str:
        nonlocal live_plain, peak_plain, live_uploads, peak_uploads
        with lock:
            live_uploads += 1
            peak_uploads = max(peak_uploads, live_uploads)
        try:
            time.sleep(0.02)
            return original_upload(self, remote_id, data, parent_id=parent_id)
        finally:
            with lock:
                live_uploads -= 1
                live_plain -= 1

    monkeypatch.setattr(up.upload_pipelines, "put_chunk", tracking_put)
    monkeypatch.setattr(memory_mod.MemoryProvider, "upload", tracking_upload)

    # 16 shards — enough that unbounded buffering would push peak well above 2.
    size = 16 * 64 * 1024
    start = bounded_client.post(
        "/api/vault/upload/start",
        json={"filename": "bounded.bin", "size": size},
    )
    assert start.status_code == 200, start.text
    job_id = start.json()["job_id"]

    put = bounded_client.put(
        f"/api/vault/upload/{job_id}",
        content=iter_pattern_bytes(size, frame=32 * 1024),
    )
    assert put.status_code == 200, put.text

    assert peak_plain <= 2, f"peak live plaintext shards {peak_plain} > concurrency 2"
    assert peak_uploads <= 2, f"peak concurrent uploads {peak_uploads} > concurrency 2"
