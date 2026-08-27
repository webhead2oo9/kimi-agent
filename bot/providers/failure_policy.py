from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC
from email.utils import parsedate_to_datetime
from enum import StrEnum
from typing import Any, Literal

import httpx

from providers.errors import (
    ProviderAvailabilityError,
    ProviderBackendAccessError,
    ProviderContextOverflowError,
    ProviderError,
    ProviderPolicyError,
)


class FailureCategory(StrEnum):
    OUTAGE = "outage"
    RATE_LIMIT = "rate_limit"
    QUOTA = "quota"
    AUTH = "auth"
    MODEL_UNAVAILABLE = "model_unavailable"
    DETERMINISTIC = "deterministic"


class CircuitScopeKind(StrEnum):
    MODEL = "model"
    ACCOUNT = "account"


FailureDisposition = Literal["stop", "retry", "failover"]


@dataclass(frozen=True, slots=True)
class CooldownPolicy:
    outage_seconds: float = 300.0
    quota_seconds: float = 1800.0
    rate_limit_seconds: float = 60.0


@dataclass(frozen=True, slots=True)
class ProviderFailure:
    disposition: FailureDisposition
    category: FailureCategory
    scope: CircuitScopeKind | None = None
    status_code: int | None = None
    provider_code: str | None = None
    retry_at: float | None = None


FailureClassifier = Callable[[BaseException, CooldownPolicy, float], ProviderFailure]


_ADAPTERS: dict[str, FailureClassifier] = {}
_AVAILABILITY_EXCEPTION_NAMES = frozenset(
    {
        "APIConnectionError",
        "APIConnectionTimeoutError",
        "APITimeoutError",
        "InternalServerError",
        "ServiceUnavailableError",
        "RateLimitError",
    }
)


def register_failure_adapter(name: str, classifier: FailureClassifier) -> None:
    if not name or name in _ADAPTERS:
        raise ValueError(f"duplicate or empty failure adapter {name!r}")
    _ADAPTERS[name] = classifier


def failure_adapter_names() -> frozenset[str]:
    _load_builtin_adapters()
    return frozenset(_ADAPTERS)


def get_failure_classifier(name: str) -> FailureClassifier:
    _load_builtin_adapters()

    try:
        return _ADAPTERS[name]
    except KeyError as exc:
        raise ValueError(f"unknown failure adapter {name!r}") from exc


def _load_builtin_adapters() -> None:
    if "zai" not in _ADAPTERS:
        import providers.zai_failure_policy  # noqa: F401


def raise_for_terminal_finish_reason(finish_reason: str | None) -> None:
    """Normalize semantically explicit OpenAI-compatible terminal reasons."""
    if finish_reason == "network_error":
        raise ProviderAvailabilityError("provider stream ended with a network error")
    if finish_reason == "model_context_window_exceeded":
        raise ProviderContextOverflowError("The request exceeded the model context window.")
    if finish_reason in {"content_filter", "sensitive"}:
        raise ProviderPolicyError("The model provider rejected the request.")


def provider_status_code(exc: BaseException) -> int | None:
    status = getattr(exc, "status_code", None)
    if not isinstance(status, int):
        status = getattr(exc, "status", None)
    if not isinstance(status, int):
        status = getattr(getattr(exc, "response", None), "status_code", None)
    return status if isinstance(status, int) else None


def provider_error_body(exc: BaseException) -> dict[str, Any] | None:
    body = getattr(exc, "body", None)
    return body if isinstance(body, dict) else None


def _header(exc: BaseException, name: str) -> str | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    value = headers.get(name)
    return str(value).strip() if value is not None else None


def retry_after_timestamp(exc: BaseException, now: float) -> float | None:
    seconds = getattr(exc, "retry_after_seconds", None)
    if isinstance(seconds, int | float) and not isinstance(seconds, bool) and seconds >= 0:
        return now + float(seconds)
    value = _header(exc, "retry-after")
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except TypeError, ValueError, OverflowError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return max(now, parsed.timestamp())
    if seconds < 0:
        return None
    return now + seconds


def generic_failure_policy(
    exc: BaseException,
    policy: CooldownPolicy,
    now: float,
) -> ProviderFailure:
    if isinstance(exc, ProviderBackendAccessError):
        return ProviderFailure(
            "failover",
            FailureCategory.MODEL_UNAVAILABLE,
            CircuitScopeKind.MODEL,
            retry_at=now + policy.outage_seconds,
        )
    if isinstance(exc, ProviderAvailabilityError):
        return ProviderFailure(
            "retry",
            FailureCategory.OUTAGE,
            CircuitScopeKind.MODEL,
            retry_at=now + policy.outage_seconds,
        )
    if isinstance(exc, ProviderError):
        return ProviderFailure("stop", FailureCategory.DETERMINISTIC)

    status = provider_status_code(exc)
    retry_at = retry_after_timestamp(exc, now)
    if status == 429:
        # A bare 429 is usually a burst limit on this endpoint, not an account-wide
        # quota, so it keeps the same-backend retry and a short model-scope cooldown.
        # Only an explicit Retry-After justifies advancing immediately; account-scope
        # decisions are left to adapters that see structured provider evidence.
        return ProviderFailure(
            "failover" if retry_at is not None else "retry",
            FailureCategory.RATE_LIMIT,
            CircuitScopeKind.MODEL,
            status_code=status,
            provider_code=(str(code) if (code := getattr(exc, "code", None)) is not None else None),
            retry_at=retry_at or now + policy.rate_limit_seconds,
        )
    if status == 401:
        return ProviderFailure(
            "failover",
            FailureCategory.AUTH,
            CircuitScopeKind.ACCOUNT,
            status_code=status,
            retry_at=now + policy.quota_seconds,
        )
    if status == 404:
        return ProviderFailure(
            "failover",
            FailureCategory.MODEL_UNAVAILABLE,
            CircuitScopeKind.MODEL,
            status_code=status,
            retry_at=now + policy.outage_seconds,
        )
    if status in {408, 425} or (status is not None and 500 <= status <= 599):
        return ProviderFailure(
            "retry",
            FailureCategory.OUTAGE,
            CircuitScopeKind.MODEL,
            status_code=status,
            retry_at=retry_at or now + policy.outage_seconds,
        )
    if status is not None:
        return ProviderFailure("stop", FailureCategory.DETERMINISTIC, status_code=status)
    if (
        isinstance(exc, (ConnectionError, TimeoutError, httpx.HTTPError))
        or getattr(exc, "retryable", False) is True
        or type(exc).__name__ in _AVAILABILITY_EXCEPTION_NAMES
    ):
        return ProviderFailure(
            "retry",
            FailureCategory.OUTAGE,
            CircuitScopeKind.MODEL,
            retry_at=now + policy.outage_seconds,
        )
    return ProviderFailure("stop", FailureCategory.DETERMINISTIC)


register_failure_adapter("generic", generic_failure_policy)
