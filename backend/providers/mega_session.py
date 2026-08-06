from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

from mega.client import MegaNzClient

from config import (
    MEGA_ACCOUNT_UPLOAD_CONCURRENCY,
    MEGA_ACCOUNT_DOWNLOAD_CONCURRENCY,
    MEGA_SESSION_TTL_SECONDS,
    PROVIDER_OP_TIMEOUT,
    SHARD_FOLDER_NAME,
)
from providers.mega_auth import mega_login

T = TypeVar("T")
CoroFn = Callable[[MegaNzClient], Awaitable[T]]

log = logging.getLogger(__name__)


class MegaAccountSession:
    """Reused MEGA logins on a dedicated event-loop thread.

    Keeps a small pool of clients per account so same-account shard ops can run
    in parallel without sharing one MegaNzClient across concurrent tasks.
    """

    def __init__(self, email: str, password: str, mfa: str | None) -> None:
        self._email = email
        self._password = password
        self._mfa = mfa
        # Pool must cover both upload and download per-account caps.
        self._pool_size = max(
            1,
            MEGA_ACCOUNT_UPLOAD_CONCURRENCY,
            MEGA_ACCOUNT_DOWNLOAD_CONCURRENCY,
        )
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._pool: asyncio.Queue[MegaNzClient] | None = None
        self._create_lock: asyncio.Lock | None = None
        self._folder_lock: asyncio.Lock | None = None
        self._created = 0
        self._clients: list[MegaNzClient] = []
        self._folder_id: str | None = None
        self._last_used = 0.0
        self._closed = False
        self._start()

    def _start(self) -> None:
        self._thread = threading.Thread(
            target=self._thread_main,
            name=f"mega-session-{self._email}",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout=30):
            raise RuntimeError("MEGA session loop failed to start")

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._pool = asyncio.Queue()
        self._create_lock = asyncio.Lock()
        self._folder_lock = asyncio.Lock()
        self._ready.set()
        loop.run_forever()
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        for client in list(self._clients):
            try:
                loop.run_until_complete(client.aclose())
            except Exception:
                pass
        self._clients.clear()
        self._created = 0
        loop.close()

    async def _acquire(self) -> MegaNzClient:
        assert self._pool is not None and self._create_lock is not None
        try:
            return self._pool.get_nowait()
        except asyncio.QueueEmpty:
            pass

        async with self._create_lock:
            try:
                return self._pool.get_nowait()
            except asyncio.QueueEmpty:
                pass
            if self._created < self._pool_size:
                client = await mega_login(self._email, self._password, self._mfa)
                self._clients.append(client)
                self._created += 1
                return client

        return await self._pool.get()

    async def _release(self, client: MegaNzClient, *, discard: bool = False) -> None:
        assert self._pool is not None and self._create_lock is not None
        if not discard:
            await self._pool.put(client)
            return

        async with self._create_lock:
            if client in self._clients:
                self._clients.remove(client)
                self._created = max(0, self._created - 1)
            try:
                await client.aclose()
            except Exception:
                pass

    async def _ensure_folder(self, client: MegaNzClient) -> str:
        if self._folder_id:
            return self._folder_id
        assert self._folder_lock is not None
        async with self._folder_lock:
            if self._folder_id:
                return self._folder_id
            folder = await client.create_folder(SHARD_FOLDER_NAME)
            self._folder_id = folder.id
            return self._folder_id

    async def _ensure_file_folder(self, client: MegaNzClient, file_id: str) -> str:
        """Create a per-file folder under the vault root (idempotent).

        mega.py's ``create_folder`` accepts POSIX paths for lookup, but ``mkdir``
        takes a single segment name (``/`` is illegal). So we resolve/create the
        vault root, then mkdir ``file_id`` under that parent.
        """
        parent_id = await self._ensure_folder(client)
        path = f"{SHARD_FOLDER_NAME}/{file_id}"
        try:
            existing = await client.find(path)
            if existing.is_folder:
                return existing.id
        except FileNotFoundError:
            pass
        folder = await client._core.mkdir(file_id, parent_id)
        return folder.id

    async def _run_op(self, op: CoroFn[T]) -> T:
        client = await self._acquire()
        self._last_used = time.time()
        try:
            result = await op(client)
        except Exception:
            await self._release(client, discard=True)
            raise
        else:
            await self._release(client)
            return result

    def run(self, op: CoroFn[T]) -> T:
        if self._closed or self._loop is None:
            raise RuntimeError("MEGA session is closed")
        future = asyncio.run_coroutine_threadsafe(self._run_op(op), self._loop)
        # Bound cloud ops so a stuck MEGA call cannot freeze upload/download forever.
        try:
            return future.result(timeout=PROVIDER_OP_TIMEOUT)
        except TimeoutError as exc:
            log.error(
                "mega op timed out after %ss email=%s",
                PROVIDER_OP_TIMEOUT,
                self._email,
            )
            future.cancel()
            raise TimeoutError(
                f"MEGA operation timed out after {PROVIDER_OP_TIMEOUT}s"
            ) from exc

    @property
    def idle_seconds(self) -> float:
        if self._last_used <= 0:
            return 0.0
        return time.time() - self._last_used

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        loop = self._loop
        if loop is None:
            return

        def _stop() -> None:
            loop.stop()

        loop.call_soon_threadsafe(_stop)
        if self._thread is not None:
            self._thread.join(timeout=15)
        self._loop = None
        self._thread = None


class MegaSessionHub:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, MegaAccountSession] = {}

    def _key(self, email: str) -> str:
        return email.strip().lower()

    def get(self, email: str, password: str, mfa: str | None) -> MegaAccountSession:
        key = self._key(email)
        with self._lock:
            self._prune_locked()
            session = self._sessions.get(key)
            if session is not None and not session._closed:
                return session
            session = MegaAccountSession(key, password, mfa)
            self._sessions[key] = session
            return session

    def drop(self, email: str) -> None:
        key = self._key(email)
        with self._lock:
            session = self._sessions.pop(key, None)
        if session is not None:
            session.close()

    def _prune_locked(self) -> None:
        ttl = max(60, MEGA_SESSION_TTL_SECONDS)
        stale = [
            key
            for key, session in self._sessions.items()
            if session._closed or (session._last_used > 0 and session.idle_seconds > ttl)
        ]
        for key in stale:
            session = self._sessions.pop(key)
            threading.Thread(target=session.close, daemon=True).start()


mega_sessions = MegaSessionHub()
