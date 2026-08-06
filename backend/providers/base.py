from typing import Protocol


class StorageProvider(Protocol):
    def upload(self, remote_id: str, data: bytes) -> str:
        """Upload opaque bytes; return the remote id to store in the index."""
        ...

    def download(self, remote_id: str) -> bytes:
        ...

    def delete(self, remote_id: str) -> None:
        ...

    def quota(self) -> dict[str, int | None]:
        """Return {used, total} in bytes when known."""
        ...
