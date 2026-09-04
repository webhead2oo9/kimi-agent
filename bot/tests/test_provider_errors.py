from types import SimpleNamespace

import pytest

from providers.errors import (
    ProviderContextOverflowError,
    ProviderError,
    is_context_overflow_error,
    provider_status_code,
    safe_provider_error_message,
)
from providers.failure_policy import CooldownPolicy, generic_failure_policy


class _StatusError(Exception):
    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


def test_overflow_error_carries_safe_message():
    err = ProviderContextOverflowError("context window exceeded")
    assert err.safe_message == "context window exceeded"
    assert isinstance(err, ProviderError)


def test_is_context_overflow_matches_typed_error():
    assert is_context_overflow_error(ProviderContextOverflowError("x")) is True


def test_is_context_overflow_matches_known_markers():
    assert (
        is_context_overflow_error(Exception("This model's maximum context length is 256000 tokens"))
        is True
    )
    assert is_context_overflow_error(Exception("context_length_exceeded")) is True
    assert (
        is_context_overflow_error(ValueError("prompt is too long: 300000 tokens > 256000")) is True
    )


def test_is_context_overflow_ignores_unrelated_errors():
    assert is_context_overflow_error(Exception("connection reset by peer")) is False
    assert is_context_overflow_error(TimeoutError("timed out")) is False


def test_safe_provider_error_message_is_actionable_and_scrubbed():
    message = safe_provider_error_message(
        _StatusError("account secret at /run/secrets/provider", 403),
        tool_actions_completed=True,
    )

    assert "selected model is unavailable" in message
    assert "Earlier tool actions may already have completed" in message
    assert "account secret" not in message
    assert "/run/secrets/provider" not in message
    assert "403" not in message


def test_untrusted_safe_message_attribute_is_not_exposed():
    error = _StatusError("private SDK detail", 500)
    error.safe_message = "secret account detail"

    message = safe_provider_error_message(error)

    assert message == "The model provider is temporarily unavailable. Please try again shortly."
    assert "secret account detail" not in message


@pytest.mark.parametrize(
    ("attributes", "expected"),
    [
        ({"status_code": 401, "status": 503}, 401),
        ({"status_code": "invalid", "status": 503}, 503),
        ({"status": 403}, 403),
        ({"response": SimpleNamespace(status_code=429)}, 429),
        ({"status_code": True, "response": SimpleNamespace(status_code=503)}, 503),
        ({"status_code": False}, None),
        ({}, None),
    ],
)
def test_provider_status_sources_agree_for_policy_and_safe_messages(attributes, expected):
    error = Exception("private provider detail")
    error.__dict__.update(attributes)

    assert provider_status_code(error) == expected
    failure = generic_failure_policy(error, CooldownPolicy(), 0)
    assert failure.status_code == expected
    message = safe_provider_error_message(error)
    assert "private provider detail" not in message
    if expected == 503:
        assert failure.disposition == "retry"
        assert message == "The model provider is temporarily unavailable. Please try again shortly."
