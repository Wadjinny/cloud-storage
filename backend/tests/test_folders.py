from __future__ import annotations

from fastapi.testclient import TestClient

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
