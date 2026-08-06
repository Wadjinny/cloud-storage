from __future__ import annotations

import json
import threading
from typing import Any

from config import DATA_DIR, VAULT_INDEX_PATH

_lock = threading.Lock()


def _default() -> dict[str, Any]:
    return {
        "version": 1,
        "kdf_salt": None,
        "verifier": None,
        "files": [],
    }


def load_index() -> dict[str, Any]:
    with _lock:
        if not VAULT_INDEX_PATH.exists():
            return _default()
        data = json.loads(VAULT_INDEX_PATH.read_text(encoding="utf-8"))
        data.setdefault("version", 1)
        data.setdefault("files", [])
        return data


def save_index(index: dict[str, Any]) -> None:
    with _lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        VAULT_INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
        VAULT_INDEX_PATH.write_text(json.dumps(index, indent=2), encoding="utf-8")


def find_file(index: dict[str, Any], file_id: str) -> dict[str, Any] | None:
    for entry in index.get("files", []):
        if entry["id"] == file_id:
            return entry
    return None


def upsert_file(index: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
    files = index.setdefault("files", [])
    for i, existing in enumerate(files):
        if existing["id"] == entry["id"]:
            files[i] = entry
            return index
    files.append(entry)
    return index


def remove_file(index: dict[str, Any], file_id: str) -> dict[str, Any] | None:
    files = index.get("files", [])
    for i, entry in enumerate(files):
        if entry["id"] == file_id:
            return files.pop(i)
    return None
