from __future__ import annotations

import logging
import tempfile
import time
from pathlib import Path
from typing import Any

from mega.client import MegaNzClient

from providers.mega_auth import mega_login, verify_mega_login
from providers.mega_session import mega_sessions

# Re-export for routes that import verify from providers.mega
__all__ = ["MegaProvider", "verify_mega_login", "mega_login"]

log = logging.getLogger(__name__)


class MegaProvider:
    def __init__(self, credentials: dict[str, Any]) -> None:
        self._email = credentials["email"]
        self._password = credentials["password"]
        self._mfa = credentials.get("mfa")

    def _session(self):
        return mega_sessions.get(self._email, self._password, self._mfa)

    def ensure_file_folder(self, file_id: str) -> str:
        """Per-upload folder under the vault root (cancel deletes this in one call)."""
        session = self._session()

        async def _ensure(client: MegaNzClient) -> str:
            return await session._ensure_file_folder(client, file_id)

        return session.run(_ensure)

    def list_vault_children(self) -> list[dict[str, str]]:
        session = self._session()

        async def _list(client: MegaNzClient) -> list[dict[str, str]]:
            return await session._list_vault_children(client)

        return session.run(_list)

    def upload(self, remote_id: str, data: bytes, *, parent_id: str | None = None) -> str:
        session = self._session()
        log.info("mega upload start name=%s bytes=%s email=%s", remote_id, len(data), self._email)
        t0 = time.monotonic()

        async def _upload(client: MegaNzClient) -> str:
            folder_id = (
                parent_id if parent_id is not None else await session._ensure_folder(client)
            )
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / remote_id
                path.write_bytes(data)
                node = await client.upload(path, dest_node_id=folder_id)
                return node.id

        try:
            node_id = session.run(_upload)
        except TimeoutError:
            log.error(
                "mega upload timed out name=%s bytes=%s elapsed=%.2fs email=%s",
                remote_id,
                len(data),
                time.monotonic() - t0,
                self._email,
            )
            mega_sessions.drop(self._email)
            raise
        except Exception:
            log.exception(
                "mega upload failed name=%s bytes=%s elapsed=%.2fs email=%s",
                remote_id,
                len(data),
                time.monotonic() - t0,
                self._email,
            )
            raise
        log.info(
            "mega upload done name=%s id=%s bytes=%s elapsed=%.2fs email=%s",
            remote_id,
            node_id,
            len(data),
            time.monotonic() - t0,
            self._email,
        )
        return node_id

    def download(self, remote_id: str) -> bytes:
        session = self._session()

        async def _download(client: MegaNzClient) -> bytes:
            fs = await client.get_filesystem()
            node = fs[remote_id]
            with tempfile.TemporaryDirectory() as tmp:
                out = await client.download(node, tmp)
                return Path(out).read_bytes()

        return session.run(_download)

    def exists(self, remote_id: str) -> bool:
        session = self._session()

        async def _exists(client: MegaNzClient) -> bool:
            fs = await client.get_filesystem()
            try:
                _ = fs[remote_id]
                return True
            except (KeyError, FileNotFoundError, TypeError):
                return False

        return session.run(_exists)

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
