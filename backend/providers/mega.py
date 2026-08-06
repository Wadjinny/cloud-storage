from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from mega.client import MegaNzClient

from providers.mega_auth import mega_login, verify_mega_login
from providers.mega_session import mega_sessions

# Re-export for routes that import verify from providers.mega
__all__ = ["MegaProvider", "verify_mega_login", "mega_login"]


class MegaProvider:
    def __init__(self, credentials: dict[str, Any]) -> None:
        self._email = credentials["email"]
        self._password = credentials["password"]
        self._mfa = credentials.get("mfa")

    def _session(self):
        return mega_sessions.get(self._email, self._password, self._mfa)

    def upload(self, remote_id: str, data: bytes) -> str:
        session = self._session()

        async def _upload(client: MegaNzClient) -> str:
            folder_id = await session._ensure_folder(client)
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / remote_id
                path.write_bytes(data)
                node = await client.upload(path, dest_node_id=folder_id)
                return node.id

        return session.run(_upload)

    def download(self, remote_id: str) -> bytes:
        session = self._session()

        async def _download(client: MegaNzClient) -> bytes:
            fs = await client.get_filesystem()
            node = fs[remote_id]
            with tempfile.TemporaryDirectory() as tmp:
                out = await client.download(node, tmp)
                return Path(out).read_bytes()

        return session.run(_download)

    def delete(self, remote_id: str) -> None:
        session = self._session()

        async def _delete(client: MegaNzClient) -> None:
            try:
                await client.destroy(remote_id)
            except Exception:
                await client.delete(remote_id)

        session.run(_delete)

    def quota(self) -> dict[str, int | None]:
        session = self._session()

        async def _quota(client: MegaNzClient) -> dict[str, int | None]:
            stats = await client.get_account_stats()
            return {
                "used": int(stats.storage.used),
                "total": int(stats.storage.max),
            }

        try:
            return session.run(_quota)
        except Exception:
            mega_sessions.drop(self._email)
            return {"used": None, "total": None}
