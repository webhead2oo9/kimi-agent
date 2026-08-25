import pytest

from trust.tiers import TrustTier


def test_trust_tier_ordering() -> None:
    assert TrustTier.STAFF > TrustTier.REGULAR > TrustTier.MEMBER
    assert TrustTier.MEMBER < TrustTier.STAFF
    assert TrustTier.STAFF >= TrustTier.STAFF
    assert TrustTier.MEMBER <= TrustTier.REGULAR


def test_trust_tier_comparison_with_non_tier_raises_type_error() -> None:
    # Comparing against a non-TrustTier should raise TypeError (via NotImplemented),
    # not leak a KeyError from the internal ordering lookup.
    with pytest.raises(TypeError):
        _ = TrustTier.STAFF >= 5
    with pytest.raises(TypeError):
        _ = TrustTier.MEMBER < "staff"
