from __future__ import annotations

import logging
from typing import Any

from branding import DEFAULT_BOT_NAME
from providers.base import LLMProvider
from providers.errors import ProviderAvailabilityError, ProviderBackendAccessError
from providers.failure_policy import provider_error_body, provider_status_code
from providers.openai_responses import OpenAIResponsesProvider
from providers.types import ProviderCapability, ProviderRequest, ProviderResponse
from xai.auth import XAI_API_BASE_URL, XaiAuthError, XaiAuthRevokedError
from xai.credentials import AUTH_MODE_API_KEY, AUTH_MODE_OAUTH, XaiCredentialResolver

log = logging.getLogger(__name__)

_ENTITLEMENT_MARKERS = (
    "entitlement",
    "insufficient_scope",
    "insufficient scope",
    "subscription",
    "not entitled",
    "tier denied",
)


class XaiProvider(LLMProvider):
    """xAI Responses provider with request-scoped OAuth/API credential selection."""

    def __init__(
        self,
        *,
        credential_resolver: XaiCredentialResolver,
        model: str,
        reasoning_effort: str = "",
        timeout_seconds: float | None = None,
        user_agent: str = DEFAULT_BOT_NAME,
    ) -> None:
        self._credentials = credential_resolver
        self._model = model
        self._reasoning_effort = reasoning_effort
        self._timeout_seconds = timeout_seconds
        self._user_agent = user_agent

    @property
    def provider_key(self) -> str:
        return "xai"

    @property
    def model(self) -> str:
        return self._model

    @property
    def base_url(self) -> str:
        return XAI_API_BASE_URL

    @property
    def capabilities(self) -> set[ProviderCapability]:
        return {
            ProviderCapability.TEXT,
            ProviderCapability.IMAGE_INPUT,
            ProviderCapability.TOOL_CALLING,
        }

    async def run_turn(self, request: ProviderRequest) -> ProviderResponse:
        try:
            credential = await self._credentials.primary()
        except XaiAuthRevokedError as exc:
            raise ProviderBackendAccessError(str(exc)) from exc
        except XaiAuthError as exc:
            # Only a revoked credential is a real access rejection. A transient
            # token-endpoint outage must stay retryable: the backend-access class
            # fails the turn over to the next model, which is a different billing
            # source, and opens a persisted circuit for the whole cooldown.
            raise ProviderAvailabilityError(str(exc)) from exc

        refreshed = False
        api_fallback_used = credential.source == AUTH_MODE_API_KEY
        while True:
            provider = OpenAIResponsesProvider(
                api_key=credential.bearer,
                base_url=XAI_API_BASE_URL,
                model=self._model,
                reasoning_effort=self._reasoning_effort,
                timeout_seconds=self._timeout_seconds,
                user_agent=self._user_agent,
            )
            try:
                return await provider.run_turn(request)
            except Exception as exc:
                status = provider_status_code(exc)
                replacement = None
                if status == 401 and credential.source == AUTH_MODE_OAUTH:
                    if not refreshed:
                        refreshed = True
                        try:
                            replacement = await self._credentials.after_unauthorized(credential)
                        except XaiAuthRevokedError as auth_exc:
                            raise ProviderBackendAccessError(str(auth_exc)) from auth_exc
                        except XaiAuthError as auth_exc:
                            raise ProviderAvailabilityError(str(auth_exc)) from auth_exc
                    elif not api_fallback_used:
                        replacement = self._credentials.api_key_fallback()
                elif (
                    credential.source == AUTH_MODE_OAUTH
                    and not api_fallback_used
                    and _is_entitlement_error(exc)
                ):
                    replacement = self._credentials.api_key_fallback()

                if replacement is None:
                    if status == 401 or _is_entitlement_error(exc):
                        raise ProviderBackendAccessError(
                            "The configured xAI credential cannot access this model."
                        ) from exc
                    raise
                api_fallback_used = replacement.source == AUTH_MODE_API_KEY
                if api_fallback_used:
                    log.info("xAI model request falling back from OAuth to GROK_API_KEY")
                credential = replacement
            finally:
                await provider.close()


def _is_entitlement_error(exc: BaseException) -> bool:
    if provider_status_code(exc) != 403:
        return False
    body = provider_error_body(exc)
    values: list[Any] = []
    if body:
        values.extend(body.values())
        nested = body.get("error")
        if isinstance(nested, dict):
            values.extend(nested.values())
    text = " ".join(str(value).lower() for value in values)
    return any(marker in text for marker in _ENTITLEMENT_MARKERS)
