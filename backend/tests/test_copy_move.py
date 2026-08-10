from __future__ import annotations

from fastapi.testclient import TestClient

from providers.memory import MemoryProvider
from storage import auth_context
from storage import vault_index
from tests.conftest import TEST_PASSPHRASE, TEST_USERNAME
from tests.helpers import download_bytes, upload_bytes, wait_job


def _auth_scope():
    from services import crypto
    from storage import users as users_store

    user = users_store.get_by_username(TEST_USERNAME)
    assert user is not None
    master_key = crypto.derive_master_key(TEST_PASSPHRASE, crypto.b64d(user["kdf_salt"]))
    return auth_context.auth_scope(user["id"], master_key)


def test_memory_move_item_and_copy_item() -> None:
    MemoryProvider.reset()
    p = MemoryProvider({})
    root = p.vault_root()
    a = p.ensure_child_folder("dir_a", parent_id=root)
    b = p.ensure_child_folder("dir_b", parent_id=root)
    folder = p.ensure_file_folder("f_test", parent_id=a)
    shard = p.upload("shard_0", b"cipher", parent_id=folder)

    p.move_item(folder, parent_id=b)
    assert folder in MemoryProvider._folders[b]
    assert folder not in MemoryProvider._folders.get(a, set())

    copied = p.copy_item(shard, parent_id=folder, name="shard_copy")
    assert p.download(copied) == b"cipher"
    assert copied != shard


def test_move_file_relocates_and_source_delete_is_fast(
    unlocked_client: TestClient,
) -> None:
    file_id = upload_bytes(unlocked_client, b"move-me", filename="m.bin", path="/src")
    dest_mkdir = unlocked_client.post(
        "/api/vault/mkdir", json={"parent": "/", "name": "dst"}
    )
    assert dest_mkdir.status_code == 200
    dest_folder_id = dest_mkdir.json()["id"]

    with _auth_scope():
        before = vault_index.find_file(None, file_id)
        assert before is not None
        assert before.get("remote_vault_dir_id")

    moved = unlocked_client.post(
        "/api/vault/move",
        json={
            "src_path": "/src/m.bin",
            "dest_path": "/dst/m.bin",
            "kind": "file",
        },
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["path"] == "/dst/m.bin"

    with _auth_scope():
        after = vault_index.find_file(None, file_id)
        assert after is not None
        assert after.get("remote_vault_dir_id") == dest_folder_id

    resp = unlocked_client.post("/api/vault/dirs/delete", json={"path": "/src"})
    assert resp.status_code == 200
    job = wait_job(unlocked_client, resp.json()["job_id"])
    assert job["status"] == "done", job
    assert (job.get("result") or {}).get("mode") == "fast"

    dst = unlocked_client.get("/api/vault/files", params={"path": "/dst"}).json()
    assert any(e["name"] == "m.bin" for e in dst)
    assert download_bytes(unlocked_client, file_id) == b"move-me"


def test_copy_file_clones_ciphertext(unlocked_client: TestClient) -> None:
    src_id = upload_bytes(unlocked_client, b"hello-copy", filename="a.txt", path="/")
    assert (
        unlocked_client.post(
            "/api/vault/mkdir", json={"parent": "/", "name": "copies"}
        ).status_code
        == 200
    )

    with _auth_scope():
        src = vault_index.find_file(None, src_id)
        assert src is not None
        src_shards = list(src.get("shards") or [])
        assert src_shards

    resp = unlocked_client.post(
        "/api/vault/copy",
        json={
            "src_path": "/a.txt",
            "dest_path": "/copies/a.txt",
            "kind": "file",
        },
    )
    assert resp.status_code == 200, resp.text
    job = wait_job(unlocked_client, resp.json()["job_id"])
    assert job["status"] == "done", job
    result = job.get("result") or {}
    assert result.get("path") == "/copies/a.txt"
    copy_id = result.get("id")
    assert copy_id and copy_id != src_id

    assert download_bytes(unlocked_client, copy_id) == b"hello-copy"
    assert download_bytes(unlocked_client, src_id) == b"hello-copy"

    with _auth_scope():
        copied = vault_index.find_file(None, copy_id)
        assert copied is not None
        copy_shards = list(copied.get("shards") or [])
        assert len(copy_shards) == len(src_shards)
        for a, b in zip(src_shards, copy_shards, strict=True):
            assert a["sha256"] == b["sha256"]
            assert a["nonce"] == b["nonce"]
            assert a["remote_id"] != b["remote_id"]

    del_resp = unlocked_client.delete(f"/api/vault/files/{copy_id}")
    assert del_resp.status_code == 200, del_resp.text
    del_job = wait_job(unlocked_client, del_resp.json()["job_id"])
    assert del_job["status"] == "done", del_job

    assert download_bytes(unlocked_client, src_id) == b"hello-copy"


def test_copy_file_conflict(unlocked_client: TestClient) -> None:
    upload_bytes(unlocked_client, b"one", filename="x.txt", path="/")
    upload_bytes(unlocked_client, b"two", filename="y.txt", path="/box")

    resp = unlocked_client.post(
        "/api/vault/copy",
        json={
            "src_path": "/box/y.txt",
            "dest_path": "/x.txt",
            "kind": "file",
        },
    )
    assert resp.status_code == 200
    job = wait_job(unlocked_client, resp.json()["job_id"])
    assert job["status"] == "error", job
    assert "already exists" in (job.get("error") or "").lower()


def test_copy_folder_recursive(unlocked_client: TestClient) -> None:
    upload_bytes(unlocked_client, b"a", filename="a.txt", path="/tree")
    upload_bytes(unlocked_client, b"b", filename="b.txt", path="/tree/nested")

    resp = unlocked_client.post(
        "/api/vault/copy",
        json={
            "src_path": "/tree",
            "dest_path": "/tree-copy",
            "kind": "dir",
        },
    )
    assert resp.status_code == 200, resp.text
    job = wait_job(unlocked_client, resp.json()["job_id"])
    assert job["status"] == "done", job

    root = unlocked_client.get("/api/vault/files").json()
    assert any(e["name"] == "tree" and e["kind"] == "dir" for e in root)
    assert any(e["name"] == "tree-copy" and e["kind"] == "dir" for e in root)

    nested = unlocked_client.get(
        "/api/vault/files", params={"path": "/tree-copy/nested"}
    ).json()
    assert any(e["name"] == "b.txt" for e in nested)

    orig = unlocked_client.get("/api/vault/files", params={"path": "/tree"}).json()
    assert any(e["name"] == "a.txt" for e in orig)
