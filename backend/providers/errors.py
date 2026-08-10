from __future__ import annotations

from typing import Any


class ProviderError(RuntimeError):
    """User-facing provider failure with optional account context."""

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        account_id: str | None = None,
        account_label: str | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.account_id = account_id
        self.account_label = account_label
        self.status_code = status_code

    def to_meta(self) -> dict[str, Any]:
        meta: dict[str, Any] = {}
        if self.provider:
            meta["provider"] = self.provider
        if self.account_id:
            meta["account_id"] = self.account_id
        if self.account_label:
            meta["account_label"] = self.account_label
        if self.status_code is not None:
            meta["status_code"] = self.status_code
        return meta


def provider_error_from_download(exc: BaseException, account: dict[str, Any]) -> ProviderError:
    """Map a raw provider download exception into a clear, account-tagged error."""
    if isinstance(exc, ProviderError):
        label = exc.account_label or account.get("label") or account.get("id")
        provider = exc.provider or account.get("provider")
        return ProviderError(
            str(exc),
            provider=str(provider) if provider else None,
            account_id=exc.account_id or account.get("id"),
            account_label=str(label) if label else None,
            status_code=exc.status_code,
        )

    label = str(account.get("label") or account.get("id") or "unknown")
    provider = str(account.get("provider") or "storage")
    status = getattr(exc, "status", None)
    if status is None:
        # aiohttp ClientResponseError string: "509, message='Bandwidth Limit Exceeded', ..."
        text = str(exc)
        if text.startswith("509") or "bandwidth limit exceeded" in text.lower():
            status = 509

    if status == 509:
        return ProviderError(
            f"Bandwidth limit exceeded on {provider} account {label}",
            provider=provider,
            account_id=account.get("id"),
            account_label=label,
            status_code=509,
        )

    detail = str(exc).strip() or type(exc).__name__
    if status is not None:
        return ProviderError(
            f"Download failed on {provider} account {label}: HTTP {status}",
            provider=provider,
            account_id=account.get("id"),
            account_label=label,
            status_code=int(status),
        )
    return ProviderError(
        f"Download failed on {provider} account {label}: {detail}",
        provider=provider,
        account_id=account.get("id"),
        account_label=label,
    )
