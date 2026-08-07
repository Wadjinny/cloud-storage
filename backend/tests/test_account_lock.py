from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from services import crypto
from services.jobs import jobs
from storage import auth_context
from storage import users as users_store
from tests.conftest import TEST_PASSPHRASE, TEST_USERNAME


def _scope():
    user = users_store.get_by_username(TEST_USERNAME)
    assert user is not None
    master_key = crypto.derive_master_key(TEST_PASSPHRASE, crypto.b64d(user["kdf_salt"]))
    return auth_context.auth_scope(user["id"], master_key), user["id"]


def test_status_accounts_locked_while_job_running(
    unlocked_client: TestClient,
    data_dir: Path,
    memory_account: dict,
) -> None:
    _, user_id = _scope()
    job = jobs.create("upload", user_id=user_id)
    try:
        status = unlocked_client.get("/api/vault/status")
        assert status.status_code == 200
        assert status.json()["accounts_locked"] is True

        res = unlocked_client.delete(f"/api/accounts/{memory_account['id']}")
        assert res.status_code == 409
        assert "locked" in res.json()["detail"].lower()
    finally:
        jobs.complete(job.id)


def test_mega_connect_blocked_while_job_running(
    unlocked_client: TestClient,
    data_dir: Path,
    memory_account: dict,
) -> None:
    _, user_id = _scope()
    job = jobs.create("download", user_id=user_id)
    try:
        res = unlocked_client.post(
            "/api/auth/mega",
            json={"email": "x@example.com", "password": "secret"},
        )
        assert res.status_code == 409
    finally:
        jobs.fail(job.id, "done")


def test_accounts_unlocked_after_job_done(
    unlocked_client: TestClient,
    data_dir: Path,
    memory_account: dict,
) -> None:
    _, user_id = _scope()
    job = jobs.create("health", user_id=user_id)
    jobs.complete(job.id)
    # Let notify settle; status should allow management again.
    status = unlocked_client.get("/api/vault/status")
    assert status.status_code == 200
    assert status.json()["accounts_locked"] is False
