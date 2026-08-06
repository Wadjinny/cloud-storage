from __future__ import annotations

import json
import secrets
import threading
from pathlib import Path
from typing import Any, Literal

from config import ACCOUNTS_PATH, DATA_DIR

ProviderName = Literal["gdrive", "mega"]

_lock = threading.Lock()


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _default() -> dict[str, Any]:
    return {"accounts": []}


def load_accounts() -> list[dict[str, Any]]:
    with _lock:
        if not ACCOUNTS_PATH.exists():
            return []
        data = json.loads(ACCOUNTS_PATH.read_text(encoding="utf-8"))
        return list(data.get("accounts", []))


def save_accounts(accounts: list[dict[str, Any]]) -> None:
    with _lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        _ensure_parent(ACCOUNTS_PATH)
        ACCOUNTS_PATH.write_text(
            json.dumps({"accounts": accounts}, indent=2),
            encoding="utf-8",
        )


def public_account(account: dict[str, Any]) -> dict[str, str]:
    return {
        "id": account["id"],
        "provider": account["provider"],
        "label": account["label"],
    }


def list_public() -> list[dict[str, str]]:
    return [public_account(a) for a in load_accounts()]


def get_account(account_id: str) -> dict[str, Any] | None:
    for account in load_accounts():
        if account["id"] == account_id:
            return account
    return None


def add_account(
    provider: ProviderName,
    label: str,
    credentials: dict[str, Any],
) -> dict[str, Any]:
    accounts = load_accounts()
    # Replace existing account with same provider+label
    accounts = [
        a
        for a in accounts
        if not (a["provider"] == provider and a["label"] == label)
    ]
    account = {
        "id": f"acc_{secrets.token_hex(8)}",
        "provider": provider,
        "label": label,
        "credentials": credentials,
    }
    accounts.append(account)
    save_accounts(accounts)
    return account


def delete_account(account_id: str) -> bool:
    accounts = load_accounts()
    next_accounts = [a for a in accounts if a["id"] != account_id]
    if len(next_accounts) == len(accounts):
        return False
    save_accounts(next_accounts)
    return True
