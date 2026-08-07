from __future__ import annotations

from fastapi.testclient import TestClient

from providers.memory import MemoryProvider
from tests.conftest import TEST_CHUNK_SIZE, TEST_PASSPHRASE, TEST_USERNAME
from tests.helpers import download_bytes, upload_bytes, wait_job


def test_health(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_locked_endpoints_return_401(client: TestClient) -> None:
    assert client.get("/api/vault/files").status_code == 401
    assert client.post("/api/vault/upload/start", json={"filename": "x"}).status_code == 401
    assert client.post("/api/vault/download", params={"id": "f_x"}).status_code == 401
    assert client.delete("/api/vault/files/f_x").status_code == 401
    assert client.get("/api/accounts").status_code == 401


def test_login_logout_status(client: TestClient) -> None:
    status = client.get("/api/vault/status")
    assert status.status_code == 200
    body = status.json()
    assert body["unlocked"] is False
    assert body["initialized"] is True
    assert body["account_count"] == 0

    ok = client.post(
        "/api/auth/login",
        json={"username": TEST_USERNAME, "passphrase": TEST_PASSPHRASE},
    )
    assert ok.status_code == 200
    assert ok.json()["username"] == TEST_USERNAME

    locked = client.post("/api/auth/logout")
    assert locked.status_code == 200
    assert client.get("/api/vault/files").status_code == 401

    bad = client.post(
        "/api/auth/login",
        json={"username": TEST_USERNAME, "passphrase": "wrong"},
    )
    assert bad.status_code == 401

    again = client.post(
        "/api/auth/login",
        json={"username": TEST_USERNAME, "passphrase": TEST_PASSPHRASE},
    )
    assert again.status_code == 200
    status = client.get("/api/vault/status").json()
    assert status["unlocked"] is True
    assert status["initialized"] is True
    assert status["username"] == TEST_USERNAME
    assert status["account_count"] == 1


def test_upload_requires_account(data_dir, small_chunks) -> None:
    from main import app
    from storage import accounts as accounts_store
    from storage import auth_context
    from services import crypto
    from storage import users as users_store

    user = users_store.get_by_username(TEST_USERNAME)
    assert user is not None
    master_key = crypto.derive_master_key(TEST_PASSPHRASE, crypto.b64d(user["kdf_salt"]))
    with auth_context.auth_scope(user["id"], master_key):
        for account in accounts_store.load_accounts():
            accounts_store.delete_account(account["id"])

    with TestClient(app) as client:
        client.post(
            "/api/auth/login",
            json={"username": TEST_USERNAME, "passphrase": TEST_PASSPHRASE},
        )
        resp = client.post(
            "/api/vault/upload/start",
            json={"filename": "x.bin", "size": 1},
        )
        assert resp.status_code == 400
        assert "account" in resp.json()["detail"].lower()


def test_list_empty_then_nested(unlocked_client: TestClient) -> None:
    listing = unlocked_client.get("/api/vault/files")
    assert listing.status_code == 200
    assert listing.json() == []

    file_id = upload_bytes(
        unlocked_client,
        b"nested-data",
        filename="note.txt",
        path="/docs",
    )
    root = unlocked_client.get("/api/vault/files").json()
    assert any(e["kind"] == "dir" and e["name"] == "docs" for e in root)

    docs = unlocked_client.get("/api/vault/files", params={"path": "/docs"}).json()
    assert len(docs) == 1
    assert docs[0]["id"] == file_id
    assert docs[0]["name"] == "note.txt"
    assert docs[0]["size"] == len(b"nested-data")


def test_upload_download_small_roundtrip(unlocked_client: TestClient) -> None:
    payload = b"hello from vault tests"
    file_id = upload_bytes(unlocked_client, payload, filename="hello.txt")
    assert download_bytes(unlocked_client, file_id) == payload

    detail = unlocked_client.get(f"/api/vault/files/{file_id}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["name"] == "hello.txt"
    assert body["size"] == len(payload)
    assert body["shard_count"] == 1


def test_upload_download_empty_file(unlocked_client: TestClient) -> None:
    file_id = upload_bytes(unlocked_client, b"", filename="empty.bin")
    assert download_bytes(unlocked_client, file_id) == b""


def test_upload_download_multi_chunk(unlocked_client: TestClient) -> None:
    # 3 full chunks + remainder
    payload = bytes((i % 256) for i in range(TEST_CHUNK_SIZE * 3 + 17))
    file_id = upload_bytes(unlocked_client, payload, filename="big.bin")
    detail = unlocked_client.get(f"/api/vault/files/{file_id}").json()
    assert detail["shard_count"] == 4
    assert download_bytes(unlocked_client, file_id) == payload


def test_delete_removes_file_and_shards(unlocked_client: TestClient) -> None:
    payload = b"delete-me" * 20
    file_id = upload_bytes(unlocked_client, payload, filename="gone.txt")
    detail = unlocked_client.get(f"/api/vault/files/{file_id}").json()
    remote_ids = [s["remote_id"] for s in detail["shards"]]
    assert remote_ids
    for rid in remote_ids:
        assert MemoryProvider._store.get(rid) is not None
    assert f"dir_{file_id}" in MemoryProvider._folders

    resp = unlocked_client.delete(f"/api/vault/files/{file_id}")
    assert resp.status_code == 200
    job = wait_job(unlocked_client, resp.json()["job_id"])
    assert job["status"] == "done", job

    assert unlocked_client.get(f"/api/vault/files/{file_id}").status_code == 404
    listing = unlocked_client.get("/api/vault/files").json()
    assert listing == []
    for rid in remote_ids:
        assert MemoryProvider._store.get(rid) is None
    assert f"dir_{file_id}" not in MemoryProvider._folders


def test_cancel_upload(unlocked_client: TestClient) -> None:
    # Large enough that cancel can race the pipeline before finish.
    payload = bytes(range(256)) * (TEST_CHUNK_SIZE * 8)
    start = unlocked_client.post(
        "/api/vault/upload/start",
        json={"filename": "slow.bin", "size": len(payload)},
    )
    assert start.status_code == 200
    job_id = start.json()["job_id"]

    cancel = unlocked_client.post(f"/api/vault/upload/{job_id}/cancel")
    assert cancel.status_code == 200

    put = unlocked_client.put(f"/api/vault/upload/{job_id}", content=payload)
    assert put.status_code in (409, 502)

    job = wait_job(unlocked_client, job_id)
    assert job["status"] == "error"
    listing = unlocked_client.get("/api/vault/files").json()
    assert listing == []
    # Per-file folder delete should leave no orphan shards in the memory provider.
    assert MemoryProvider._store == {}
    assert MemoryProvider._folders == {MemoryProvider.VAULT_ROOT_ID: set()}


def test_accounts_list_and_storage(unlocked_client: TestClient) -> None:
    accounts = unlocked_client.get("/api/accounts")
    assert accounts.status_code == 200
    rows = accounts.json()
    assert len(rows) == 1
    assert rows[0]["provider"] == "memory"

    storage = unlocked_client.get("/api/accounts/storage")
    assert storage.status_code == 200
    body = storage.json()
    assert body["combined"]["account_count"] == 1
    assert body["accounts"][0]["total"] is not None
