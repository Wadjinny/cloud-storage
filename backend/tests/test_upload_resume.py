"""Resume-on-failure: completed shards are skipped on a second attempt."""

from __future__ import annotations

from fastapi.testclient import TestClient

from providers.memory import MemoryProvider
from storage import upload_sessions
from tests.conftest import TEST_CHUNK_SIZE
from tests.helpers import download_bytes, upload_bytes, wait_job


def test_resume_skips_uploaded_shards(unlocked_client: TestClient, monkeypatch) -> None:
    from providers import memory as memory_mod

    payload = bytes((i % 256) for i in range(TEST_CHUNK_SIZE * 4 + 11))
    original_upload = memory_mod.MemoryProvider.upload
    fail_once = {"remaining": 1}

    def flaky_upload(
        self,
        remote_id: str,
        data: bytes,
        *,
        parent_id: str | None = None,
    ) -> str:
        if fail_once["remaining"] > 0 and remote_id.endswith("_2"):
            fail_once["remaining"] -= 1
            raise RuntimeError("simulated provider blip")
        return original_upload(self, remote_id, data, parent_id=parent_id)

    monkeypatch.setattr(memory_mod.MemoryProvider, "upload", flaky_upload)

    start = unlocked_client.post(
        "/api/vault/upload/start",
        json={"filename": "resume.bin", "size": len(payload)},
    )
    assert start.status_code == 200, start.text
    job_id = start.json()["job_id"]
    session_id = start.json()["session_id"]

    put = unlocked_client.put(f"/api/vault/upload/{job_id}", content=payload)
    assert put.status_code in (400, 502), put.text
    job = wait_job(unlocked_client, job_id)
    assert job["status"] == "error"
    resume = (job.get("result") or {}).get("resume")
    assert resume is not None, job
    assert resume["session_id"] == session_id
    assert resume["done_shards"] >= 2

    session = upload_sessions.load(session_id)
    assert session is not None
    done_before = {int(s["i"]) for s in session["shards"]}
    assert done_before

    # Second attempt: provider works; already-uploaded shards must not be re-sent.
    uploaded_names: list[str] = []

    def tracking_upload(
        self,
        remote_id: str,
        data: bytes,
        *,
        parent_id: str | None = None,
    ) -> str:
        uploaded_names.append(remote_id)
        return original_upload(self, remote_id, data, parent_id=parent_id)

    monkeypatch.setattr(memory_mod.MemoryProvider, "upload", tracking_upload)

    resume_resp = unlocked_client.post(
        "/api/vault/upload/resume",
        json={"session_id": session_id},
    )
    assert resume_resp.status_code == 200, resume_resp.text
    body = resume_resp.json()
    job2 = body["job_id"]
    offset = int(body["resume_from_byte"])
    assert offset % TEST_CHUNK_SIZE == 0

    put2 = unlocked_client.put(
        f"/api/vault/upload/{job2}",
        content=payload[offset:],
    )
    assert put2.status_code == 200, put2.text
    job2_final = wait_job(unlocked_client, job2)
    assert job2_final["status"] == "done", job2_final

    # No re-upload of shards that were already done before resume.
    for i in done_before:
        assert not any(name.endswith(f"_{i}") for name in uploaded_names), (
            uploaded_names,
            done_before,
        )

    file_id = job2_final["result"]["id"]
    assert download_bytes(unlocked_client, file_id) == payload
    assert upload_sessions.load(session_id) is None


def test_cancel_discards_resume_session(unlocked_client: TestClient) -> None:
    payload = bytes(range(256)) * (TEST_CHUNK_SIZE * 4)
    start = unlocked_client.post(
        "/api/vault/upload/start",
        json={"filename": "cancel-resume.bin", "size": len(payload)},
    )
    assert start.status_code == 200
    job_id = start.json()["job_id"]
    session_id = start.json()["session_id"]

    # Cancel before/during stream.
    cancel = unlocked_client.post(f"/api/vault/upload/{job_id}/cancel")
    assert cancel.status_code == 200
    put = unlocked_client.put(f"/api/vault/upload/{job_id}", content=payload)
    assert put.status_code in (409, 502)

    assert upload_sessions.load(session_id) is None
    assert MemoryProvider._store == {}
    # Smoke: a fresh upload still works after cancel.
    file_id = upload_bytes(unlocked_client, b"ok", filename="after-cancel.bin")
    assert unlocked_client.get(f"/api/vault/files/{file_id}").status_code == 200

