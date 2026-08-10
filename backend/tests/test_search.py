from __future__ import annotations

from fastapi.testclient import TestClient

from tests.helpers import upload_bytes


def test_search_requires_unlock(client: TestClient) -> None:
    assert client.get("/api/vault/search", params={"q": "x"}).status_code == 401


def test_search_name_scope_kind_regex(unlocked_client: TestClient) -> None:
    unlocked_client.post("/api/vault/mkdir", json={"parent": "/", "name": "Photos"})
    unlocked_client.post(
        "/api/vault/mkdir", json={"parent": "/Photos", "name": "2021"}
    )
    upload_bytes(unlocked_client, b"a", filename="VID_report.mp4", path="/Photos/2021")
    upload_bytes(unlocked_client, b"b", filename="notes.txt", path="/Photos")
    upload_bytes(unlocked_client, b"c", filename="readme.md", path="/")

    # Case-insensitive substring on name only.
    resp = unlocked_client.get(
        "/api/vault/search",
        params={"q": "vid", "scope": "global", "kind": "all"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    names = [r["name"] for r in body["results"]]
    assert "VID_report.mp4" in names
    assert "notes.txt" not in names

    # Folder scope: recursive under /Photos (not root readme).
    resp = unlocked_client.get(
        "/api/vault/search",
        params={"q": "read", "scope": "folder", "path": "/Photos", "kind": "file"},
    )
    assert resp.status_code == 200
    assert resp.json()["results"] == []

    resp = unlocked_client.get(
        "/api/vault/search",
        params={"q": "note", "scope": "folder", "path": "/Photos", "kind": "file"},
    )
    assert [r["name"] for r in resp.json()["results"]] == ["notes.txt"]

    # Folders only.
    resp = unlocked_client.get(
        "/api/vault/search",
        params={"q": "2021", "scope": "global", "kind": "dir"},
    )
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert len(results) == 1
    assert results[0]["kind"] == "dir"
    assert results[0]["path"] == "/Photos/2021"

    # Regex.
    resp = unlocked_client.get(
        "/api/vault/search",
        params={"q": r"^VID_.*\.mp4$", "scope": "global", "kind": "file", "regex": True},
    )
    assert resp.status_code == 200
    assert [r["name"] for r in resp.json()["results"]] == ["VID_report.mp4"]

    bad = unlocked_client.get(
        "/api/vault/search",
        params={"q": "(", "scope": "global", "regex": True},
    )
    assert bad.status_code == 400
