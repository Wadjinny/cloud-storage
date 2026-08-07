from __future__ import annotations

import math
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any

from config import SHARD_BALANCE_SLACK, STORAGE_FULL_RATIO
from providers.factory import get_provider

# AES-GCM authentication tag appended to each ciphertext chunk.
GCM_TAG_LEN = 16


class _UserStripe:
    """Process-wide round-robin cursor shared by concurrent uploads for one user."""

    __slots__ = ("lock", "cursor")

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.cursor = 0


_stripes_lock = threading.Lock()
_stripes: dict[str, _UserStripe] = {}


def _stripe_for(user_id: str | None) -> _UserStripe | None:
    if not user_id:
        return None
    with _stripes_lock:
        stripe = _stripes.get(user_id)
        if stripe is None:
            stripe = _UserStripe()
            _stripes[user_id] = stripe
        return stripe


def reset_placement_stripes() -> None:
    """Clear shared placement cursors (tests)."""
    with _stripes_lock:
        _stripes.clear()


def cipher_size(plaintext_len: int) -> int:
    return max(0, plaintext_len) + GCM_TAG_LEN


def usage_ratio(used: int, total: int) -> float:
    if total <= 0:
        return 1.0
    return used / total


def is_full(used: int | None, total: int | None, *, reserved: int = 0) -> bool:
    """True when usage (plus reserved bytes) is at or above STORAGE_FULL_RATIO.

    Unknown quotas are treated as not full so placement is not blocked.
    """
    if used is None or total is None or total <= 0:
        return False
    return usage_ratio(used + max(0, reserved), total) >= STORAGE_FULL_RATIO


def headroom_bytes(used: int | None, total: int | None, *, reserved: int = 0) -> int | None:
    """Bytes that can still be placed before hitting the fullness ratio.

    Returns None when quota is unknown.
    """
    if used is None or total is None or total <= 0:
        return None
    # Max bytes allowed under the threshold: just below ratio * total.
    # Use floor of ratio * total as the soft cap.
    soft_cap = int(total * STORAGE_FULL_RATIO)
    hard_free = max(0, total - used - reserved)
    soft_free = max(0, soft_cap - used - reserved)
    return min(hard_free, soft_free)


def can_fit(
    used: int | None,
    total: int | None,
    need: int,
    *,
    reserved: int = 0,
) -> bool:
    if need < 0:
        return False
    if used is None or total is None or total <= 0:
        return True
    room = headroom_bytes(used, total, reserved=reserved)
    return room is not None and room >= need


def shard_balance_cap(total_shards: int, placeable_accounts: int, *, slack: float | None = None) -> int:
    """Max shards one placeable account may take before others catch up."""
    n = max(1, placeable_accounts)
    total = max(0, total_shards)
    factor = 1.0 + (SHARD_BALANCE_SLACK if slack is None else max(0.0, slack))
    return max(1, math.ceil((total / n) * factor))


@dataclass
class AccountQuota:
    account_id: str
    used: int | None
    total: int | None
    reserved: int = 0

    def is_full(self) -> bool:
        return is_full(self.used, self.total, reserved=self.reserved)

    def can_fit(self, need: int) -> bool:
        return can_fit(self.used, self.total, need, reserved=self.reserved)

    def headroom(self) -> int | None:
        return headroom_bytes(self.used, self.total, reserved=self.reserved)


@dataclass
class PlacementPlanner:
    """Round-robin among accounts that still fit under the fullness ratio.

    Optionally enforces a soft per-account shard balance cap so fast providers
    (e.g. Drive) cannot monopolize placement while slower ones (e.g. MEGA) lag.
    When every eligible candidate is at the balance cap, the cap is relaxed so
    the upload can still finish.

    When ``user_id`` is set, the round-robin cursor is shared across all planners
    for that user so concurrent small-file uploads stripe across accounts instead
    of each job restarting at the first account.
    """

    quotas: list[AccountQuota]
    user_id: str | None = None
    _cursor: int = 0
    _lock: Any = field(default=None, repr=False)
    _stripe: _UserStripe | None = field(default=None, repr=False)
    _shard_counts: dict[str, int] = field(default_factory=dict)
    _balance_cap: int | None = None

    def __post_init__(self) -> None:
        self._lock = threading.Lock()
        self._stripe = _stripe_for(self.user_id)
        for q in self.quotas:
            self._shard_counts.setdefault(q.account_id, 0)

    @classmethod
    def from_accounts(
        cls,
        account_list: list[dict[str, Any]],
        *,
        user_id: str | None = None,
    ) -> PlacementPlanner:
        # Disabled accounts stay connected for download/health but skip new shards.
        active = [a for a in account_list if a.get("enabled", True)]
        quotas = _fetch_quotas(active)
        uid = user_id
        if uid is None:
            try:
                from storage import auth_context

                uid = auth_context.get_user_id()
            except Exception:
                uid = None
        return cls(quotas=quotas, user_id=uid)

    def placeable_accounts(self) -> list[AccountQuota]:
        return [q for q in self.quotas if not q.is_full()]

    def placeable_total(self) -> int | None:
        """Sum of known headrooms; None if any account has unknown quota."""
        total = 0
        for q in self.quotas:
            room = q.headroom()
            if room is None:
                return None
            total += room
        return total

    def configure_balance(self, total_shards: int | None) -> int | None:
        """Enable shard-count balancing for a known total shard count.

        Returns the per-account cap, or None when balancing is skipped
        (unknown total, or no placeable accounts).
        """
        with self._lock:
            if total_shards is None or total_shards < 1:
                self._balance_cap = None
                return None
            placeable = self.placeable_accounts()
            if not placeable:
                self._balance_cap = None
                return None
            self._balance_cap = shard_balance_cap(total_shards, len(placeable))
            return self._balance_cap

    def seed_shards(self, counts: dict[str, int]) -> None:
        """Count already-placed shards (e.g. resume) toward the balance cap."""
        with self._lock:
            for account_id, n in counts.items():
                if account_id in self._shard_counts:
                    self._shard_counts[account_id] = max(0, int(n))

    def shard_count(self, account_id: str) -> int:
        with self._lock:
            return self._shard_counts.get(account_id, 0)

    def balance_cap(self) -> int | None:
        with self._lock:
            return self._balance_cap

    def pick(self, need: int, *, only: set[str] | None = None) -> str:
        """Reserve ``need`` bytes on an eligible account and return its id.

        If ``only`` is set, only those account ids are considered (used to skip
        accounts that are already at their upload concurrency cap).
        """
        # Shared stripe lock coordinates cursor across concurrent upload jobs.
        lock = self._stripe.lock if self._stripe is not None else self._lock
        with lock:
            n = len(self.quotas)
            if n == 0:
                raise ValueError("Connect at least one account before uploading")

            def _cursor() -> int:
                if self._stripe is not None:
                    return self._stripe.cursor
                return self._cursor

            def _set_cursor(value: int) -> None:
                if self._stripe is not None:
                    self._stripe.cursor = value
                else:
                    self._cursor = value

            def _candidates(*, respect_balance: bool) -> str | None:
                for offset in range(n):
                    idx = (_cursor() + offset) % n
                    q = self.quotas[idx]
                    if only is not None and q.account_id not in only:
                        continue
                    if not q.can_fit(need):
                        continue
                    if respect_balance and self._balance_cap is not None:
                        if self._shard_counts.get(q.account_id, 0) >= self._balance_cap:
                            continue
                    q.reserved += need
                    self._shard_counts[q.account_id] = (
                        self._shard_counts.get(q.account_id, 0) + 1
                    )
                    _set_cursor(idx + 1)
                    return q.account_id
                return None

            chosen = _candidates(respect_balance=True)
            if chosen is not None:
                return chosen
            # Relax: every placeable/concurrency-available account is at the
            # balance cap (or none were), finish the upload anyway.
            chosen = _candidates(respect_balance=False)
            if chosen is not None:
                return chosen

            ratio_pct = int(round(STORAGE_FULL_RATIO * 100))
            scope = "selected " if only is not None else ""
            raise ValueError(
                f"No connected {scope}account has enough free space under the "
                f"{ratio_pct}% full threshold for the next shard "
                f"({need} bytes needed)"
            )


def _fetch_quotas(account_list: list[dict[str, Any]]) -> list[AccountQuota]:
    if not account_list:
        return []

    def _one(account: dict[str, Any]) -> AccountQuota:
        used: int | None = None
        total: int | None = None
        try:
            quota = get_provider(account).quota()
            raw_used = quota.get("used")
            raw_total = quota.get("total")
            used = int(raw_used) if raw_used is not None else None
            total = int(raw_total) if raw_total is not None else None
        except Exception:
            pass
        return AccountQuota(account_id=account["id"], used=used, total=total)

    by_id: dict[str, AccountQuota] = {}
    workers = min(8, len(account_list))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_one, account): account["id"] for account in account_list}
        for fut in as_completed(futures):
            by_id[futures[fut]] = fut.result()
    return [by_id[a["id"]] for a in account_list]
