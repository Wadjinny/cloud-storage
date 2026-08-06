from __future__ import annotations

import hashlib
from collections.abc import Iterator

# Deterministic bodies for streaming tests (aligned to 256-byte pattern).
LARGE_FILE_SIZE = 100 * 1024 * 1024
HUGE_FILE_SIZE = 1024 * 1024 * 1024  # 1 GiB
# HTTP body frames (distinct from vault CHUNK_SIZE). Must be a multiple of 256.
HTTP_FRAME = 1024 * 1024

_PATTERN = bytes(range(256))


def iter_pattern_bytes(size: int, frame: int = HTTP_FRAME) -> Iterator[bytes]:
    """Yield a repeating 0..255 pattern totaling exactly ``size`` bytes."""
    if frame % 256 != 0:
        raise ValueError("frame must be a multiple of 256")
    frame_blob = _PATTERN * (frame // 256)
    remaining = size
    while remaining >= frame:
        yield frame_blob
        remaining -= frame
    if remaining:
        yield frame_blob[:remaining]


def pattern_sha256(size: int, frame: int = HTTP_FRAME) -> str:
    digest = hashlib.sha256()
    for chunk in iter_pattern_bytes(size, frame=frame):
        digest.update(chunk)
    return digest.hexdigest()


def sha256_of_iter(chunks: Iterator[bytes]) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    for chunk in chunks:
        digest.update(chunk)
        total += len(chunk)
    return digest.hexdigest(), total
