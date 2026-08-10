from __future__ import annotations

import pytest

from providers.retry import call_with_retries, is_transient_provider_error


def test_connection_reset_is_transient() -> None:
    assert is_transient_provider_error(ConnectionResetError(104, "Connection reset by peer"))
    assert is_transient_provider_error(TimeoutError("timed out"))
    assert not is_transient_provider_error(ValueError("bad shard"))


def test_call_with_retries_succeeds_after_transient(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("providers.retry.time.sleep", lambda _s: None)
    calls = {"n": 0}

    def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionResetError(104, "Connection reset by peer")
        return "ok"

    assert call_with_retries(flaky, op="test", attempts=3) == "ok"
    assert calls["n"] == 3


def test_call_with_retries_gives_up(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("providers.retry.time.sleep", lambda _s: None)

    def always_fail() -> str:
        raise ConnectionResetError(104, "Connection reset by peer")

    with pytest.raises(ConnectionResetError):
        call_with_retries(always_fail, op="test", attempts=2)


def test_call_with_retries_does_not_retry_hard_errors() -> None:
    calls = {"n": 0}

    def hard() -> str:
        calls["n"] += 1
        raise ValueError("nope")

    with pytest.raises(ValueError):
        call_with_retries(hard, op="test", attempts=5)
    assert calls["n"] == 1
