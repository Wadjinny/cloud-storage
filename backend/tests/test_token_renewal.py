from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from providers import gdrive
from providers.errors import AccountAuthError
from services import crypto, session_keys, token_renewal
from storage import accounts as accounts_store
from storage import auth_context
from storage import users as users_store
from tests.conftest import TEST_PASSPHRASE, TEST_USERNAME


def _user_and_key() -> tuple[str, bytes]:
    user = users_store.get_by_username(TEST_USERNAME)
    assert user is not None
    return user["id"], crypto.derive_master_key(TEST_PASSPHRASE, crypto.b64d(user["kdf_salt"]))


def _naive_utc(**delta: float) -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(**delta)


def _add_gdrive_account(
    user_id: str,
    master_key: bytes,
    *,
    label: str = "drive@example.com",
    expiry: datetime | None = None,
    token: str = "old-access-token",
) -> dict:
    with auth_context.auth_scope(user_id, master_key):
        return accounts_store.add_account(
            provider="gdrive",
            label=label,
            credentials={
                "token": token,
                "refresh_token": "refresh-token",
                "expiry": None if expiry is None else expiry.isoformat(),
            },
        )


class _FakeRefresh:
    """Stand in for Credentials.refresh: hand out a new token or fail like Google."""

    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls = 0

    def __call__(self, creds, _request) -> None:  # noqa: ANN001
        self.calls += 1
        if self.error is not None:
            raise self.error
        creds.token = "new-access-token"
        creds.expiry = _naive_utc(hours=1)


def _patch_refresh(monkeypatch: pytest.MonkeyPatch, fake: _FakeRefresh) -> None:
    monkeypatch.setattr(
        "google.oauth2.credentials.Credentials.refresh",
        lambda self, request: fake(self, request),
    )


def test_credentials_expire_within_window() -> None:
    soon = {"token": "t", "refresh_token": "r", "expiry": _naive_utc(minutes=5).isoformat()}
    later = {"token": "t", "refresh_token": "r", "expiry": _naive_utc(hours=3).isoformat()}
    undated = {"token": "t", "refresh_token": "r", "expiry": None}
    assert gdrive.credentials_expire_within(soon, 900) is True
    assert gdrive.credentials_expire_within(later, 900) is False
    assert gdrive.credentials_expire_within(undated, 900) is True
    # No refresh token: nothing to renew (MEGA-style credentials).
    assert gdrive.credentials_expire_within({"token": "t"}, 900) is False


def test_sweep_renews_expiring_token(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    user_id, master_key = _user_and_key()
    account = _add_gdrive_account(user_id, master_key, expiry=_naive_utc(minutes=2))
    fake = _FakeRefresh()
    _patch_refresh(monkeypatch, fake)
    session_keys.clear()
    session_keys.remember(user_id, master_key)
    try:
        assert token_renewal.sweep_once() == 1
    finally:
        session_keys.clear()

    assert fake.calls == 1
    with auth_context.auth_scope(user_id, master_key):
        stored = accounts_store.get_account(account["id"])
    assert stored is not None
    assert stored["credentials"]["token"] == "new-access-token"
    assert stored["credentials"]["refresh_token"] == "refresh-token"
    assert stored["auth_state"] == "ok"
    assert stored["auth_renewed_at"] is not None


def test_sweep_skips_token_with_headroom(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    user_id, master_key = _user_and_key()
    _add_gdrive_account(user_id, master_key, expiry=_naive_utc(hours=5))
    fake = _FakeRefresh()
    _patch_refresh(monkeypatch, fake)
    session_keys.clear()
    session_keys.remember(user_id, master_key)
    try:
        assert token_renewal.sweep_once() == 0
    finally:
        session_keys.clear()
    assert fake.calls == 0


def test_invalid_grant_flags_account_for_reconnect(
    data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id, master_key = _user_and_key()
    account = _add_gdrive_account(user_id, master_key, expiry=_naive_utc(minutes=1))
    fake = _FakeRefresh(error=RuntimeError("('invalid_grant: Token has been expired or revoked.',)"))
    _patch_refresh(monkeypatch, fake)

    rows = token_renewal.renew_user_accounts(user_id, master_key)
    assert [row["state"] for row in rows] == ["expired"]
    assert "must be reconnected" in (rows[0]["error"] or "")

    with auth_context.auth_scope(user_id, master_key):
        stored = accounts_store.get_account(account["id"])
        assert stored is not None
        assert stored["auth_state"] == "expired"
        assert accounts_store.needs_reauth(stored)
        # Flagged accounts stop taking new shards but stay listed and downloadable.
        assert accounts_store.load_enabled_accounts() == []
        assert any(a["id"] == account["id"] for a in accounts_store.load_accounts())

    # The sweep does not retry a dead grant; the manual renew (force) does.
    rows = token_renewal.renew_user_accounts(user_id, master_key)
    assert fake.calls == 1
    assert rows[0]["state"] == "expired"
    token_renewal.renew_user_accounts(user_id, master_key, force=True)
    assert fake.calls == 2


def test_transient_refresh_error_keeps_account_ok(
    data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id, master_key = _user_and_key()
    account = _add_gdrive_account(user_id, master_key, expiry=_naive_utc(minutes=1))
    _patch_refresh(monkeypatch, _FakeRefresh(error=RuntimeError("connection reset by peer")))

    rows = token_renewal.renew_user_accounts(user_id, master_key)
    assert rows[0]["state"] == "error"
    with auth_context.auth_scope(user_id, master_key):
        stored = accounts_store.get_account(account["id"])
    assert stored is not None
    assert stored["auth_state"] == "ok"


def test_renew_endpoint_reports_expired(
    unlocked_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id, master_key = _user_and_key()
    _add_gdrive_account(user_id, master_key, expiry=_naive_utc(minutes=1))
    _patch_refresh(monkeypatch, _FakeRefresh(error=RuntimeError("invalid_grant: Token has been expired or revoked.")))

    resp = unlocked_client.post("/api/accounts/renew")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["expired"] == 1
    assert body["renewed"] == 0

    listing = unlocked_client.get("/api/accounts")
    assert listing.status_code == 200, listing.text
    drive = next(a for a in listing.json() if a["provider"] == "gdrive")
    assert drive["auth_state"] == "expired"
    assert "must be reconnected" in drive["auth_error"]


def test_reconnect_keeps_account_id_and_clears_flag(data_dir: Path) -> None:
    user_id, master_key = _user_and_key()
    account = _add_gdrive_account(user_id, master_key, expiry=_naive_utc(minutes=1))
    with auth_context.auth_scope(user_id, master_key):
        accounts_store.set_enabled(account["id"], False)
        accounts_store.mark_auth_expired(account["id"], "dead grant")
        # Same provider + label = the same account, re-consented.
        again = accounts_store.add_account(
            provider="gdrive",
            label=account["label"],
            credentials={"token": "t2", "refresh_token": "r2", "expiry": None},
        )
        assert again["id"] == account["id"]
        assert again["reconnected"] is True
        # An explicit Off stays off after reconnecting.
        assert again["enabled"] is False
        stored = accounts_store.get_account(account["id"])
        assert stored is not None
        assert stored["auth_state"] == "ok"
        assert stored["auth_error"] is None
        assert stored["credentials"]["refresh_token"] == "r2"
        # Reconnect updated in place instead of inserting a second row.
        assert accounts_store.count_accounts() == 1


def test_provider_refresh_failure_raises_account_auth_error(
    data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id, master_key = _user_and_key()
    account = _add_gdrive_account(user_id, master_key, expiry=_naive_utc(minutes=1))
    _patch_refresh(
        monkeypatch,
        _FakeRefresh(error=RuntimeError("('invalid_grant: Token has been expired or revoked.',)")),
    )
    with auth_context.auth_scope(user_id, master_key), pytest.raises(AccountAuthError) as excinfo:
        from providers.factory import get_provider

        get_provider(dict(account, user_id=user_id))
    assert "must be reconnected" in str(excinfo.value)
    assert excinfo.value.account_id == account["id"]
    with auth_context.auth_scope(user_id, master_key):
        stored = accounts_store.get_account(account["id"])
    assert stored is not None
    assert stored["auth_state"] == "expired"


def test_oauth_start_rejects_cross_host_flow(
    unlocked_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A flow started on another loopback host could never finish (no cookie there)."""
    import routes.auth_google as auth_google

    monkeypatch.setattr(auth_google, "GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setattr(auth_google, "GOOGLE_CLIENT_SECRET", "client-secret")
    monkeypatch.setattr(auth_google, "google_configured", lambda: True)
    monkeypatch.setattr(
        auth_google,
        "GOOGLE_REDIRECT_URI",
        "http://127.0.0.1:1234/api/auth/google/callback",
    )

    # TestClient requests come from host 'testserver'.
    resp = unlocked_client.get("/api/auth/google/start", follow_redirects=False)
    assert resp.status_code == 400, resp.text
    assert "session cookies are not shared" in resp.json()["detail"]

    resp = unlocked_client.get("/api/auth/google/callback", follow_redirects=False)
    assert resp.status_code == 400, resp.text
    assert "session cookies are not shared" in resp.json()["detail"]

    # Same host: the guard stays out of the way (no code/state → invalid state).
    monkeypatch.setattr(
        auth_google,
        "GOOGLE_REDIRECT_URI",
        "http://testserver/api/auth/google/callback",
    )
    resp = unlocked_client.get("/api/auth/google/callback", follow_redirects=False)
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"] == "Invalid OAuth state"
