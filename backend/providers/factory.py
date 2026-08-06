from __future__ import annotations

from typing import Any

from providers.gdrive import GDriveProvider
from providers.mega import MegaProvider
from providers.memory import MemoryProvider


def get_provider(
    account: dict[str, Any],
) -> GDriveProvider | MegaProvider | MemoryProvider:
    provider = account["provider"]
    credentials = account["credentials"]
    if provider == "gdrive":
        return GDriveProvider(credentials, account_id=account.get("id"))
    if provider == "mega":
        return MegaProvider(credentials)
    if provider == "memory":
        return MemoryProvider(credentials)
    raise ValueError(f"Unknown provider: {provider}")
