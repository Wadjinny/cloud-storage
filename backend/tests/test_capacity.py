from __future__ import annotations

import pytest

from services.capacity import (
    AccountQuota,
    PlacementPlanner,
    can_fit,
    cipher_size,
    headroom_bytes,
    is_full,
)


def test_cipher_size_adds_gcm_tag() -> None:
    assert cipher_size(0) == 16
    assert cipher_size(100) == 116


def test_is_full_respects_ratio(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("services.capacity.STORAGE_FULL_RATIO", 0.8)
    assert is_full(79, 100) is False
    assert is_full(80, 100) is True
    assert is_full(70, 100, reserved=10) is True


def test_unknown_quota_never_full() -> None:
    assert is_full(None, None) is False
    assert is_full(50, None) is False
    assert can_fit(None, None, 999_999) is True


def test_headroom_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("services.capacity.STORAGE_FULL_RATIO", 0.8)
    # soft cap 80; used 50 → 30 placeable
    assert headroom_bytes(50, 100) == 30
    assert headroom_bytes(50, 100, reserved=20) == 10


def test_placement_round_robin() -> None:
    planner = PlacementPlanner(
        quotas=[
            AccountQuota(account_id="a", used=0, total=1000),
            AccountQuota(account_id="b", used=0, total=1000),
        ]
    )
    assert planner.pick(10) == "a"
    assert planner.pick(10) == "b"
    assert planner.pick(10) == "a"


def test_placement_skips_full_accounts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("services.capacity.STORAGE_FULL_RATIO", 0.8)
    planner = PlacementPlanner(
        quotas=[
            AccountQuota(account_id="full", used=80, total=100),
            AccountQuota(account_id="ok", used=0, total=1000),
        ]
    )
    assert planner.pick(16) == "ok"


def test_placement_raises_when_nowhere_to_put(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("services.capacity.STORAGE_FULL_RATIO", 0.8)
    planner = PlacementPlanner(
        quotas=[AccountQuota(account_id="full", used=80, total=100)]
    )
    with pytest.raises(ValueError, match="enough free space"):
        planner.pick(16)


def test_placement_only_restricts_candidates() -> None:
    planner = PlacementPlanner(
        quotas=[
            AccountQuota(account_id="a", used=0, total=1000),
            AccountQuota(account_id="b", used=0, total=1000),
        ]
    )
    assert planner.pick(10, only={"b"}) == "b"
    assert planner.pick(10, only={"b"}) == "b"
    with pytest.raises(ValueError, match="selected"):
        planner.pick(10, only=set())


def test_placeable_total_none_when_unknown() -> None:
    planner = PlacementPlanner(
        quotas=[
            AccountQuota(account_id="known", used=0, total=100),
            AccountQuota(account_id="unknown", used=None, total=None),
        ]
    )
    assert planner.placeable_total() is None
