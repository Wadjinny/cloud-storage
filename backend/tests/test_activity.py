from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from tests.conftest import TEST_PASSPHRASE, TEST_USERNAME
from tests.helpers import upload_bytes, wait_job


def test_activity_requires_unlock(client: TestClient) -> None:
    assert client.get("/api/activity").status_code == 401


def test_activity_record_list_and_retention(
    data_dir, monkeypatch: pytest.MonkeyPatch
) -> None:
    from storage import activity as activity_store
    from storage import users as users_store

    user = users_store.get_by_username(TEST_USERNAME)
    assert user is not None
    user_id = user["id"]

    monkeypatch.setattr("config.HISTORY_RETENTION_DAYS", 1)
    monkeypatch.setattr("storage.activity.HISTORY_RETENTION_DAYS", 1)

    old = activity_store.record(
        user_id,
        "login",
        "success",
        detail="old",
    )
    from storage.db import connect

    with connect() as conn:
        conn.execute(
            "UPDATE activity_events SET created_at = ? WHERE id = ?",
            (int(time.time()) - 3 * 86400, old["id"]),
        )

    fresh = activity_store.record(user_id, "mkdir", "success", path="/docs")
    events = activity_store.list_events(user_id, limit=50)
    ids = {e["id"] for e in events}
    assert fresh["id"] in ids
    assert old["id"] not in ids


def test_activity_api_login_mkdir_upload(client: TestClient, memory_account) -> None:
    login = client.post(
        "/api/auth/login",
        json={"username": TEST_USERNAME, "passphrase": TEST_PASSPHRASE},
    )
    assert login.status_code == 200

    bad = client.post(
        "/api/auth/login",
        json={"username": TEST_USERNAME, "passphrase": "wrong"},
    )
    assert bad.status_code == 401

    mkdir = client.post("/api/vault/mkdir", json={"parent": "/", "name": "hist"})
    assert mkdir.status_code == 200

    file_id = upload_bytes(client, b"hello-history", filename="hist.txt", path="/hist")
    assert file_id

    delete = client.delete(f"/api/vault/files/{file_id}")
    assert delete.status_code == 200
    job = wait_job(client, delete.json()["job_id"])
    assert job["status"] == "done"

    resp = client.get("/api/activity", params={"limit": 100})
    assert resp.status_code == 200
    body = resp.json()
    assert body["retention_days"] >= 1
    actions = [e["action"] for e in body["events"]]
    assert "login" in actions
    assert "mkdir" in actions
    assert "upload" in actions
    assert "delete_file" in actions

    login_events = [e for e in body["events"] if e["action"] == "login"]
    assert any(e["status"] == "success" for e in login_events)
    assert any(e["status"] == "error" for e in login_events)

    mkdir_events = [e for e in body["events"] if e["action"] == "mkdir"]
    assert any(e["status"] == "success" and e["path"] == "/hist" for e in mkdir_events)

    filtered = client.get("/api/activity", params={"action": "upload", "status": "success"})
    assert filtered.status_code == 200
    assert all(
        e["action"] == "upload" and e["status"] == "success"
        for e in filtered.json()["events"]
    )
