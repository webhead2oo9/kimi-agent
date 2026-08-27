from providers.errors import (
    ProviderBackendAccessError,
    ProviderContextOverflowError,
    ProviderError,
    is_context_overflow_error,
    provider_failure_disposition,
    safe_provider_error_message,
)


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


def test_provider_failure_disposition_separates_retry_failover_and_stop():
    assert provider_failure_disposition(_StatusError("busy", 429)) == "retry"
    assert provider_failure_disposition(_StatusError("unauthorized", 401)) == "failover"
    assert provider_failure_disposition(_StatusError("sparse", 403)) == "stop"
    assert provider_failure_disposition(ProviderBackendAccessError("recognized")) == "failover"
    assert provider_failure_disposition(_StatusError("bad payload", 400)) == "stop"
    assert provider_failure_disposition(ProviderContextOverflowError("too long")) == "stop"


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
