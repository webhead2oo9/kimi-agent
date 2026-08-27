from __future__ import annotations

from datetime import UTC, datetime
from email.utils import format_datetime
from types import SimpleNamespace

import pytest

from providers.errors import (
    ProviderAvailabilityError,
    ProviderContextOverflowError,
    ProviderPolicyError,
)
from providers.failure_policy import (
    CircuitScopeKind,
    CooldownPolicy,
    FailureCategory,
    generic_failure_policy,
    get_failure_classifier,
    raise_for_terminal_finish_reason,
)


class _ProviderError(Exception):
    def __init__(
        self,
        status_code: int,
        *,
        headers: dict[str, str] | None = None,
        body: dict | None = None,
    ) -> None:
        super().__init__("private provider detail")
        self.status_code = status_code
        self.response = SimpleNamespace(status_code=status_code, headers=headers or {})
        self.body = body


def test_generic_retry_after_seconds_overrides_default() -> None:
    failure = generic_failure_policy(
        _ProviderError(429, headers={"retry-after": "12"}),
        CooldownPolicy(outage_seconds=1800),
        1000,
    )

    assert failure.disposition == "failover"
    assert failure.scope is CircuitScopeKind.ACCOUNT
    assert failure.retry_at == 1012


def test_generic_retry_after_http_date_overrides_default() -> None:
    retry_at = datetime.fromtimestamp(1120, UTC)
    failure = generic_failure_policy(
        _ProviderError(503, headers={"retry-after": format_datetime(retry_at)}),
        CooldownPolicy(outage_seconds=1800),
        1000,
    )

    assert failure.disposition == "retry"
    assert failure.retry_at == 1120


def test_zai_adapter_maps_quota_without_leaking_into_core() -> None:
    classifier = get_failure_classifier("zai")
    failure = classifier(
        _ProviderError(429, body={"error": {"code": 1316, "message": "secret"}}),
        CooldownPolicy(quota_seconds=18000),
        1000,
    )

    assert failure.category is FailureCategory.QUOTA
    assert failure.scope is CircuitScopeKind.ACCOUNT
    assert failure.provider_code == "1316"
    assert failure.retry_at == 1000 + 5 * 60 * 60


def test_zai_adapter_maps_seven_day_and_model_scopes() -> None:
    classifier = get_failure_classifier("zai")
    weekly = classifier(_ProviderError(429, body={"code": "1319"}), CooldownPolicy(), 1000)
    model = classifier(
        _ProviderError(429, body={"code": "1311"}),
        CooldownPolicy(quota_seconds=18000),
        1000,
    )

    assert weekly.retry_at == 1000 + 7 * 24 * 60 * 60
    assert model.scope is CircuitScopeKind.MODEL
    assert model.retry_at == 19000


def test_openai_compatible_terminal_reasons_are_normalized_generically() -> None:
    with pytest.raises(ProviderAvailabilityError):
        raise_for_terminal_finish_reason("network_error")
    with pytest.raises(ProviderContextOverflowError):
        raise_for_terminal_finish_reason("model_context_window_exceeded")
    with pytest.raises(ProviderPolicyError):
        raise_for_terminal_finish_reason("sensitive")
    raise_for_terminal_finish_reason("stop")
