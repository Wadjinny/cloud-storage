"""Several small files should share SHARD_UPLOAD_CONCURRENCY across jobs."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest
from fastapi.testclient import TestClient

from tests.conftest import TEST_PASSPHRASE, TEST_USERNAME


@pytest.fixture()
def parallel_client(data_dir, real_chunks, memory_account, monkeypatch: pytest.MonkeyPatch):
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
    monkeypatch.setattr(config, "SHARD_UPLOAD_CONCURRENCY", 3)
    monkeypatch.setattr(config, "UPLOAD_QUEUE_DEPTH", 0)
    monkeypatch.setattr(upload_pipeline, "SHARD_UPLOAD_CONCURRENCY", 3)
    monkeypatch.setattr(upload_pipeline, "UPLOAD_QUEUE_DEPTH", 0)
    monkeypatch.setattr(services_vault, "SHARD_UPLOAD_CONCURRENCY", 3)

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


def test_parallel_small_files_share_slot_pool(
    parallel_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import providers.memory as memory_mod
    from services.upload_pipeline import upload_pipelines

    lock = threading.Lock()
    live_uploads = 0
    peak_uploads = 0
    started_jobs: set[str] = set()
    original_upload = memory_mod.MemoryProvider.upload

    def tracking_upload(
        self,
        remote_id: str,
        data: bytes,
        *,
        parent_id: str | None = None,
    ) -> str:
        nonlocal live_uploads, peak_uploads
        with lock:
            live_uploads += 1
            peak_uploads = max(peak_uploads, live_uploads)
        try:
            time.sleep(0.05)
            return original_upload(self, remote_id, data, parent_id=parent_id)
        finally:
            with lock:
                live_uploads -= 1

    monkeypatch.setattr(memory_mod.MemoryProvider, "upload", tracking_upload)

    # Six single-shard files; concurrency=3 so they must overlap without exceeding 3.
    size = 1024
    job_ids: list[str] = []
    for i in range(6):
        start = parallel_client.post(
            "/api/vault/upload/start",
            json={"filename": f"small_{i}.bin", "size": size},
        )
        assert start.status_code == 200, start.text
        job_ids.append(start.json()["job_id"])

    def put_one(job_id: str) -> None:
        with lock:
            started_jobs.add(job_id)
        put = parallel_client.put(
            f"/api/vault/upload/{job_id}",
            content=b"x" * size,
        )
        assert put.status_code == 200, put.text

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(put_one, jid) for jid in job_ids]
        for fut in as_completed(futures):
            fut.result()

    assert len(started_jobs) == 6
    assert peak_uploads <= 3, f"peak concurrent uploads {peak_uploads} > concurrency 3"
    assert peak_uploads >= 2, "expected small files to overlap under shared slot pool"
    assert not upload_pipelines._pipelines
