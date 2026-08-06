from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from collections.abc import Iterator

_user_id: ContextVar[str | None] = ContextVar("auth_user_id", default=None)
_master_key: ContextVar[bytes | None] = ContextVar("auth_master_key", default=None)


def get_user_id() -> str | None:
    return _user_id.get()


def get_master_key() -> bytes | None:
    return _master_key.get()


def require_user_id() -> str:
    user_id = _user_id.get()
    if not user_id:
        raise RuntimeError("No authenticated user in context")
    return user_id


def require_master_key() -> bytes:
    key = _master_key.get()
    if not key:
        raise RuntimeError("No master key in context")
    return key


def set_auth(user_id: str, master_key: bytes) -> None:
    # Prefer set() over Token.reset(): StreamingResponse / thread hops can exit
    # the scope in a different Context, which makes reset() raise ValueError.
    _user_id.set(user_id)
    _master_key.set(master_key)


def clear_auth() -> None:
    _user_id.set(None)
    _master_key.set(None)


@contextmanager
def auth_scope(user_id: str, master_key: bytes) -> Iterator[None]:
    prev_user = _user_id.get()
    prev_key = _master_key.get()
    set_auth(user_id, master_key)
    try:
        yield
    finally:
        if prev_user is None:
            _user_id.set(None)
        else:
            _user_id.set(prev_user)
        if prev_key is None:
            _master_key.set(None)
        else:
            _master_key.set(prev_key)
