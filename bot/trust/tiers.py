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


def trust_tier_from_value(
    value: str,
    *,
    default: TrustTier = TrustTier.MEMBER,
    label: str = "trust tier",
) -> TrustTier:
    normalized = value.strip().lower()
    if not normalized:
        return default
    for tier in TrustTier:
        if tier.value == normalized:
            return tier
    allowed = ", ".join(t.value for t in TrustTier)
    raise ValueError(f"Invalid {label} {value!r}; expected one of: {allowed}")
