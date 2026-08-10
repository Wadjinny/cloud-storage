from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from providers.memory import MemoryProvider
from services import crypto
from storage import auth_context
from storage import users as users_store
from storage import vault_index
from tests.conftest import TEST_PASSPHRASE, TEST_USERNAME


def _scope():
    user = users_store.get_by_username(TEST_USERNAME)
    assert user is not None
    master_key = crypto.derive_master_key(TEST_PASSPHRASE, crypto.b64d(user["kdf_salt"]))
    return auth_context.auth_scope(user["id"], master_key)


def test_cannot_delete_account_with_shards(
    unlocked_client: TestClient,
    data_dir: Path,
    memory_account: dict,
) -> None:
    MemoryProvider.reset()
    account_id = memory_account["id"]
    provider = MemoryProvider({})

    with _scope():
        folder = provider.ensure_file_folder("f_keep")
        remote_id = provider.upload("shard_f_keep_0", b"abc", parent_id=folder)
        vault_index.upsert_file(
            None,
            {
                "id": "f_keep",
                "path": "/keep.bin",
                "size": 3,
                "mtime": 1,
                "chunk_size": 64,
                "crypto": {
                    "alg": "AES-256-GCM-CHUNK",
                    "wrapped_file_key": "dGVzdA==",
                },
                "remote_folders": {account_id: folder},
                "shards": [
                    {
                        "i": 0,
                        "account_id": account_id,
                        "remote_id": remote_id,
                        "sha256": "a" * 64,
                        "nonce": "b" * 24,
                    }
                ],
            },
        )

    res = unlocked_client.delete(f"/api/accounts/{account_id}")
    assert res.status_code == 409
    detail = res.json()["detail"]
    assert isinstance(detail, dict)
    assert detail["code"] == "account_in_use"
    assert detail["shard_count"] >= 1
    assert "shard" in detail["message"].lower()

    listed = unlocked_client.get("/api/accounts")
    assert listed.status_code == 200
    assert any(a["id"] == account_id for a in listed.json())


def test_can_delete_unused_account(
    unlocked_client: TestClient,
    data_dir: Path,
    memory_account: dict,
) -> None:
    account_id = memory_account["id"]
    res = unlocked_client.delete(f"/api/accounts/{account_id}")
    assert res.status_code == 200
    assert res.json()["ok"] is True

    listed = unlocked_client.get("/api/accounts")
    assert listed.status_code == 200
    assert not any(a["id"] == account_id for a in listed.json())
