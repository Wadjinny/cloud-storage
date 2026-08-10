from __future__ import annotations

import pytest

from services.capacity import (
    AccountQuota,
    PlacementPlanner,
    can_fit,
    cipher_size,
    estimate_shard_cipher_bytes,
    headroom_bytes,
    is_full,
    reset_placement_stripes,
    shard_balance_cap,
)


def test_cipher_size_adds_gcm_tag() -> None:
    assert cipher_size(0) == 16
    assert cipher_size(100) == 116


def test_estimate_shard_cipher_bytes() -> None:
    assert estimate_shard_cipher_bytes(100, 64, 0) == cipher_size(64)
    assert estimate_shard_cipher_bytes(100, 64, 1) == cipher_size(36)
    assert estimate_shard_cipher_bytes(0, 64, 0) == cipher_size(0)
    assert estimate_shard_cipher_bytes(100, 64, 2) == 0


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


def test_placement_stripe_shared_across_planners() -> None:
    """Concurrent small-file jobs must share the round-robin cursor per user."""
    reset_placement_stripes()
    quotas = [
        AccountQuota(account_id="a", used=0, total=1000),
        AccountQuota(account_id="b", used=0, total=1000),
        AccountQuota(account_id="c", used=0, total=1000),
    ]
    # Each planner mimics one single-shard file upload.
    picks = [
        PlacementPlanner(quotas=list(quotas), user_id="usr_stripe").pick(10)
        for _ in range(6)
    ]
    assert picks == ["a", "b", "c", "a", "b", "c"]
    # Without a user_id, each planner keeps a private cursor (legacy / tests).
    alone = [
        PlacementPlanner(quotas=list(quotas)).pick(10)
        for _ in range(3)
    ]
    assert alone == ["a", "a", "a"]


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


def test_shard_balance_cap_formula(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("services.capacity.SHARD_BALANCE_SLACK", 0.2)
    # 100 / 4 * 1.2 = 30
    assert shard_balance_cap(100, 4) == 30
    # 10 / 3 * 1.2 ≈ 4
    assert shard_balance_cap(10, 3) == 4
    assert shard_balance_cap(1, 1) == 2  # ceil(1 * 1.2) = 2


def test_placement_balance_skips_saturated_then_relaxes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("services.capacity.SHARD_BALANCE_SLACK", 0.0)
    planner = PlacementPlanner(
        quotas=[
            AccountQuota(account_id="fast", used=0, total=10_000),
            AccountQuota(account_id="slow", used=0, total=10_000),
        ]
    )
    # Hard equal split: ceil(4/2)*1.0 = 2
    assert planner.configure_balance(4) == 2

    assert planner.pick(10) == "fast"
    assert planner.pick(10) == "slow"
    assert planner.pick(10) == "fast"
    assert planner.pick(10) == "slow"
    # Both at cap=2; relax so upload can finish.
    assert planner.pick(10) in {"fast", "slow"}
    assert planner.shard_count("fast") + planner.shard_count("slow") == 5


def test_placement_balance_uses_placeable_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("services.capacity.STORAGE_FULL_RATIO", 0.8)
    monkeypatch.setattr("services.capacity.SHARD_BALANCE_SLACK", 0.2)
    planner = PlacementPlanner(
        quotas=[
            AccountQuota(account_id="full", used=80, total=100),
            AccountQuota(account_id="a", used=0, total=10_000),
            AccountQuota(account_id="b", used=0, total=10_000),
        ]
    )
    # Placeable = 2 (full excluded) → ceil(10/2 * 1.2) = 6
    assert planner.configure_balance(10) == 6
    for _ in range(6):
        assert planner.pick(10) in {"a", "b"}
    assert planner.shard_count("full") == 0
    assert planner.shard_count("a") <= 6
    assert planner.shard_count("b") <= 6


def test_placement_balance_seeds_resume(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("services.capacity.SHARD_BALANCE_SLACK", 0.0)
    planner = PlacementPlanner(
        quotas=[
            AccountQuota(account_id="a", used=0, total=10_000),
            AccountQuota(account_id="b", used=0, total=10_000),
        ]
    )
    assert planner.configure_balance(4) == 2
    planner.seed_shards({"a": 2})
    # a is already at cap; next picks should prefer b.
    assert planner.pick(10) == "b"
    assert planner.pick(10) == "b"


def test_placement_balance_skipped_when_total_unknown() -> None:
    planner = PlacementPlanner(
        quotas=[AccountQuota(account_id="a", used=0, total=1000)]
    )
    assert planner.configure_balance(None) is None
    assert planner.balance_cap() is None
