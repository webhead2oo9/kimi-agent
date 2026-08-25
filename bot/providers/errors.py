from __future__ import annotations

from typing import Literal

import httpx


class ProviderError(Exception):
    safe_message = "The model provider failed."


class ProviderCapabilityError(ProviderError):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.safe_message = message


class ProviderContextOverflowError(ProviderError):
    """Raised (or recognized) when a request exceeds the model's context window."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.safe_message = message


class ProviderBackendAccessError(ProviderError):
    """A provider-confirmed access/model rejection that may use another backend."""

    safe_message = (
        "The selected model is unavailable to this bot right now. "
        "Please retry or contact the bot operator."
    )


# Lowercased substrings that reliably indicate a context-window overflow across the
# OpenAI-compatible, Anthropic, and OpenRouter error surfaces this bot uses.
_OVERFLOW_MARKERS = (
    "context_length_exceeded",
    "maximum context length",
    "maximum context",
    "context window",
    "context length",
    "too many tokens",
    "prompt is too long",
    "reduce the length",
    "input is too long",
)


def is_context_overflow_error(exc: BaseException) -> bool:
    """True if `exc` is (or reads as) a model context-window overflow.

    Recognizes the typed `ProviderContextOverflowError` and heuristically matches the
    common provider error strings. Heuristic by design; providers may raise the typed
    error for precision while this keeps detection working without SDK-specific edits.
    """
    if isinstance(exc, ProviderContextOverflowError):
        return True
    text = str(exc).lower()
    return any(marker in text for marker in _OVERFLOW_MARKERS)


# HTTP status codes that mean "this endpoint is unavailable right now, retry it"
# rather than "your request is wrong". 429 (rate limited) and every 5xx qualify,
# including the Cloudflare-specific 520-530 tunnel/origin codes a Cloudflare-fronted
# gateway emits.
_AVAILABILITY_STATUS_CODES = frozenset({408, 425, 429})

# Authentication, authorization, and model/endpoint lookup statuses receive a
# dedicated user message. Only the unambiguous subset is safe to send to another
# backend automatically; a generic 403 may instead be a policy/safety rejection.
_BACKEND_ACCESS_STATUS_CODES = frozenset({401, 403, 404})
_UNAMBIGUOUS_FAILOVER_STATUS_CODES = frozenset({401, 404})

ProviderFailureDisposition = Literal["stop", "retry", "failover"]

# SDK exception class names that signal transport-level unavailability but may not carry
# a numeric status_code (connection drops, read timeouts). Matched by name so this stays
# free of hard SDK imports, mirroring the heuristic style of is_context_overflow_error.
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


def _provider_status_code(exc: BaseException) -> int | None:
    status = getattr(exc, "status_code", None)
    if not isinstance(status, int):
        status = getattr(exc, "status", None)
    if not isinstance(status, int):
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


def provider_failure_disposition(exc: BaseException) -> ProviderFailureDisposition:
    """Classify whether a provider failure should stop, retry, or fail over.

    ``retry`` means retry the same backend before advancing. ``failover`` means the
    failure is backend-specific but deterministic there, so skip a pointless retry
    and try a separately configured fallback. ``stop`` means another backend is
    expected to reject the same request too.

    ``ProviderBackendAccessError`` is the trusted opt-in to failover. Other typed
    ``ProviderError`` instances stop because capability and context-overflow failures
    have dedicated handling. The remaining checks intentionally avoid SDK imports so
    OpenAI-compatible and other providers share one policy.
    """
    if isinstance(exc, ProviderBackendAccessError):
        return "failover"
    if isinstance(exc, ProviderError):
        return "stop"
    status = _provider_status_code(exc)
    if isinstance(status, int):
        if status in _AVAILABILITY_STATUS_CODES or 500 <= status <= 599:
            return "retry"
        if status in _UNAMBIGUOUS_FAILOVER_STATUS_CODES:
            return "failover"
        return "stop"
    if isinstance(exc, (ConnectionError, TimeoutError, httpx.HTTPError)):
        return "retry"
    if getattr(exc, "retryable", False) is True:
        return "retry"
    if type(exc).__name__ in _AVAILABILITY_EXCEPTION_NAMES:
        return "retry"
    return "stop"


def is_provider_availability_error(exc: BaseException) -> bool:
    """True when retrying the same backend may recover the request."""
    return provider_failure_disposition(exc) == "retry"


def safe_provider_error_message(
    exc: BaseException,
    *,
    tool_actions_completed: bool = False,
) -> str:
    """Return an actionable user message without exposing provider error details."""
    explicit = getattr(exc, "safe_message", None) if isinstance(exc, ProviderError) else None
    if isinstance(explicit, str) and explicit.strip():
        message = explicit
    else:
        status = _provider_status_code(exc)
        if status in _BACKEND_ACCESS_STATUS_CODES:
            message = (
                "The selected model is unavailable to this bot right now. "
                "Please retry or contact the bot operator."
            )
        elif status == 429:
            message = "The model provider is busy right now. Please try again shortly."
        elif provider_failure_disposition(exc) == "retry":
            message = "The model provider is temporarily unavailable. Please try again shortly."
        else:
            message = "Sorry, I hit an internal error reaching the model. Please try again."

    if tool_actions_completed:
        message += (
            " Earlier tool actions may already have completed, so check their result "
            "before repeating them."
        )
    return message
