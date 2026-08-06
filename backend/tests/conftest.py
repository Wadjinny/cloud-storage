from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient

load_dotenv(Path(__file__).resolve().parents[1] / ".env")
os.environ.setdefault("SESSION_SECRET", "test-session-secret")

TEST_USERNAME = "test-user"
TEST_PASSPHRASE = "test-vault-passphrase"
TEST_CHUNK_SIZE = 64
REAL_CHUNK_SIZE = 16 * 1024 * 1024
LARGE_CHUNK_SIZE = 16 * 1024 * 1024
LARGE_CHUNK_THRESHOLD = 100 * 1024 * 1024


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "data"
    root.mkdir()
    db_path = root / "app.db"

    monkeypatch.setenv("DATA_DIR", str(root))
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("INIT_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("INIT_PASSPHRASE", TEST_PASSPHRASE)
    monkeypatch.setattr("config.DATA_DIR", root)
    monkeypatch.setattr("config.DB_PATH", db_path)
    monkeypatch.setattr("config.INIT_USERNAME", TEST_USERNAME)
    monkeypatch.setattr("config.INIT_PASSPHRASE", TEST_PASSPHRASE)
    monkeypatch.setattr("storage.db.DATA_DIR", root)
    monkeypatch.setattr("storage.db.DB_PATH", db_path)

    from storage.db import init_db, reset_for_tests
    from storage.users import ensure_init_user

    reset_for_tests()
    init_db(db_path)
    ensure_init_user()
    return root


def _set_fixed_chunk_size(monkeypatch: pytest.MonkeyPatch, size: int) -> None:
    """Pin both size tiers so adaptive selection always returns ``size``."""
    monkeypatch.setattr("config.CHUNK_SIZE", size)
    monkeypatch.setattr("config.CHUNK_SIZE_LARGE", size)
    monkeypatch.setattr("config.CHUNK_SIZE_LARGE_THRESHOLD", 2**62)


@pytest.fixture()
def small_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shrink chunk size so multi-shard tests stay tiny."""
    _set_fixed_chunk_size(monkeypatch, TEST_CHUNK_SIZE)


@pytest.fixture()
def real_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Restore production chunk sizes (guards against leak from small_chunks)."""
    monkeypatch.setattr("config.CHUNK_SIZE", REAL_CHUNK_SIZE)
    monkeypatch.setattr("config.CHUNK_SIZE_LARGE", LARGE_CHUNK_SIZE)
    monkeypatch.setattr("config.CHUNK_SIZE_LARGE_THRESHOLD", LARGE_CHUNK_THRESHOLD)


def _test_user_and_key() -> tuple[str, bytes]:
    from services import crypto
    from storage import users as users_store

    user = users_store.get_by_username(TEST_USERNAME)
    assert user is not None
    master_key = crypto.derive_master_key(TEST_PASSPHRASE, crypto.b64d(user["kdf_salt"]))
    return user["id"], master_key


@pytest.fixture()
def memory_account(data_dir: Path) -> dict:
    from providers.memory import MemoryProvider
    from storage import accounts as accounts_store
    from storage import auth_context

    MemoryProvider.reset()
    user_id, master_key = _test_user_and_key()
    with auth_context.auth_scope(user_id, master_key):
        return accounts_store.add_account(
            provider="memory",
            label="memory-test",
            credentials={},
        )


def _login(client: TestClient) -> None:
    resp = client.post(
        "/api/auth/login",
        json={"username": TEST_USERNAME, "passphrase": TEST_PASSPHRASE},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True


@pytest.fixture()
def client(data_dir: Path, small_chunks: None, memory_account: dict) -> Iterator[TestClient]:
    from main import app
    from services.download_session import download_sessions
    from services.jobs import jobs
    from services.upload_pipeline import upload_pipelines

    jobs._jobs.clear()
    jobs._subs.clear()
    upload_pipelines._pipelines.clear()
    download_sessions._sessions.clear()

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def stream_client(
    data_dir: Path,
    real_chunks: None,
    memory_account: dict,
) -> Iterator[TestClient]:
    """Production chunk sizes for large streaming tests."""
    from main import app
    import config

    from services.download_session import download_sessions
    from services.jobs import jobs
    from services.upload_pipeline import upload_pipelines

    assert config.CHUNK_SIZE == REAL_CHUNK_SIZE, config.CHUNK_SIZE
    assert config.CHUNK_SIZE_LARGE == LARGE_CHUNK_SIZE, config.CHUNK_SIZE_LARGE
    assert config.CHUNK_SIZE_LARGE_THRESHOLD == LARGE_CHUNK_THRESHOLD

    jobs._jobs.clear()
    jobs._subs.clear()
    upload_pipelines._pipelines.clear()
    download_sessions._sessions.clear()

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def unlocked_client(client: TestClient) -> TestClient:
    _login(client)
    return client


@pytest.fixture()
def unlocked_stream_client(stream_client: TestClient) -> TestClient:
    _login(stream_client)
    return stream_client


@pytest.fixture()
def unlocked_disk_stream_client(
    data_dir: Path,
    real_chunks: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    """Like unlocked_stream_client but shards spill to disk (for 1 GiB tests)."""
    from main import app
    import config
    import services.vault as services_vault
    from providers.memory import MemoryProvider
    from services.download_session import download_sessions
    from services.jobs import jobs
    from services.upload_pipeline import upload_pipelines
    from storage import accounts as accounts_store
    from storage import auth_context

    # Keep shard buffers tiny for huge-file tests (WSL OOM otherwise).
    import services.upload_pipeline as upload_pipeline

    monkeypatch.setattr(config, "DOWNLOAD_QUEUE_DEPTH", 1)
    monkeypatch.setattr(services_vault, "DOWNLOAD_QUEUE_DEPTH", 1)
    monkeypatch.setattr(config, "SHARD_UPLOAD_CONCURRENCY", 2)
    monkeypatch.setattr(config, "SHARD_DOWNLOAD_CONCURRENCY", 2)
    monkeypatch.setattr(services_vault, "SHARD_UPLOAD_CONCURRENCY", 2)
    monkeypatch.setattr(services_vault, "SHARD_DOWNLOAD_CONCURRENCY", 2)
    monkeypatch.setattr(upload_pipeline, "SHARD_UPLOAD_CONCURRENCY", 2)
    monkeypatch.setattr(upload_pipeline, "UPLOAD_QUEUE_DEPTH", 0)

    MemoryProvider.reset()
    shard_dir = tmp_path / "shards"
    user_id, master_key = _test_user_and_key()
    with auth_context.auth_scope(user_id, master_key):
        # Replace the default memory account with a disk-backed one.
        for account in accounts_store.load_accounts():
            accounts_store.delete_account(account["id"])
        accounts_store.add_account(
            provider="memory",
            label="memory-disk",
            credentials={
                "disk_root": str(shard_dir),
                # Drop each shard after download so 1 GiB tests do not keep ~1 GiB cached.
                "unlink_after_read": True,
            },
        )

    jobs._jobs.clear()
    jobs._subs.clear()
    upload_pipelines._pipelines.clear()
    download_sessions._sessions.clear()

    with TestClient(app) as test_client:
        _login(test_client)
        yield test_client
