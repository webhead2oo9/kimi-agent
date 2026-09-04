"""White-box probes for the shared network-namespace lease."""

from sandbox.netns_lease import NetnsLease


def netns_lease_is_poisoned(lease: NetnsLease) -> bool:
    """Inspect fail-closed state without adding a production status API."""

    return lease._poisoned
