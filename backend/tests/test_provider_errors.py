from __future__ import annotations

from providers.errors import ProviderError, provider_error_from_download


class _FakeResponseError(Exception):
    def __init__(self, status: int, message: str) -> None:
        self.status = status
        self.message = message
        super().__init__(f"{status}, message='{message}', url='http://example'")


def test_bandwidth_limit_includes_account_label() -> None:
    account = {"id": "acc1", "provider": "mega", "label": "user@example.com"}
    err = provider_error_from_download(
        _FakeResponseError(509, "Bandwidth Limit Exceeded"),
        account,
    )
    assert isinstance(err, ProviderError)
    assert err.status_code == 509
    assert err.account_id == "acc1"
    assert err.account_label == "user@example.com"
    assert "Bandwidth limit exceeded" in str(err)
    assert "user@example.com" in str(err)
    assert err.to_meta()["provider"] == "mega"


def test_generic_download_error_includes_account() -> None:
    account = {"id": "acc2", "provider": "gdrive", "label": "drive-main"}
    err = provider_error_from_download(RuntimeError("boom"), account)
    assert "gdrive account drive-main" in str(err)
    assert "boom" in str(err)
