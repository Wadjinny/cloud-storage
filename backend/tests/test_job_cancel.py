from __future__ import annotations

import time

from fastapi.testclient import TestClient

from services import health as health_service
from services import jobs
from services.jobs import raise_if_cancelled
from storage import users as users_store
from tests.conftest import TEST_USERNAME
from tests.helpers import upload_bytes, wait_job


def _user_id() -> str:
    user = users_store.get_by_username(TEST_USERNAME)
    assert user is not None
    return user["id"]


def test_cancel_health_job(unlocked_client: TestClient, monkeypatch) -> None:
    def slow_check(on_progress=None):
        for i in range(200):
            raise_if_cancelled("Health check cancelled")
            if on_progress is not None:
                on_progress("scan", i, 200, f"slow {i}")
            time.sleep(0.02)
        return {
            "file_count": 0,
            "object_count": 0,
            "checked": 0,
            "ok": 0,
            "issue_count": 0,
            "healthy": True,
            "missing": [],
            "files": [],
            "account_errors": [],
        }

    monkeypatch.setattr(health_service, "check_shards", slow_check)

    res = unlocked_client.post("/api/accounts/health")
    assert res.status_code == 200, res.text
    job_id = res.json()["job_id"]

    # Let the worker start so cancel hits a running job.
    time.sleep(0.05)
    cancel = unlocked_client.post(f"/api/vault/jobs/{job_id}/cancel")
    assert cancel.status_code == 200, cancel.text

    body = wait_job(unlocked_client, job_id)
    assert body["cancel_requested"] is True
    assert body["status"] == "error"
    assert "cancel" in (body.get("error") or "").lower()


def test_cancel_delete_file_job(unlocked_client: TestClient, monkeypatch) -> None:
    from services import vault as vault_service

    file_id = upload_bytes(unlocked_client, b"bye", filename="cancel-me.bin")

    original = vault_service.delete_file

    def slow_delete(file_id: str, on_progress=None, **kwargs):
        for i in range(200):
            raise_if_cancelled("Delete cancelled")
            if on_progress is not None:
                on_progress("delete_shard", i, 200, f"slow delete {i}")
            time.sleep(0.02)
        return original(file_id, on_progress=on_progress, **kwargs)

    monkeypatch.setattr(vault_service, "delete_file", slow_delete)

    res = unlocked_client.delete(f"/api/vault/files/{file_id}")
    assert res.status_code == 200, res.text
    job_id = res.json()["job_id"]

    time.sleep(0.05)
    cancel = unlocked_client.post(f"/api/vault/jobs/{job_id}/cancel")
    assert cancel.status_code == 200, cancel.text

    body = wait_job(unlocked_client, job_id)
    assert body["cancel_requested"] is True
    assert body["status"] == "error"
    assert "cancel" in (body.get("error") or "").lower()

    # File should still exist when cancel won the race before remove_file.
    listing = unlocked_client.get("/api/vault/files").json()
    assert any(e.get("id") == file_id for e in listing)


def test_cancel_rejects_non_cancelable_kind(unlocked_client: TestClient) -> None:
    job = jobs.jobs.create("prune", user_id=_user_id())
    bad = unlocked_client.post(f"/api/vault/jobs/{job.id}/cancel")
    assert bad.status_code == 400
