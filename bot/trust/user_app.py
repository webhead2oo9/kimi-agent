from __future__ import annotations

from dataclasses import dataclass

from trust.tiers import TrustTier


@dataclass(frozen=True)
class UserAppAccess:
    """User-install access and trust, independent from guild membership/roles."""

    member_ids: frozenset[str] = frozenset()
    regular_ids: frozenset[str] = frozenset()
    staff_ids: frozenset[str] = frozenset()

    def resolve(self, user_id: str) -> TrustTier | None:
        if user_id in self.staff_ids:
            return TrustTier.STAFF
        if user_id in self.regular_ids:
            return TrustTier.REGULAR
        if user_id in self.member_ids:
            return TrustTier.MEMBER
        return None
