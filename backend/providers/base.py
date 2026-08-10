from typing import Protocol


class StorageProvider(Protocol):
    def ensure_child_folder(self, name: str, *, parent_id: str | None = None) -> str:
        """Create (or reuse) folder ``name`` under ``parent_id``, or vault root if None."""
        ...

    def ensure_file_folder(self, file_id: str, *, parent_id: str | None = None) -> str:
        """Create (or reuse) a per-vault-file folder under ``parent_id`` or vault root."""
        ...

    def list_vault_children(self) -> list[dict[str, str]]:
        """Children of the shard root: ``{id, name, kind}`` where kind is ``folder``|``file``."""
        ...

    def upload(self, remote_id: str, data: bytes, *, parent_id: str | None = None) -> str:
        """Upload opaque bytes into ``parent_id`` (or the vault root); return remote id."""
        ...

    def download(self, remote_id: str) -> bytes:
        ...

    def exists(self, remote_id: str) -> bool:
        """Return True if ``remote_id`` still exists on the provider (cheap metadata check)."""
        ...

    def delete(self, remote_id: str) -> None:
        """Delete a shard or a per-file folder (folder delete removes children)."""
        ...

    def vault_root(self) -> str:
        """Remote folder id of the opaque vault root (``SHARD_FOLDER_NAME``)."""
        ...

    def move_item(self, remote_id: str, *, parent_id: str) -> None:
        """Reparent ``remote_id`` under ``parent_id`` (provider-native when possible)."""
        ...

    def copy_item(self, remote_id: str, *, parent_id: str, name: str) -> str:
        """Duplicate ``remote_id`` under ``parent_id`` as ``name``; return new remote id."""
        ...

    def quota(self) -> dict[str, int | None]:
        """Return {used, total} in bytes when known."""
        ...
