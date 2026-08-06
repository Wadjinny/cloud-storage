from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any

from config import STORAGE_FULL_RATIO
from providers.factory import get_provider

# AES-GCM authentication tag appended to each ciphertext chunk.
GCM_TAG_LEN = 16


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
    """Round-robin among accounts that still fit under the fullness ratio."""

    quotas: list[AccountQuota]
    _cursor: int = 0
    _lock: Any = field(default=None, repr=False)

    def __post_init__(self) -> None:
        import threading

        self._lock = threading.Lock()

    @classmethod
    def from_accounts(cls, account_list: list[dict[str, Any]]) -> PlacementPlanner:
        quotas = _fetch_quotas(account_list)
        return cls(quotas=quotas)

    def placeable_total(self) -> int | None:
        """Sum of known headrooms; None if any account has unknown quota."""
        total = 0
        for q in self.quotas:
            room = q.headroom()
            if room is None:
                return None
            total += room
        return total

    def pick(self, need: int) -> str:
        """Reserve ``need`` bytes on an eligible account and return its id."""
        with self._lock:
            n = len(self.quotas)
            if n == 0:
                raise ValueError("Connect at least one account before uploading")

            for offset in range(n):
                idx = (self._cursor + offset) % n
                q = self.quotas[idx]
                if q.can_fit(need):
                    q.reserved += need
                    self._cursor = idx + 1
                    return q.account_id

            ratio_pct = int(round(STORAGE_FULL_RATIO * 100))
            raise ValueError(
                f"No connected account has enough free space under the "
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
