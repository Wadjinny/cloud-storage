from __future__ import annotations

import secrets
import threading
from pathlib import Path
from typing import Any


class MemoryProvider:
    """Test blob store. In-process by default; set credentials disk_root for on-disk."""

    VAULT_ROOT_ID = "dir_vault_root"

    _store: dict[str, bytes] = {}
    _folders: dict[str, set[str]] = {}
    # folder_id → display name (for vault-root children listing)
    _folder_names: dict[str, str] = {}
    _lock = threading.Lock()
    _used = 0
    # Large enough that 1 GiB uploads stay under STORAGE_FULL_RATIO headroom.
    _total = 8 * 1024 * 1024 * 1024

    def __init__(
        self,
        credentials: dict[str, Any] | None = None,
        *,
        account_id: str | None = None,
    ) -> None:
        creds = credentials or {}
        self._used_override = creds.get("used")
        self._total_override = creds.get("total")
        root = creds.get("disk_root")
        self._disk_root = Path(root) if root else None
        self._unlink_after_read = bool(creds.get("unlink_after_read"))
        # Per-account roots so evacuate can delete the source vault folder without
        # wiping another memory account that shares the process-wide store.
        self._root_id = (
            f"{self.VAULT_ROOT_ID}_{account_id}" if account_id else self.VAULT_ROOT_ID
        )
        if self._disk_root is not None:
            self._disk_root.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._folders.setdefault(self._root_id, set())

    @classmethod
    def reset(cls) -> None:
        with cls._lock:
            cls._store.clear()
            cls._folders.clear()
            cls._folder_names.clear()
            cls._used = 0
            cls._folders.setdefault(cls.VAULT_ROOT_ID, set())

    def ensure_child_folder(self, name: str, *, parent_id: str | None = None) -> str:
        parent = parent_id if parent_id is not None else self._root_id
        folder_id = f"dir_{parent}_{name}"
        with self._lock:
            # Reuse existing child with the same display name under parent.
            for child_id in self._folders.get(parent, set()):
                if self._folder_names.get(child_id) == name and child_id in self._folders:
                    return child_id
            self._folders.setdefault(parent, set()).add(folder_id)
            self._folders.setdefault(folder_id, set())
            self._folder_names[folder_id] = name
        return folder_id

    def ensure_file_folder(self, file_id: str, *, parent_id: str | None = None) -> str:
        return self.ensure_child_folder(file_id, parent_id=parent_id)

    def list_vault_children(self) -> list[dict[str, str]]:
        with self._lock:
            child_ids = list(self._folders.get(self._root_id, set()))
            out: list[dict[str, str]] = []
            for child_id in child_ids:
                if child_id in self._folders:
                    out.append(
                        {
                            "id": child_id,
                            "name": self._folder_names.get(child_id, child_id),
                            "kind": "folder",
                        }
                    )
                else:
                    out.append(
                        {
                            "id": child_id,
                            "name": child_id,
                            "kind": "file",
                        }
                    )
            return out

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

    def exists(self, remote_id: str) -> bool:
        if self._disk_root is not None:
            if (self._disk_root / remote_id).exists():
                return True
            with self._lock:
                return remote_id in self._folders
        with self._lock:
            return remote_id in self._store or remote_id in self._folders

    def delete(self, remote_id: str) -> None:
        with self._lock:
            children = self._folders.pop(remote_id, None)
            self._folder_names.pop(remote_id, None)
            for folder_children in self._folders.values():
                folder_children.discard(remote_id)
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

    def vault_root(self) -> str:
        with self._lock:
            self._folders.setdefault(self._root_id, set())
        return self._root_id

    def move_item(self, remote_id: str, *, parent_id: str) -> None:
        if not self.exists(remote_id):
            raise FileNotFoundError(f"Missing remote {remote_id}")
        with self._lock:
            for children in self._folders.values():
                children.discard(remote_id)
            self._folders.setdefault(parent_id, set()).add(remote_id)

    def copy_item(self, remote_id: str, *, parent_id: str, name: str) -> str:
        with self._lock:
            is_folder = remote_id in self._folders
            child_ids = list(self._folders.get(remote_id, set())) if is_folder else []
            child_names = {
                child: self._folder_names.get(child, child) for child in child_ids
            }
        if is_folder:
            new_id = self.ensure_child_folder(name, parent_id=parent_id)
            for child in child_ids:
                self.copy_item(child, parent_id=new_id, name=child_names[child])
            return new_id
        data = self.download(remote_id)
        return self.upload(name, data, parent_id=parent_id)

    def quota(self) -> dict[str, int | None]:
        used = self._used_override if self._used_override is not None else self._used
        total = self._total_override if self._total_override is not None else self._total
        return {"used": int(used), "total": int(total)}
