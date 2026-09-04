from __future__ import annotations

from typing import Any

from providers.errors import provider_status_code
from providers.failure_policy import (
    CircuitScopeKind,
    CooldownPolicy,
    FailureCategory,
    ProviderFailure,
    generic_failure_policy,
    provider_error_body,
    register_failure_adapter,
    retry_after_timestamp,
)


_FIVE_HOUR_CODES = {"1316", "1318", "1320"}
_SEVEN_DAY_CODES = {"1317", "1319", "1321"}
_ACCOUNT_RESTRICTION_CODES = {"1309", "1313", "1314", "1315"}


def _business_code(body: dict[str, Any] | None) -> str | None:
    if body is None:
        return None
    error = body.get("error")
    candidate = error.get("code") if isinstance(error, dict) else body.get("code")
    if isinstance(candidate, (str, int)):
        return str(candidate)
    return None


def zai_failure_policy(
    exc: BaseException,
    policy: CooldownPolicy,
    now: float,
) -> ProviderFailure:
    code = _business_code(provider_error_body(exc))
    if code is None:
        return generic_failure_policy(exc, policy, now)
    status = provider_status_code(exc)
    retry_at = retry_after_timestamp(exc, now)
    if code in {"1302", "1305"}:
        return ProviderFailure(
            "failover",
            FailureCategory.RATE_LIMIT,
            CircuitScopeKind.ACCOUNT,
            status,
            code,
            retry_at or now + policy.outage_seconds,
        )
    if code in {"1308", "1310"}:
        seconds = policy.quota_seconds
    elif code in _FIVE_HOUR_CODES:
        seconds = 5 * 60 * 60
    elif code in _SEVEN_DAY_CODES:
        seconds = 7 * 24 * 60 * 60
    elif code in _ACCOUNT_RESTRICTION_CODES:
        seconds = policy.quota_seconds
    else:
        seconds = 0
    if seconds:
        return ProviderFailure(
            "failover",
            FailureCategory.QUOTA,
            CircuitScopeKind.ACCOUNT,
            status,
            code,
            retry_at or now + seconds,
        )
    if code == "1311":
        return ProviderFailure(
            "failover",
            FailureCategory.MODEL_UNAVAILABLE,
            CircuitScopeKind.MODEL,
            status,
            code,
            retry_at or now + policy.quota_seconds,
        )
    if code == "1113":
        return ProviderFailure(
            "failover",
            FailureCategory.QUOTA,
            CircuitScopeKind.ACCOUNT,
            status,
            code,
            retry_at or now + policy.outage_seconds,
        )
    return generic_failure_policy(exc, policy, now)


register_failure_adapter("zai", zai_failure_policy)
