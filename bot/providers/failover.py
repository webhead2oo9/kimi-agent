from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from typing import Any

from providers.base import LLMProvider
from providers.errors import ProviderCapabilityError, provider_failure_disposition
from providers.types import (
    ProviderCapability,
    ProviderRequest,
    ProviderResponse,
    ReasoningEscalation,
)

log = logging.getLogger(__name__)

__all__ = ["FailoverProvider"]

# Attempts per backend before advancing: the initial call plus one retry. The
# retry goes to the SAME backend first because the primary is usually the
# preferred model and keeps any server-side prompt cache warm; the next backend
# is only tried once the retry has also failed.
_ATTEMPTS_PER_BACKEND = 2
_RETRY_DELAY_SECONDS = 2.0
_STATE_ENVELOPE_KEY = "_failover_state"


class FailoverProvider(LLMProvider):
    """Ordered chain of providers tried in sequence on availability failures.

    `run_turn` calls the primary; if it raises a transient availability error
    (connection drops, timeouts, 429, 5xx)
    the same backend is retried once after a short pause, and only if the retry
    also fails does the chain advance to the next backend (which gets the same
    two attempts, including the last one). Unambiguous backend-specific access/model
    failures (401, 404), plus provider-recognized ``ProviderBackendAccessError``
    instances, skip the same-backend retry and advance immediately because a
    separately configured fallback may still be usable. Ambiguous 403 responses do
    not fail over. The last backend's final error propagates unchanged so the
    caller's scrubbed-error path still fires.
    Deterministic request failures (bad request, capability mismatch, context
    overflow) propagate immediately because another backend should reject them too.

    Capabilities are the intersection across the chain: the agent core validates
    and shapes each request against `capabilities`, so it must only ever promise
    what *every* backend can honor (a turn that started on the primary could be
    retried on a fallback mid-failure).

    The chain's providers are owned by whoever built them (the ProviderManager
    cache); this wrapper never closes them, so `close()` is intentionally a no-op.
    """

    def __init__(
        self,
        providers: list[LLMProvider],
        *,
        attempts_per_backend: int = _ATTEMPTS_PER_BACKEND,
        retry_delay_seconds: float = _RETRY_DELAY_SECONDS,
    ) -> None:
        if not providers:
            raise ValueError("FailoverProvider requires at least one provider")
        if attempts_per_backend < 1:
            raise ValueError("attempts_per_backend must be at least 1")
        self._providers = list(providers)
        self._attempts_per_backend = attempts_per_backend
        self._retry_delay_seconds = retry_delay_seconds

    @property
    def provider_key(self) -> str:
        keys = "+".join(p.provider_key for p in self._providers)
        return f"failover[{keys}]"

    @property
    def model(self) -> str:
        return self._providers[0].model

    @property
    def capabilities(self) -> set[ProviderCapability]:
        caps = set(self._providers[0].capabilities)
        for provider in self._providers[1:]:
            caps &= provider.capabilities
        return caps

    @property
    def reasoning_escalations(self) -> tuple[ReasoningEscalation, ...]:
        # Routing policy belongs to the selected primary model. A fallback that
        # cannot adjust reasoning simply ignores ProviderRequest.reasoning_effort.
        return self._providers[0].reasoning_escalations

    async def run_turn(self, request: ProviderRequest) -> ProviderResponse:
        state_owner, provider_state = self._unwrap_provider_state(request.provider_state)
        last_index = len(self._providers) - 1
        for index, provider in enumerate(self._providers):
            for attempt in range(1, self._attempts_per_backend + 1):
                try:
                    backend_request = replace(
                        request,
                        provider_state=(provider_state if state_owner == index else {}),
                    )
                    response = await provider.run_turn(backend_request)
                    # Stamp the serving backend so downstream (usage attribution,
                    # the observability stream) can tell a fallback-served response
                    # from the primary; keep a more specific model a backend
                    # already reported.
                    return replace(
                        response,
                        model=response.model or provider.model,
                        pricing_model=response.pricing_model or provider.model,
                        provider_state=self._wrap_provider_state(
                            index,
                            response.provider_state,
                        ),
                    )
                except ProviderCapabilityError:
                    raise
                except Exception as exc:
                    disposition = provider_failure_disposition(exc)
                    if disposition == "stop":
                        raise
                    if disposition == "retry" and attempt < self._attempts_per_backend:
                        log.warning(
                            "provider %s unavailable (%s: %s); retrying same backend "
                            "(attempt %d/%d)",
                            provider.provider_key,
                            type(exc).__name__,
                            exc,
                            attempt + 1,
                            self._attempts_per_backend,
                        )
                        if self._retry_delay_seconds > 0:
                            await asyncio.sleep(self._retry_delay_seconds)
                        continue
                    if index == last_index:
                        raise
                    nxt = self._providers[index + 1]
                    if disposition == "failover":
                        log.warning(
                            "provider %s rejected this request (%s: %s); failing over to %s",
                            provider.provider_key,
                            type(exc).__name__,
                            exc,
                            nxt.provider_key,
                        )
                    else:
                        log.warning(
                            "provider %s unavailable after %d attempts (%s: %s); "
                            "failing over to %s",
                            provider.provider_key,
                            self._attempts_per_backend,
                            type(exc).__name__,
                            exc,
                            nxt.provider_key,
                        )
                    break
        # Unreachable: the loop either returns or re-raises on the last backend.
        raise RuntimeError("FailoverProvider exhausted its chain without returning")

    @staticmethod
    def _unwrap_provider_state(
        state: dict[str, Any],
    ) -> tuple[int, dict[str, Any]]:
        envelope = state.get(_STATE_ENVELOPE_KEY)
        if not isinstance(envelope, dict):
            # A bare state predates the envelope or was supplied directly by a
            # caller. Preserve compatibility by treating it as primary-owned.
            return 0, dict(state)
        owner = envelope.get("provider_index")
        provider_state = envelope.get("provider_state")
        if not isinstance(owner, int) or not isinstance(provider_state, dict):
            return 0, {}
        return owner, dict(provider_state)

    @staticmethod
    def _wrap_provider_state(
        provider_index: int,
        state: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            _STATE_ENVELOPE_KEY: {
                "provider_index": provider_index,
                "provider_state": dict(state),
            }
        }

    async def close(self) -> None:
        # No-op: underlying providers are owned and closed by the ProviderManager.
        return
