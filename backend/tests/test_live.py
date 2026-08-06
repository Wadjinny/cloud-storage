"""Live provider smoke tests. Skipped unless credentials are in the environment.

MEGA:
  CLOUD_STORAGE_TEST_MEGA_EMAIL
  CLOUD_STORAGE_TEST_MEGA_PASSWORD
  CLOUD_STORAGE_TEST_MEGA_MFA (optional)

Google Drive (uses GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET from backend/.env):
  CLOUD_STORAGE_TEST_GDRIVE_REFRESH_TOKEN
  CLOUD_STORAGE_TEST_GDRIVE_TOKEN (optional access token)
  CLOUD_STORAGE_TEST_GDRIVE_LABEL (optional label, default test-gdrive)

Shards go under SHARD_FOLDER_NAME=cloud-storage-vault-test (not the prod folder).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from tests.conftest import TEST_PASSPHRASE, TEST_USERNAME
from tests.helpers import download_bytes, upload_bytes, wait_job

pytestmark = pytest.mark.live

LIVE_FOLDER = "cloud-storage-vault-test"


def _env(*names: str) -> str | None:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return None


@pytest.fixture()
def live_data_dir(data_dir, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SHARD_FOLDER_NAME", LIVE_FOLDER)
    monkeypatch.setattr("config.SHARD_FOLDER_NAME", LIVE_FOLDER)
    monkeypatch.setattr("providers.gdrive.SHARD_FOLDER_NAME", LIVE_FOLDER)
    monkeypatch.setattr("providers.mega_session.SHARD_FOLDER_NAME", LIVE_FOLDER)
    return data_dir


@pytest.fixture()
def live_client(live_data_dir) -> Iterator[TestClient]:
    from main import app
    from services.download_session import download_sessions
    from services.jobs import jobs
    from services.upload_pipeline import upload_pipelines

    jobs._jobs.clear()
    jobs._subs.clear()
    upload_pipelines._pipelines.clear()
    download_sessions._sessions.clear()

    with TestClient(app) as client:
        login = client.post(
            "/api/auth/login",
            json={"username": TEST_USERNAME, "passphrase": TEST_PASSPHRASE},
        )
        assert login.status_code == 200, login.text
        yield client


def _roundtrip(client: TestClient, provider: str) -> None:
    suffix = uuid.uuid4().hex[:10]
    payload = f"live-{provider}-{suffix}\n".encode() * 200
    filename = f"pytest-{provider}-{suffix}.bin"
    file_id = upload_bytes(client, payload, filename=filename, path="/pytest-live")
    assert download_bytes(client, file_id) == payload

    detail = client.get(f"/api/vault/files/{file_id}")
    assert detail.status_code == 200
    assert detail.json()["shard_count"] >= 1

    deleted = client.delete(f"/api/vault/files/{file_id}")
    assert deleted.status_code == 200
    job = wait_job(client, deleted.json()["job_id"], timeout=120)
    assert job["status"] == "done", job
    assert client.get(f"/api/vault/files/{file_id}").status_code == 404


@pytest.mark.skipif(
    not (_env("CLOUD_STORAGE_TEST_MEGA_EMAIL") and _env("CLOUD_STORAGE_TEST_MEGA_PASSWORD")),
    reason="Set CLOUD_STORAGE_TEST_MEGA_EMAIL and CLOUD_STORAGE_TEST_MEGA_PASSWORD",
)
def test_mega_upload_download_delete(live_client: TestClient) -> None:
    from services import crypto
    from storage import accounts as accounts_store
    from storage import auth_context
    from storage import users as users_store

    email = _env("CLOUD_STORAGE_TEST_MEGA_EMAIL")
    password = _env("CLOUD_STORAGE_TEST_MEGA_PASSWORD")
    assert email and password
    credentials = {"email": email, "password": password}
    mfa = _env("CLOUD_STORAGE_TEST_MEGA_MFA")
    if mfa:
        credentials["mfa"] = mfa
    user = users_store.get_by_username(TEST_USERNAME)
    assert user is not None
    master_key = crypto.derive_master_key(TEST_PASSPHRASE, crypto.b64d(user["kdf_salt"]))
    with auth_context.auth_scope(user["id"], master_key):
        accounts_store.add_account(provider="mega", label=email, credentials=credentials)
    _roundtrip(live_client, "mega")


@pytest.mark.skipif(
    not _env("CLOUD_STORAGE_TEST_GDRIVE_REFRESH_TOKEN"),
    reason="Set CLOUD_STORAGE_TEST_GDRIVE_REFRESH_TOKEN",
)
def test_gdrive_upload_download_delete(live_client: TestClient) -> None:
    from config import google_configured
    from services import crypto
    from storage import accounts as accounts_store
    from storage import auth_context
    from storage import users as users_store

    if not google_configured():
        pytest.skip("GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET not configured")

    refresh = _env("CLOUD_STORAGE_TEST_GDRIVE_REFRESH_TOKEN")
    assert refresh
    credentials = {
        "refresh_token": refresh,
        "token": _env("CLOUD_STORAGE_TEST_GDRIVE_TOKEN"),
    }
    label = _env("CLOUD_STORAGE_TEST_GDRIVE_LABEL") or "test-gdrive"
    user = users_store.get_by_username(TEST_USERNAME)
    assert user is not None
    master_key = crypto.derive_master_key(TEST_PASSPHRASE, crypto.b64d(user["kdf_salt"]))
    with auth_context.auth_scope(user["id"], master_key):
        accounts_store.add_account(provider="gdrive", label=label, credentials=credentials)
    _roundtrip(live_client, "gdrive")
