from __future__ import annotations

from typing import Any

from providers.gdrive import GDriveProvider
from providers.mega import MegaProvider


def get_provider(account: dict[str, Any]) -> GDriveProvider | MegaProvider:
    provider = account["provider"]
    credentials = account["credentials"]
    if provider == "gdrive":
        return GDriveProvider(credentials)
    if provider == "mega":
        return MegaProvider(credentials)
    raise ValueError(f"Unknown provider: {provider}")
