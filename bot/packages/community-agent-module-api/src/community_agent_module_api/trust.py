"""Trust tiers shared by module declarations and the host runtime."""

from __future__ import annotations

from enum import Enum

_TIER_ORDER = {"member": 0, "regular": 1, "staff": 2}


class TrustTier(Enum):
    STAFF = "staff"
    REGULAR = "regular"
    MEMBER = "member"

    def __ge__(self, other: object) -> bool:
        if not isinstance(other, TrustTier):
            return NotImplemented
        return _TIER_ORDER[self.value] >= _TIER_ORDER[other.value]

    def __gt__(self, other: object) -> bool:
        if not isinstance(other, TrustTier):
            return NotImplemented
        return _TIER_ORDER[self.value] > _TIER_ORDER[other.value]

    def __le__(self, other: object) -> bool:
        if not isinstance(other, TrustTier):
            return NotImplemented
        return _TIER_ORDER[self.value] <= _TIER_ORDER[other.value]

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, TrustTier):
            return NotImplemented
        return _TIER_ORDER[self.value] < _TIER_ORDER[other.value]


__all__ = ["TrustTier"]
