from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from providers.memory import MemoryProvider
from services import crypto
from services.capacity import PlacementPlanner
from storage import accounts as accounts_store
from storage import auth_context
from storage import users as users_store
from tests.conftest import TEST_PASSPHRASE, TEST_USERNAME


def _scope():
    user = users_store.get_by_username(TEST_USERNAME)
    assert user is not None
    master_key = crypto.derive_master_key(TEST_PASSPHRASE, crypto.b64d(user["kdf_salt"]))
    return auth_context.auth_scope(user["id"], master_key)


def test_disabled_account_excluded_from_placement(
    data_dir: Path,
    memory_account: dict,
) -> None:
    MemoryProvider.reset()
    with _scope():
        other = accounts_store.add_account(
            provider="memory",
            label="memory-off",
            credentials={},
            enabled=False,
        )
        planner = PlacementPlanner.from_accounts(accounts_store.load_accounts())
        ids = {q.account_id for q in planner.quotas}
        assert memory_account["id"] in ids
        assert other["id"] not in ids
        assert len(accounts_store.load_enabled_accounts()) == 1


def test_patch_enabled_blocked_while_job_running(
    unlocked_client: TestClient,
    memory_account: dict,
) -> None:
    from services.jobs import jobs
    from storage import users as users_store

    user = users_store.get_by_username(TEST_USERNAME)
    assert user is not None
    job = jobs.create("upload", user_id=user["id"])
    try:
        resp = unlocked_client.patch(
            f"/api/accounts/{memory_account['id']}",
            json={"enabled": False},
        )
        assert resp.status_code == 409, resp.text
    finally:
        jobs.complete(job.id)


def test_patch_enabled_and_blocks_upload_when_all_off(
    unlocked_client: TestClient,
    memory_account: dict,
) -> None:
    account_id = memory_account["id"]
    listed = unlocked_client.get("/api/accounts")
    assert listed.status_code == 200
    row = next(a for a in listed.json() if a["id"] == account_id)
    assert row["enabled"] is True

    off = unlocked_client.patch(
        f"/api/accounts/{account_id}",
        json={"enabled": False},
    )
    assert off.status_code == 200, off.text
    assert off.json()["enabled"] is False

    start = unlocked_client.post(
        "/api/vault/upload/start",
        json={"filename": "x.bin", "size": 3},
    )
    assert start.status_code == 400
    assert "Enable at least one account" in start.json()["detail"]

    on = unlocked_client.patch(
        f"/api/accounts/{account_id}",
        json={"enabled": True},
    )
    assert on.status_code == 200
    assert on.json()["enabled"] is True

    start2 = unlocked_client.post(
        "/api/vault/upload/start",
        json={"filename": "y.bin", "size": 3},
    )
    assert start2.status_code == 200, start2.text
