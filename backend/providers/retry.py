from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")

_TRANSIENT_MARKERS = (
    "connection reset",
    "errno 104",
    "broken pipe",
    "temporarily unavailable",
    "eagain",
    "try again",
    "server disconnected",
    "clientoserror",
    "clientconnectorerror",
    "timeout",
    "timed out",
    "connection aborted",
    "connection refused",
    "network is unreachable",
    "ssl",
    "remote end closed",
)


def is_transient_provider_error(exc: BaseException) -> bool:
    name = type(exc).__name__.lower()
    if name in {
        "timeouterror",
        "connectionreseterror",
        "connectionabortederror",
        "connectionerror",
        "clientoserror",
        "clientconnectorerror",
        "serverdisconnectederror",
    }:
        return True
    text = str(exc).lower()
    return any(marker in text for marker in _TRANSIENT_MARKERS)


def call_with_retries(
    fn: Callable[[], T],
    *,
    op: str,
    attempts: int,
    on_retry: Callable[[BaseException, int], None] | None = None,
) -> T:
    """Run ``fn`` up to ``attempts`` times on transient provider/network errors."""
    tries = max(1, int(attempts))
    last: BaseException | None = None
    for attempt in range(1, tries + 1):
        try:
            return fn()
        except Exception as exc:
            last = exc
            if attempt >= tries or not is_transient_provider_error(exc):
                raise
            log.warning(
                "%s transient failure attempt %s/%s: %s",
                op,
                attempt,
                tries,
                exc,
            )
            if on_retry is not None:
                try:
                    on_retry(exc, attempt)
                except Exception:
                    log.exception("%s on_retry failed", op)
            time.sleep(min(8.0, 0.5 * (2 ** (attempt - 1))))
    assert last is not None
    raise last
