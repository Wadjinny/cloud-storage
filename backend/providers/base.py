from typing import Protocol


class StorageProvider(Protocol):
    def ensure_file_folder(self, file_id: str) -> str:
        """Create (or reuse) a per-vault-file folder under the shard root; return its id."""
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

    def quota(self) -> dict[str, int | None]:
        """Return {used, total} in bytes when known."""
        ...
