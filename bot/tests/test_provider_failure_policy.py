from __future__ import annotations

from datetime import UTC, datetime
from email.utils import format_datetime
from types import SimpleNamespace

import httpx
import pytest

from codex.transport import CodexWebSocketRequestError
from providers.errors import (
    ProviderAvailabilityError,
    ProviderBackendAccessError,
    ProviderCapabilityError,
    ProviderContextOverflowError,
    ProviderPolicyError,
)
from providers.failure_policy import (
    CircuitScopeKind,
    CooldownPolicy,
    FailureCategory,
    FailureDisposition,
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


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (_ProviderError(429), "retry"),
        (_ProviderError(401), "failover"),
        (_ProviderError(404), "failover"),
        (_ProviderError(403), "stop"),
        (_ProviderError(400), "stop"),
        (_ProviderError(418), "stop"),
        (_ProviderError(499), "stop"),
        (_ProviderError(600), "stop"),
        (ProviderBackendAccessError("recognized"), "failover"),
        (ProviderCapabilityError("unsupported"), "stop"),
        (ProviderContextOverflowError("too long"), "stop"),
        (CodexWebSocketRequestError("bad request", retryable=False), "stop"),
    ],
)
def test_generic_failure_dispositions(error: BaseException, expected: FailureDisposition) -> None:
    assert generic_failure_policy(error, CooldownPolicy(), 0).disposition == expected


@pytest.mark.parametrize("status_code", [500, 502, 503, 599])
def test_generic_failure_retries_server_errors_including_range_boundaries(status_code: int) -> None:
    failure = generic_failure_policy(_ProviderError(status_code), CooldownPolicy(), 0)

    assert failure.disposition == "retry"


@pytest.mark.parametrize(("status_code", "expected"), [(400, "stop"), (503, "retry")])
def test_httpx_status_errors_use_response_status(status_code, expected) -> None:
    request = httpx.Request("POST", "https://provider.test/messages")
    error = httpx.HTTPStatusError(
        "private provider detail",
        request=request,
        response=httpx.Response(status_code, request=request),
    )

    assert generic_failure_policy(error, CooldownPolicy(), 0).disposition == expected


def test_generic_retry_after_seconds_overrides_default() -> None:
    failure = generic_failure_policy(
        _ProviderError(429, headers={"retry-after": "12"}),
        CooldownPolicy(outage_seconds=1800),
        1000,
    )

    assert failure.disposition == "failover"
    assert failure.category is FailureCategory.RATE_LIMIT
    assert failure.scope is CircuitScopeKind.MODEL
    assert failure.retry_at == 1012


def test_generic_bare_rate_limit_retries_with_short_model_cooldown() -> None:
    failure = generic_failure_policy(
        _ProviderError(429),
        CooldownPolicy(outage_seconds=300, rate_limit_seconds=45),
        1000,
    )

    assert failure.disposition == "retry"
    assert failure.category is FailureCategory.RATE_LIMIT
    assert failure.scope is CircuitScopeKind.MODEL
    assert failure.retry_at == 1045


def test_codex_stream_rate_limit_uses_structured_retry_metadata() -> None:
    failure = generic_failure_policy(
        CodexWebSocketRequestError(
            "private detail",
            status_code=429,
            code="rate_limit_exceeded",
            retry_after_seconds=2.5,
        ),
        CooldownPolicy(rate_limit_seconds=45),
        1000,
    )

    assert failure.disposition == "failover"
    assert failure.category is FailureCategory.RATE_LIMIT
    assert failure.scope is CircuitScopeKind.MODEL
    assert failure.provider_code == "rate_limit_exceeded"
    assert failure.retry_at == 1002.5


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
    with pytest.raises(ProviderPolicyError):
        raise_for_terminal_finish_reason("content_filter")
    raise_for_terminal_finish_reason("stop")
