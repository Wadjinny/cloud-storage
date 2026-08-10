from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from providers.memory import MemoryProvider
from services import crypto
from services import prune as prune_service
from storage import auth_context
from storage import upload_sessions
from storage import users as users_store
from storage import vault_index
from tests.conftest import TEST_PASSPHRASE, TEST_USERNAME


def _scope():
    user = users_store.get_by_username(TEST_USERNAME)
    assert user is not None
    master_key = crypto.derive_master_key(TEST_PASSPHRASE, crypto.b64d(user["kdf_salt"]))
    return auth_context.auth_scope(user["id"], master_key)


def _file_entry(file_id: str, account_id: str, folder_id: str) -> dict:
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
                "remote_id": f"shard_{file_id}_0",
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


def test_prune_deletes_orphans_keeps_indexed(
    data_dir: Path,
    memory_account: dict,
) -> None:
    MemoryProvider.reset()
    account_id = memory_account["id"]
    provider = MemoryProvider({}, account_id=account_id)

    with _scope():
        kept_folder = provider.ensure_file_folder("f_kept")
        orphan_folder = provider.ensure_file_folder("f_orphan")
        vault_index.upsert_file(
            None,
            _file_entry("f_kept", account_id, kept_folder),
        )

        result = prune_service.prune_account(account_id)
        assert result["deleted"] >= 1
        assert result["kept"] >= 1
        assert result["errors"] == 0

        children = {c["name"]: c for c in provider.list_vault_children()}
        assert "f_kept" in children
        assert "f_orphan" not in children
        assert orphan_folder not in MemoryProvider._folders

        assert vault_index.find_file(None, "f_kept") is not None


def test_prune_discards_abandoned_active_and_failed(
    data_dir: Path,
    memory_account: dict,
) -> None:
    """Active sessions with no live upload pipeline are abandoned garbage."""
    MemoryProvider.reset()
    account_id = memory_account["id"]
    provider = MemoryProvider({}, account_id=account_id)

    with _scope():
        abandoned_folder = provider.ensure_file_folder("f_abandoned")
        failed_folder = provider.ensure_file_folder("f_failed")

        now = int(time.time())
        upload_sessions.save(
            {
                "id": "f_abandoned",
                "status": "active",
                "path": "/",
                "filename": "abandoned.bin",
                "size": 10,
                "chunk_size": 64,
                "crypto": {
                    "alg": "AES-256-GCM-CHUNK",
                    "wrapped_file_key": "dGVzdA==",
                },
                "remote_folders": {account_id: abandoned_folder},
                "shards": [],
                "created_at": now,
            }
        )
        upload_sessions.save(
            {
                "id": "f_failed",
                "status": "failed",
                "path": "/",
                "filename": "failed.bin",
                "size": 10,
                "chunk_size": 64,
                "crypto": {
                    "alg": "AES-256-GCM-CHUNK",
                    "wrapped_file_key": "dGVzdA==",
                },
                "remote_folders": {account_id: failed_folder},
                "shards": [],
                "error": "boom",
                "created_at": now,
            }
        )

        report = prune_service.analyze_all()
        assert report["total_stale_sessions"] == 2
        statuses = {
            s["status"]
            for acc in report["accounts"]
            for s in acc["stale_sessions"]
        }
        assert "abandoned" in statuses
        assert "failed" in statuses

        result = prune_service.prune_account(account_id)
        assert result["discarded_sessions"] == 2

        children = {c["name"] for c in provider.list_vault_children()}
        assert "f_abandoned" not in children
        assert "f_failed" not in children
        assert upload_sessions.load("f_abandoned") is None
        assert upload_sessions.load("f_failed") is None


def test_prune_keeps_live_upload_session(
    data_dir: Path,
    memory_account: dict,
) -> None:
    MemoryProvider.reset()
    account_id = memory_account["id"]
    provider = MemoryProvider({}, account_id=account_id)

    with _scope():
        live_folder = provider.ensure_file_folder("f_live")
        now = int(time.time())
        upload_sessions.save(
            {
                "id": "f_live",
                "status": "active",
                "path": "/",
                "filename": "live.bin",
                "size": 10,
                "chunk_size": 64,
                "crypto": {
                    "alg": "AES-256-GCM-CHUNK",
                    "wrapped_file_key": "dGVzdA==",
                },
                "remote_folders": {account_id: live_folder},
                "shards": [],
                "created_at": now,
            }
        )

        with patch(
            "services.prune.upload_pipelines.live_session_ids",
            return_value={"f_live"},
        ):
            report = prune_service.analyze_all()
            assert report["total_stale_sessions"] == 0
            result = prune_service.prune_account(account_id)

        assert result["discarded_sessions"] == 0
        children = {c["name"] for c in provider.list_vault_children()}
        assert "f_live" in children
        assert upload_sessions.load("f_live") is not None


def test_analyze_all_reports_orphans_without_deleting(
    data_dir: Path,
    memory_account: dict,
) -> None:
    MemoryProvider.reset()
    account_id = memory_account["id"]
    provider = MemoryProvider({}, account_id=account_id)

    with _scope():
        kept_folder = provider.ensure_file_folder("f_kept")
        provider.ensure_file_folder("f_orphan")
        vault_index.upsert_file(
            None,
            _file_entry("f_kept", account_id, kept_folder),
        )

        report = prune_service.analyze_all()
        assert report["total_orphans"] >= 1
        row = next(r for r in report["accounts"] if r["account_id"] == account_id)
        names = {o["name"] for o in row["orphans"]}
        assert "f_orphan" in names
        assert "f_kept" not in names

        # Dry-run: orphan still present
        children = {c["name"] for c in provider.list_vault_children()}
        assert "f_orphan" in children
        assert "f_kept" in children


def test_prune_orphan_includes_vault_path(
    data_dir: Path,
    memory_account: dict,
) -> None:
    MemoryProvider.reset()
    account_id = memory_account["id"]
    provider = MemoryProvider({}, account_id=account_id)

    with _scope():
        from storage import folders as folders_store

        folder = folders_store.ensure_folder("/docs")
        provider.ensure_child_folder(folder["id"])
        provider.ensure_file_folder("f_gone")

        report = prune_service.analyze_all()
        by_name = {
            o["name"]: o
            for acc in report["accounts"]
            if acc["account_id"] == account_id
            for o in acc["orphans"]
        }
        assert by_name[folder["id"]]["path"] == "/docs"
        assert "path" not in by_name["f_gone"]


def test_prune_api_analyze_then_apply(
    unlocked_client: TestClient,
    memory_account: dict,
) -> None:
    MemoryProvider.reset()
    account_id = memory_account["id"]
    provider = MemoryProvider({}, account_id=account_id)
    provider.ensure_file_folder("f_api_orphan")

    analyze = unlocked_client.post("/api/accounts/prune/analyze")
    assert analyze.status_code == 200, analyze.text
    analyze_job = _wait_job(unlocked_client, analyze.json()["job_id"])
    assert analyze_job["status"] == "done", analyze_job
    assert analyze_job["result"]["total_orphans"] >= 1
    assert "f_api_orphan" in {
        o["name"]
        for acc in analyze_job["result"]["accounts"]
        for o in acc["orphans"]
    }
    assert "f_api_orphan" in {c["name"] for c in provider.list_vault_children()}

    apply = unlocked_client.post("/api/accounts/prune")
    assert apply.status_code == 200, apply.text
    apply_job = _wait_job(unlocked_client, apply.json()["job_id"])
    assert apply_job["status"] == "done", apply_job
    assert apply_job["result"]["deleted"] >= 1
    assert "f_api_orphan" not in {c["name"] for c in provider.list_vault_children()}
