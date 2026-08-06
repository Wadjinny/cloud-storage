from __future__ import annotations

import time

from fastapi.testclient import TestClient


def wait_job(
    client: TestClient,
    job_id: str,
    *,
    timeout: float = 60.0,
    poll: float = 0.05,
) -> dict:
    deadline = time.time() + timeout
    last: dict | None = None
    while time.time() < deadline:
        resp = client.get(f"/api/vault/jobs/{job_id}")
        assert resp.status_code == 200, resp.text
        last = resp.json()
        if last["status"] in ("done", "error"):
            return last
        time.sleep(poll)
    raise AssertionError(f"Job {job_id} did not finish; last={last}")


def upload_bytes(
    client: TestClient,
    data: bytes,
    *,
    filename: str = "file.bin",
    path: str = "/",
) -> str:
    start = client.post(
        "/api/vault/upload/start",
        params={"path": path},
        json={"filename": filename, "size": len(data)},
    )
    assert start.status_code == 200, start.text
    job_id = start.json()["job_id"]
    put = client.put(f"/api/vault/upload/{job_id}", content=data)
    assert put.status_code == 200, put.text
    job = wait_job(client, job_id)
    assert job["status"] == "done", job
    listing = client.get("/api/vault/files", params={"path": path})
    assert listing.status_code == 200, listing.text
    match = next(e for e in listing.json() if e["kind"] == "file" and e["name"] == filename)
    return match["id"]


def download_bytes(client: TestClient, file_id: str) -> bytes:
    start = client.post("/api/vault/download", params={"id": file_id})
    assert start.status_code == 200, start.text
    job_id = start.json()["job_id"]
    stream = client.get(f"/api/vault/download/{job_id}/stream")
    assert stream.status_code == 200, stream.text
    job = wait_job(client, job_id)
    assert job["status"] == "done", job
    return stream.content
