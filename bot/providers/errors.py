from __future__ import annotations

from typing import Literal


class ProviderError(Exception):
    safe_message = "The model provider failed."


class ProviderAvailabilityError(ProviderError):
    safe_message = "The model provider is temporarily unavailable. Please try again shortly."


class ProviderCapabilityError(ProviderError):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.safe_message = message


class ProviderContextOverflowError(ProviderError):
    """Raised (or recognized) when a request exceeds the model's context window."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.safe_message = message


class ProviderPolicyError(ProviderError):
    safe_message = "The model provider could not complete that request."


class ProviderBackendAccessError(ProviderError):
    """A provider-confirmed access/model rejection that may use another backend."""

    safe_message = (
        "The selected model is unavailable to this bot right now. "
        "Please retry or contact the bot operator."
    )


class ProviderCircuitOpenError(ProviderError):
    safe_message = "The configured model providers are cooling down. Please try again later."


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


# Authentication, authorization, and model/endpoint lookup statuses receive a
# dedicated user message. Only the unambiguous subset is safe to send to another
# backend automatically; a generic 403 may instead be a policy/safety rejection.
_BACKEND_ACCESS_STATUS_CODES = frozenset({401, 403, 404})
ProviderFailureDisposition = Literal["stop", "retry", "failover"]


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
    # Import lazily: failure_policy builds on the public ProviderError hierarchy.
    from providers.failure_policy import CooldownPolicy, generic_failure_policy

    return generic_failure_policy(exc, CooldownPolicy(), 0).disposition


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
