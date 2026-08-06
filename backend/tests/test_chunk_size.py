"""Adaptive plaintext shard sizing."""

from __future__ import annotations

from config import (
    CHUNK_SIZE,
    CHUNK_SIZE_LARGE,
    CHUNK_SIZE_LARGE_THRESHOLD,
    choose_chunk_size,
)


def test_choose_chunk_size_small_and_unknown() -> None:
    assert choose_chunk_size(None) == CHUNK_SIZE
    assert choose_chunk_size(0) == CHUNK_SIZE
    assert choose_chunk_size(CHUNK_SIZE_LARGE_THRESHOLD - 1) == CHUNK_SIZE


def test_choose_chunk_size_large() -> None:
    assert choose_chunk_size(CHUNK_SIZE_LARGE_THRESHOLD) == CHUNK_SIZE_LARGE
    assert choose_chunk_size(CHUNK_SIZE_LARGE_THRESHOLD * 10) == CHUNK_SIZE_LARGE
