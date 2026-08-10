from __future__ import annotations

from fastapi.testclient import TestClient

from providers.memory import MemoryProvider
from tests.helpers import upload_bytes, wait_job


def test_mkdir_list_empty_folder(unlocked_client: TestClient) -> None:
    resp = unlocked_client.post("/api/vault/mkdir", json={"parent": "/", "name": "docs"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["kind"] == "dir"
    assert body["path"] == "/docs"

    listing = unlocked_client.get("/api/vault/files").json()
    assert any(e["kind"] == "dir" and e["name"] == "docs" for e in listing)

    inside = unlocked_client.get("/api/vault/files", params={"path": "/docs"}).json()
    assert inside == []


def test_mkdir_rejects_duplicate(unlocked_client: TestClient) -> None:
    assert (
        unlocked_client.post("/api/vault/mkdir", json={"parent": "/", "name": "a"}).status_code
        == 200
    )
    again = unlocked_client.post("/api/vault/mkdir", json={"parent": "/", "name": "a"})
    assert again.status_code == 400


def test_rename_file_and_folder(unlocked_client: TestClient) -> None:
    file_id = upload_bytes(unlocked_client, b"hi", filename="a.txt", path="/docs")
    moved = unlocked_client.post(
        "/api/vault/move",
        json={"src_path": "/docs/a.txt", "dest_path": "/docs/b.txt", "kind": "file"},
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["path"] == "/docs/b.txt"
    assert unlocked_client.get(f"/api/vault/files/{file_id}").json()["name"] == "b.txt"

    folder_move = unlocked_client.post(
        "/api/vault/move",
        json={"src_path": "/docs", "dest_path": "/notes", "kind": "dir"},
    )
    assert folder_move.status_code == 200, folder_move.text
    root = unlocked_client.get("/api/vault/files").json()
    assert any(e["name"] == "notes" and e["kind"] == "dir" for e in root)
    notes = unlocked_client.get("/api/vault/files", params={"path": "/notes"}).json()
    assert len(notes) == 1
    assert notes[0]["name"] == "b.txt"


def test_drag_move_into_folder(unlocked_client: TestClient) -> None:
    unlocked_client.post("/api/vault/mkdir", json={"parent": "/", "name": "inbox"})
    upload_bytes(unlocked_client, b"x", filename="x.bin", path="/")
    moved = unlocked_client.post(
        "/api/vault/move",
        json={"src_path": "/x.bin", "dest_path": "/inbox/x.bin", "kind": "file"},
    )
    assert moved.status_code == 200, moved.text
    root = unlocked_client.get("/api/vault/files").json()
    assert not any(e["name"] == "x.bin" for e in root)
    inbox = unlocked_client.get("/api/vault/files", params={"path": "/inbox"}).json()
    assert any(e["name"] == "x.bin" for e in inbox)


def test_delete_folder_recursive(unlocked_client: TestClient) -> None:
    upload_bytes(unlocked_client, b"one", filename="a.txt", path="/wipe")
    upload_bytes(unlocked_client, b"two", filename="b.txt", path="/wipe/nested")
    resp = unlocked_client.post("/api/vault/dirs/delete", json={"path": "/wipe"})
    assert resp.status_code == 200, resp.text
    job = wait_job(unlocked_client, resp.json()["job_id"])
    assert job["status"] == "done", job
    root = unlocked_client.get("/api/vault/files").json()
    assert not any(e["name"] == "wipe" for e in root)


def test_upload_nests_under_vault_dir_ids(unlocked_client: TestClient) -> None:
    file_id = upload_bytes(unlocked_client, b"nested", filename="n.bin", path="/album")
    listing = unlocked_client.get("/api/vault/files").json()
    album = next(e for e in listing if e["kind"] == "dir" and e["name"] == "album")
    children = MemoryProvider({}).list_vault_children()
    root_names = {c["name"] for c in children}
    # Opaque vault-dir id at provider root; per-file folder nested under it.
    assert album["id"] in root_names
    assert file_id not in root_names


def test_delete_folder_uses_fast_remote_tree(unlocked_client: TestClient) -> None:
    upload_bytes(unlocked_client, b"one", filename="a.txt", path="/fastwipe")
    upload_bytes(unlocked_client, b"two", filename="b.txt", path="/fastwipe/nested")
    listing = unlocked_client.get("/api/vault/files").json()
    folder = next(e for e in listing if e["kind"] == "dir" and e["name"] == "fastwipe")
    assert any(
        c["name"] == folder["id"] for c in MemoryProvider({}).list_vault_children()
    )

    resp = unlocked_client.post("/api/vault/dirs/delete", json={"path": "/fastwipe"})
    assert resp.status_code == 200, resp.text
    job = wait_job(unlocked_client, resp.json()["job_id"])
    assert job["status"] == "done", job
    assert (job.get("result") or {}).get("mode") == "fast"

    root = unlocked_client.get("/api/vault/files").json()
    assert not any(e["name"] == "fastwipe" for e in root)
    remaining_names = {c["name"] for c in MemoryProvider({}).list_vault_children()}
    assert folder["id"] not in remaining_names


def test_delete_folder_fast_after_file_move_out(unlocked_client: TestClient) -> None:
    """File move relocates remotes, so source folder delete can use the fast path."""
    upload_bytes(unlocked_client, b"keep", filename="keep.txt", path="/slowwipe")
    upload_bytes(unlocked_client, b"move", filename="move.txt", path="/slowwipe")
    moved = unlocked_client.post(
        "/api/vault/move",
        json={
            "src_path": "/slowwipe/move.txt",
            "dest_path": "/elsewhere/move.txt",
            "kind": "file",
        },
    )
    assert moved.status_code == 200, moved.text

    resp = unlocked_client.post("/api/vault/dirs/delete", json={"path": "/slowwipe"})
    assert resp.status_code == 200, resp.text
    job = wait_job(unlocked_client, resp.json()["job_id"])
    assert job["status"] == "done", job
    assert (job.get("result") or {}).get("mode") == "fast"
    assert (job.get("result") or {}).get("deleted_files") == 1

    root = unlocked_client.get("/api/vault/files").json()
    assert not any(e["name"] == "slowwipe" for e in root)
    elsewhere = unlocked_client.get("/api/vault/files", params={"path": "/elsewhere"}).json()
    assert any(e["name"] == "move.txt" for e in elsewhere)
