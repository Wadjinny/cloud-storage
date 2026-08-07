from __future__ import annotations

import io
import zipfile

from fastapi.testclient import TestClient

from services import crypto
from services.zip_download import (
    common_parent_dir,
    parent_dir,
    resolve_zip_selection,
    zip_name_from_base,
)
from storage import auth_context
from storage import users as users_store
from tests.conftest import TEST_PASSPHRASE, TEST_USERNAME
from tests.helpers import upload_bytes, wait_job


def test_common_parent_and_zip_name() -> None:
    assert parent_dir("/docs/a.txt") == "/docs"
    assert parent_dir("/a.txt") == "/"
    assert common_parent_dir(["/docs/a", "/docs/b"]) == "/docs"
    assert common_parent_dir(["/docs/pay", "/docs/notes"]) == "/docs"
    assert common_parent_dir(["/a/x", "/b/y"]) == "/"
    assert zip_name_from_base("/") == "vault.zip"
    assert zip_name_from_base("/Bulletins_de_paie") == "Bulletins_de_paie.zip"


def _auth_scope_for_tests():
    user = users_store.get_by_username(TEST_USERNAME)
    assert user is not None
    master_key = crypto.derive_master_key(TEST_PASSPHRASE, crypto.b64d(user["kdf_salt"]))
    return auth_context.auth_scope(user["id"], master_key)


def test_resolve_relative_paths_multi_file(unlocked_client: TestClient) -> None:
    upload_bytes(unlocked_client, b"one", filename="a.txt", path="/Bulletins_de_paie")
    upload_bytes(unlocked_client, b"two", filename="b.txt", path="/Bulletins_de_paie")
    listing = unlocked_client.get(
        "/api/vault/files", params={"path": "/Bulletins_de_paie"}
    ).json()
    items = [
        {"kind": "file", "path": e["path"], "id": e["id"]}
        for e in listing
        if e["kind"] == "file"
    ]
    with _auth_scope_for_tests():
        zip_name, members, empty = resolve_zip_selection(items)
    assert zip_name == "Bulletins_de_paie.zip"
    assert empty == []
    assert {m["arcname"] for m in members} == {"a.txt", "b.txt"}


def test_resolve_single_folder_named_after_folder(unlocked_client: TestClient) -> None:
    upload_bytes(unlocked_client, b"x", filename="nested.txt", path="/Bulletins_de_paie/2024")
    with _auth_scope_for_tests():
        zip_name, members, empty = resolve_zip_selection(
            [{"kind": "dir", "path": "/Bulletins_de_paie"}]
        )
    assert zip_name == "Bulletins_de_paie.zip"
    assert empty == []
    assert members[0]["arcname"] == "2024/nested.txt"


def test_zip_download_stream_roundtrip(unlocked_client: TestClient) -> None:
    upload_bytes(unlocked_client, b"alpha", filename="a.txt", path="/pack")
    upload_bytes(unlocked_client, b"beta", filename="b.txt", path="/pack/sub")
    start = unlocked_client.post(
        "/api/vault/download/zip",
        json={"items": [{"kind": "dir", "path": "/pack"}]},
    )
    assert start.status_code == 200, start.text
    body = start.json()
    assert body["filename"] == "pack.zip"
    assert body["file_count"] == 2
    job_id = body["job_id"]

    stream = unlocked_client.get(f"/api/vault/download/{job_id}/stream")
    assert stream.status_code == 200, stream.text
    assert stream.headers.get("content-type", "").startswith("application/zip")
    job = wait_job(unlocked_client, job_id)
    assert job["status"] == "done", job

    zf = zipfile.ZipFile(io.BytesIO(stream.content))
    names = sorted(zf.namelist())
    assert names == ["a.txt", "sub/b.txt"]
    assert zf.read("a.txt") == b"alpha"
    assert zf.read("sub/b.txt") == b"beta"

    listing = unlocked_client.get("/api/vault/files", params={"path": "/pack"}).json()
    items = [
        {"kind": "file", "path": e["path"], "id": e["id"]}
        for e in listing
        if e["kind"] == "file"
    ]
    # Nested file is under /pack/sub; select folder + root file for multi-item naming.
    start2 = unlocked_client.post(
        "/api/vault/download/zip",
        json={
            "items": [
                *items,
                {"kind": "dir", "path": "/pack/sub"},
            ]
        },
    )
    assert start2.status_code == 200, start2.text
    assert start2.json()["filename"] == "pack.zip"
