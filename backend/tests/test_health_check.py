from __future__ import annotations

import time
from pathlib import Path

from fastapi.testclient import TestClient

from providers.memory import MemoryProvider
from services import crypto
from services import health as health_service
from storage import auth_context
from storage import users as users_store
from storage import vault_index
from tests.conftest import TEST_PASSPHRASE, TEST_USERNAME


def _scope():
    user = users_store.get_by_username(TEST_USERNAME)
    assert user is not None
    master_key = crypto.derive_master_key(TEST_PASSPHRASE, crypto.b64d(user["kdf_salt"]))
    return auth_context.auth_scope(user["id"], master_key)


def _file_entry(file_id: str, account_id: str, folder_id: str, remote_id: str) -> dict:
    return {
        "id": file_id,
        "path": f"/{file_id}.bin",
        "size": 3,
        "mtime": 1,
        "chunk_size": 64,
        "crypto": {
            "alg": "AES-256-GCM-CHUNK",
            "wrapped_file_key": "dGVzdA==",
        },
        "remote_folders": {account_id: folder_id},
        "shards": [
            {
                "i": 0,
                "account_id": account_id,
                "remote_id": remote_id,
                "sha256": "a" * 64,
                "nonce": "b" * 24,
            }
        ],
    }


def _wait_job(client: TestClient, job_id: str) -> dict:
    body: dict = {}
    for _ in range(50):
        snap = client.get(f"/api/vault/jobs/{job_id}")
        assert snap.status_code == 200
        body = snap.json()
        if body["status"] in ("done", "error"):
            break
        time.sleep(0.05)
    return body


def test_health_ok_when_shards_present(
    data_dir: Path,
    memory_account: dict,
) -> None:
    MemoryProvider.reset()
    account_id = memory_account["id"]
    provider = MemoryProvider({})

    with _scope():
        folder = provider.ensure_file_folder("f_ok")
        remote_id = provider.upload("shard_f_ok_0", b"abc", parent_id=folder)
        vault_index.upsert_file(None, _file_entry("f_ok", account_id, folder, remote_id))

        result = health_service.check_shards()
        assert result["healthy"] is True
        assert result["issue_count"] == 0
        assert result["ok"] == 1
        assert result["object_count"] == 1


def test_health_reports_missing_shard(
    data_dir: Path,
    memory_account: dict,
) -> None:
    MemoryProvider.reset()
    account_id = memory_account["id"]
    provider = MemoryProvider({})

    with _scope():
        folder = provider.ensure_file_folder("f_bad")
        vault_index.upsert_file(
            None,
            _file_entry("f_bad", account_id, folder, "shard_f_bad_0"),
        )

        result = health_service.check_shards()
        assert result["healthy"] is False
        assert result["issue_count"] == 1
        assert result["missing"][0]["reason"] == "missing"
        assert result["missing"][0]["path"] == "/f_bad.bin"
        assert result["missing"][0]["i"] == 0


def test_list_health_objects_includes_ready_thumb(
    data_dir: Path,
    memory_account: dict,
) -> None:
    account_id = memory_account["id"]
    with _scope():
        entry = _file_entry("f_thumb", account_id, "folder", "shard_f_thumb_0")
        entry["thumb_status"] = "ready"
        entry["thumb"] = {
            "status": "ready",
            "account_id": account_id,
            "remote_id": "thumb_f_thumb",
            "wrapped_key": "dGVzdA==",
            "mime": "image/webp",
            "bytes": 1,
            "error": None,
        }
        vault_index.upsert_file(None, entry)
        checklist = vault_index.list_health_objects()
        kinds = {(row["kind"], row["remote_id"]) for row in checklist["items"]}
        assert checklist["file_count"] == 1
        assert ("shard", "shard_f_thumb_0") in kinds
        assert ("thumb", "thumb_f_thumb") in kinds


def test_health_api_job(
    unlocked_client: TestClient,
    data_dir: Path,
    memory_account: dict,
) -> None:
    MemoryProvider.reset()
    account_id = memory_account["id"]
    provider = MemoryProvider({})

    with _scope():
        folder = provider.ensure_file_folder("f_api")
        remote_id = provider.upload("shard_f_api_0", b"xyz", parent_id=folder)
        vault_index.upsert_file(None, _file_entry("f_api", account_id, folder, remote_id))

    res = unlocked_client.post("/api/accounts/health")
    assert res.status_code == 200
    job_id = res.json()["job_id"]
    body = _wait_job(unlocked_client, job_id)
    assert body["status"] == "done"
    result = body["result"]
    assert result["healthy"] is True
    assert result["ok"] >= 1
