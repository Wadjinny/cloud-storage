"""Master keys of unlocked users, held in memory for background work.

Trust model A already keeps the master key in the server session, so background
OAuth renewal needs the same key between requests: every authenticated request
re-arms the entry, and it is dropped on logout or after
``TOKEN_RENEWAL_SESSION_TTL`` seconds without a request from that user.
"""

from __future__ import annotations

import threading
import time

from config import TOKEN_RENEWAL_SESSION_TTL

_lock = threading.Lock()
_keys: dict[str, tuple[bytes, float]] = {}


def remember(user_id: str, master_key: bytes) -> None:
    """Keep (or refresh) this user's key for background renewal."""
    if TOKEN_RENEWAL_SESSION_TTL <= 0:
        return
    with _lock:
        _keys[user_id] = (master_key, time.monotonic())


def forget(user_id: str) -> None:
    with _lock:
        _keys.pop(user_id, None)


def active() -> list[tuple[str, bytes]]:
    """(user_id, master_key) for users seen within the TTL; expired entries dropped."""
    if TOKEN_RENEWAL_SESSION_TTL <= 0:
        return []
    now = time.monotonic()
    with _lock:
        for user_id, (_, seen_at) in list(_keys.items()):
            if now - seen_at > TOKEN_RENEWAL_SESSION_TTL:
                del _keys[user_id]
        return [(user_id, key) for user_id, (key, _) in _keys.items()]


def clear() -> None:
    with _lock:
        _keys.clear()
