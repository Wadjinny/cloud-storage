from __future__ import annotations

import io
import threading
import zipfile
from typing import Any

import pytest
from fastapi.testclient import TestClient

from services import crypto
from services import vault as vault_service
from services import zip_download as zip_download_mod
from services.zip_download import (
    common_parent_dir,
    iter_zip_download,
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
    assert body["files"] == ["a.txt", "sub/b.txt"]
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


def test_zip_prefetch_downloads_overlap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two small members download concurrently (Barrier would deadlock if sequential)."""
    barrier = threading.Barrier(2, timeout=3.0)

    def fake_iter(
        master_key: bytes,
        file_id: str,
        on_progress: Any = None,
        *,
        should_abort: Any = None,
    ):
        if file_id in ("fa", "fb"):
            barrier.wait()
        yield f"body-{file_id}".encode()

    monkeypatch.setattr(vault_service, "iter_download", fake_iter)
    monkeypatch.setattr(zip_download_mod, "ZIP_PREFETCH_FILES", 2)
    monkeypatch.setattr(zip_download_mod, "ZIP_PREFETCH_MAX_FILE_BYTES", 1024)

    members = [
        {"file_id": "fa", "arcname": "a.txt", "size": 8, "mtime": 1_700_000_000},
        {"file_id": "fb", "arcname": "b.txt", "size": 8, "mtime": 1_700_000_000},
    ]
    seen_prefetch: list[list[str]] = []

    def on_progress(
        phase: str,
        current: int,
        total: int,
        message: str,
        stats: dict[str, Any] | None = None,
    ) -> None:
        if stats and stats.get("zip_prefetching"):
            seen_prefetch.append(list(stats["zip_prefetching"]))

    blob = b"".join(
        iter_zip_download(b"k" * 32, members, on_progress=on_progress)
    )
    zf = zipfile.ZipFile(io.BytesIO(blob))
    assert zf.read("a.txt") == b"body-fa"
    assert zf.read("b.txt") == b"body-fb"
    assert any("b.txt" in names for names in seen_prefetch)


def test_zip_prefetch_skips_large_ahead(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[str] = []
    lock = threading.Lock()

    def fake_iter(
        master_key: bytes,
        file_id: str,
        on_progress: Any = None,
        *,
        should_abort: Any = None,
    ):
        with lock:
            started.append(file_id)
        yield b"x"

    monkeypatch.setattr(vault_service, "iter_download", fake_iter)
    monkeypatch.setattr(zip_download_mod, "ZIP_PREFETCH_FILES", 3)
    monkeypatch.setattr(zip_download_mod, "ZIP_PREFETCH_MAX_FILE_BYTES", 100)

    members = [
        {"file_id": "small", "arcname": "a.txt", "size": 10, "mtime": 1},
        {"file_id": "huge", "arcname": "big.bin", "size": 10_000, "mtime": 1},
        {"file_id": "tail", "arcname": "c.txt", "size": 10, "mtime": 1},
    ]
    prefetch = zip_download_mod._ZipPrefetcher(
        master_key=b"k" * 32,
        user_id=None,
        members=members,
        depth=3,
        max_file_bytes=100,
        should_abort=None,
    )
    try:
        prefetch.ensure(0)
        with prefetch._lock:
            assert 0 not in prefetch._futures  # packing member streams live
            assert 1 not in prefetch._futures  # over size cap
            assert 2 in prefetch._futures
        # Drain so workers finish cleanly.
        assert b"".join(prefetch.take(0)) == b"x"
        assert b"".join(prefetch.take(1)) == b"x"
        assert b"".join(prefetch.take(2)) == b"x"
    finally:
        prefetch.close()
    assert "huge" in started
    assert started.count("huge") == 1


def test_zip_progress_is_byte_weighted(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_iter(
        master_key: bytes,
        file_id: str,
        on_progress: Any = None,
        *,
        should_abort: Any = None,
    ):
        payload = b"x" * (8 if file_id == "fa" else 24)
        if on_progress:
            on_progress(
                "fetch_shard",
                1,
                1,
                "done",
                {"done_bytes": len(payload), "total_bytes": len(payload)},
            )
        yield payload

    monkeypatch.setattr(vault_service, "iter_download", fake_iter)
    monkeypatch.setattr(zip_download_mod, "ZIP_PREFETCH_FILES", 1)
    monkeypatch.setattr(zip_download_mod, "ZIP_PREFETCH_MAX_FILE_BYTES", 1024)

    members = [
        {"file_id": "fa", "arcname": "a.txt", "size": 8, "mtime": 1_700_000_000},
        {"file_id": "fb", "arcname": "b.txt", "size": 24, "mtime": 1_700_000_000},
    ]
    percents: list[int] = []

    def on_progress(
        phase: str,
        current: int,
        total: int,
        message: str,
        stats: dict[str, Any] | None = None,
    ) -> None:
        if total > 0:
            percents.append(min(100, int(round(100 * current / total))))

    blob = b"".join(iter_zip_download(b"k" * 32, members, on_progress=on_progress))
    assert zipfile.ZipFile(io.BytesIO(blob)).namelist() == ["a.txt", "b.txt"]
    assert percents
    assert max(percents) == 100
    # Mid-archive updates should leave 0% (not stuck) before completion.
    assert any(0 < p < 100 for p in percents)
