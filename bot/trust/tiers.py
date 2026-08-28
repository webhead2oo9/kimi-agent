from __future__ import annotations

from community_agent_module_api import TrustTier


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
