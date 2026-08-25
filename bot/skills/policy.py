from __future__ import annotations

from trust.tiers import TrustTier

DEFAULT_SKILL_TOOL_MIN_TIER = TrustTier.STAFF


def normalize_skill_tool_min_tier(value: str | None) -> TrustTier:
    if not value:
        return DEFAULT_SKILL_TOOL_MIN_TIER
    return TrustTier(str(value).lower())
