from __future__ import annotations

import secrets
import threading
from pathlib import Path
from typing import Any


class MemoryProvider:
    """Test blob store. In-process by default; set credentials disk_root for on-disk."""

    _store: dict[str, bytes] = {}
    _folders: dict[str, set[str]] = {}
    _lock = threading.Lock()
    _used = 0
    # Large enough that 1 GiB uploads stay under STORAGE_FULL_RATIO headroom.
    _total = 8 * 1024 * 1024 * 1024

    def __init__(self, credentials: dict[str, Any] | None = None) -> None:
        creds = credentials or {}
        self._used_override = creds.get("used")
        self._total_override = creds.get("total")
        root = creds.get("disk_root")
        self._disk_root = Path(root) if root else None
        self._unlink_after_read = bool(creds.get("unlink_after_read"))
        if self._disk_root is not None:
            self._disk_root.mkdir(parents=True, exist_ok=True)

    @classmethod
    def reset(cls) -> None:
        with cls._lock:
            cls._store.clear()
            cls._folders.clear()
            cls._used = 0

    def ensure_file_folder(self, file_id: str) -> str:
        folder_id = f"dir_{file_id}"
        with self._lock:
            self._folders.setdefault(folder_id, set())
        return folder_id

    def upload(self, remote_id: str, data: bytes, *, parent_id: str | None = None) -> str:
        key = remote_id or f"mem_{secrets.token_hex(8)}"
        if self._disk_root is not None:
            path = self._disk_root / key
            prev_len = path.stat().st_size if path.exists() else 0
            path.write_bytes(data)
            with self._lock:
                self._used += len(data) - prev_len
                if parent_id is not None:
                    self._folders.setdefault(parent_id, set()).add(key)
            return key

        with self._lock:
            prev = self._store.get(key)
            if prev is not None:
                self._used -= len(prev)
            self._store[key] = data
            self._used += len(data)
            if parent_id is not None:
                self._folders.setdefault(parent_id, set()).add(key)
        return key

    def download(self, remote_id: str) -> bytes:
        if self._disk_root is not None:
            path = self._disk_root / remote_id
            if not path.exists():
                raise FileNotFoundError(f"Missing shard {remote_id}")
            data = path.read_bytes()
            if self._unlink_after_read:
                size = len(data)
                path.unlink(missing_ok=True)
                with self._lock:
                    self._used -= size
                    for children in self._folders.values():
                        children.discard(remote_id)
            return data

        with self._lock:
            data = self._store.get(remote_id)
        if data is None:
            raise FileNotFoundError(f"Missing shard {remote_id}")
        return data

    def delete(self, remote_id: str) -> None:
        with self._lock:
            children = self._folders.pop(remote_id, None)
        if children is not None:
            for child in list(children):
                self.delete(child)
            return

        if self._disk_root is not None:
            path = self._disk_root / remote_id
            if path.exists():
                size = path.stat().st_size
                path.unlink()
                with self._lock:
                    self._used -= size
                    for folder_children in self._folders.values():
                        folder_children.discard(remote_id)
            return

        with self._lock:
            prev = self._store.pop(remote_id, None)
            if prev is not None:
                self._used -= len(prev)
            for folder_children in self._folders.values():
                folder_children.discard(remote_id)

    def quota(self) -> dict[str, int | None]:
        used = self._used_override if self._used_override is not None else self._used
        total = self._total_override if self._total_override is not None else self._total
        return {"used": int(used), "total": int(total)}
