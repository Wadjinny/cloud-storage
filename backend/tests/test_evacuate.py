from __future__ import annotations

import threading
import time
from pathlib import Path

from fastapi.testclient import TestClient

from providers.memory import MemoryProvider
from services import crypto
from services import evacuate as evacuate_service
from services.jobs import raise_if_cancelled
from storage import accounts as accounts_store
from storage import auth_context
from storage import users as users_store
from tests.conftest import TEST_PASSPHRASE, TEST_USERNAME
from tests.helpers import download_bytes, upload_bytes, wait_job


def _scope():
    user = users_store.get_by_username(TEST_USERNAME)
    assert user is not None
    master_key = crypto.derive_master_key(TEST_PASSPHRASE, crypto.b64d(user["kdf_salt"]))
    return auth_context.auth_scope(user["id"], master_key)


def _add_second_memory_account() -> dict:
    with _scope():
        return accounts_store.add_account(
            provider="memory",
            label="memory-dest",
            credentials={},
        )


def test_evacuate_moves_shards_and_deletes_account(
    unlocked_client: TestClient,
    data_dir: Path,
    memory_account: dict,
) -> None:
    MemoryProvider.reset()
    source_id = memory_account["id"]
    # Upload while only the source account exists so shards land there.
    file_id = upload_bytes(unlocked_client, b"evacuate-me", filename="move.bin")
    detail = unlocked_client.get(f"/api/vault/files/{file_id}").json()
    assert all(s["account_id"] == source_id for s in detail["shards"])

    dest = _add_second_memory_account()
    dest_id = dest["id"]

    res = unlocked_client.post(f"/api/accounts/{source_id}/evacuate")
    assert res.status_code == 200, res.text
    job = wait_job(unlocked_client, res.json()["job_id"])
    assert job["status"] == "done", job
    result = job["result"]
    assert result["deleted"] is True
    assert result["moved_shards"] >= 1

    accounts = unlocked_client.get("/api/accounts").json()
    assert not any(a["id"] == source_id for a in accounts)
    assert any(a["id"] == dest_id for a in accounts)

    detail = unlocked_client.get(f"/api/vault/files/{file_id}").json()
    assert all(s["account_id"] == dest_id for s in detail["shards"])
    assert download_bytes(unlocked_client, file_id) == b"evacuate-me"


def test_evacuate_fails_without_destination(
    unlocked_client: TestClient,
    memory_account: dict,
) -> None:
    upload_bytes(unlocked_client, b"solo", filename="solo.bin")
    res = unlocked_client.post(f"/api/accounts/{memory_account['id']}/evacuate")
    assert res.status_code == 200, res.text
    job = wait_job(unlocked_client, res.json()["job_id"])
    assert job["status"] == "error"
    assert "another account" in (job.get("error") or "").lower()

    accounts = unlocked_client.get("/api/accounts").json()
    assert any(a["id"] == memory_account["id"] for a in accounts)


def test_evacuate_fails_when_insufficient_headroom(
    unlocked_client: TestClient,
    memory_account: dict,
) -> None:
    MemoryProvider.reset()
    source_id = memory_account["id"]
    payload = b"x" * 50_000
    upload_bytes(unlocked_client, payload, filename="big.bin")
    # Dest is placeable but only has ~1 KiB under the 80% soft cap.
    with _scope():
        accounts_store.add_account(
            provider="memory",
            label="tiny-dest",
            credentials={
                "used": 6_400_000_000 - 1024,
                "total": 8_000_000_000,
            },
        )

    res = unlocked_client.post(f"/api/accounts/{source_id}/evacuate")
    assert res.status_code == 200, res.text
    job = wait_job(unlocked_client, res.json()["job_id"])
    assert job["status"] == "error"
    err = (job.get("error") or "").lower()
    assert "free space" in err or "placeable" in err

    accounts = unlocked_client.get("/api/accounts").json()
    assert any(a["id"] == source_id for a in accounts)


def test_evacuate_cancel_then_retry(
    unlocked_client: TestClient,
    memory_account: dict,
    monkeypatch,
) -> None:
    MemoryProvider.reset()
    source_id = memory_account["id"]
    upload_bytes(unlocked_client, b"retry", filename="retry.bin")
    _add_second_memory_account()

    original = evacuate_service.evacuate_and_delete
    started = threading.Event()

    def slow_evacuate(account_id: str, on_progress=None):
        started.set()
        for i in range(200):
            raise_if_cancelled("Evacuate cancelled")
            if on_progress is not None:
                on_progress("evacuate", i, 200, f"slow {i}")
            time.sleep(0.02)
        return original(account_id, on_progress)

    monkeypatch.setattr(evacuate_service, "evacuate_and_delete", slow_evacuate)

    res = unlocked_client.post(f"/api/accounts/{source_id}/evacuate")
    assert res.status_code == 200, res.text
    job_id = res.json()["job_id"]
    assert started.wait(timeout=2), "evacuate worker never started"
    cancel = unlocked_client.post(f"/api/vault/jobs/{job_id}/cancel")
    assert cancel.status_code == 200, cancel.text
    body = wait_job(unlocked_client, job_id)
    assert body["cancel_requested"] is True
    assert body["status"] == "error"

    accounts = unlocked_client.get("/api/accounts").json()
    assert any(a["id"] == source_id for a in accounts)

    monkeypatch.setattr(evacuate_service, "evacuate_and_delete", original)
    retry = unlocked_client.post(f"/api/accounts/{source_id}/evacuate")
    assert retry.status_code == 200, retry.text
    job = wait_job(unlocked_client, retry.json()["job_id"])
    assert job["status"] == "done", job
    accounts = unlocked_client.get("/api/accounts").json()
    assert not any(a["id"] == source_id for a in accounts)
