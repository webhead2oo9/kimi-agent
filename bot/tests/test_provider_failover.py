from __future__ import annotations

import asyncio
from dataclasses import replace

import httpx
import pytest

from codex.transport import CodexWebSocketRequestError
from providers.base import LLMProvider
from providers.circuit_breaker import CircuitRecord, CircuitTarget, ProviderCircuitBreaker
from providers.errors import (
    ProviderBackendAccessError,
    ProviderCapabilityError,
    provider_failure_disposition,
)
from providers.failover import FailoverBackend, FailoverProvider
from providers.failure_policy import CircuitScopeKind
from providers.types import ProviderCapability, ProviderRequest, ProviderResponse


class _StatusError(Exception):
    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class _StubProvider(LLMProvider):
    def __init__(
        self,
        key: str,
        *,
        model: str = "model",
        capabilities: set[ProviderCapability] | None = None,
        error: BaseException | None = None,
        fail_times: int | None = None,
    ) -> None:
        self._key = key
        self._model = model
        self._capabilities = (
            capabilities
            if capabilities is not None
            else {ProviderCapability.TEXT, ProviderCapability.TOOL_CALLING}
        )
        self._error = error
        self._fail_times = fail_times
        self.calls = 0
        self.closed = False

    @property
    def provider_key(self) -> str:
        return self._key

    @property
    def model(self) -> str:
        return self._model

    @property
    def capabilities(self) -> set[ProviderCapability]:
        return set(self._capabilities)

    async def run_turn(self, request: ProviderRequest) -> ProviderResponse:
        self.calls += 1
        if self._error is not None and (self._fail_times is None or self.calls <= self._fail_times):
            raise self._error
        return ProviderResponse(content=f"{self._key}-response")

    async def close(self) -> None:
        self.closed = True


_REQUEST = ProviderRequest(
    conversation_id=1,
    system_prompt="",
    messages=[],
    current_user_parts=[],
    tools=[],
    max_tokens=128,
)


def test_returns_primary_result_when_healthy() -> None:
    primary = _StubProvider("primary")
    fallback = _StubProvider("fallback")
    chain = FailoverProvider([primary, fallback], retry_delay_seconds=0.0)

    result = asyncio.run(chain.run_turn(_REQUEST))

    assert result.content == "primary-response"
    assert primary.calls == 1
    assert fallback.calls == 0


@pytest.mark.parametrize(
    "error",
    [
        _StatusError("provider overloaded", 529),
        _StatusError("cloudflare tunnel down", 530),
        _StatusError("bad gateway", 502),
        _StatusError("rate limited", 429),
        ConnectionError("connection reset by peer"),
        TimeoutError("read timed out"),
        httpx.RemoteProtocolError("peer closed stream"),
        httpx.ReadError("read failed"),
        httpx.ConnectError("connect failed"),
        httpx.ReadTimeout("read timed out"),
        CodexWebSocketRequestError("websocket blip", retryable=True),
    ],
)
def test_fails_over_on_availability_errors(error: BaseException) -> None:
    primary = _StubProvider("primary", error=error)
    fallback = _StubProvider("fallback")
    chain = FailoverProvider([primary, fallback], retry_delay_seconds=0.0)

    result = asyncio.run(chain.run_turn(_REQUEST))

    # Every transient failure, including a bare 429, keeps the same-backend retry
    # before the circuit opens and the chain advances.
    assert result.content == "fallback-response"
    assert primary.calls == 2
    assert fallback.calls == 1


def test_walks_multiple_links_until_one_succeeds() -> None:
    first = _StubProvider("first", error=_StatusError("down", 503))
    second = _StubProvider("second", error=_StatusError("down", 500))
    third = _StubProvider("third")
    chain = FailoverProvider([first, second, third], retry_delay_seconds=0.0)

    result = asyncio.run(chain.run_turn(_REQUEST))

    assert result.content == "third-response"
    assert (first.calls, second.calls, third.calls) == (2, 2, 1)


def test_response_is_stamped_with_serving_backend_model() -> None:
    primary = _StubProvider("primary", model="glm-4.7", error=_StatusError("down", 503))
    fallback = _StubProvider("fallback", model="kimi-k2")
    chain = FailoverProvider([primary, fallback], retry_delay_seconds=0.0)

    result = asyncio.run(chain.run_turn(_REQUEST))

    # The stamp names the backend that actually served, not the chain's primary.
    assert result.content == "fallback-response"
    assert result.model == "kimi-k2"
    assert result.pricing_model == "kimi-k2"


def test_healthy_primary_response_is_stamped_too() -> None:
    chain = FailoverProvider(
        [_StubProvider("primary", model="glm-4.7"), _StubProvider("fallback", model="kimi-k2")],
        retry_delay_seconds=0.0,
    )

    result = asyncio.run(chain.run_turn(_REQUEST))

    assert result.model == "glm-4.7"
    assert result.pricing_model == "glm-4.7"


def test_stamp_keeps_model_a_backend_already_reported() -> None:
    class _SelfReporting(_StubProvider):
        async def run_turn(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(content="r", model="served-snapshot-2026")

    chain = FailoverProvider([_SelfReporting("primary", model="configured-alias")])

    result = asyncio.run(chain.run_turn(_REQUEST))

    # A backend that reports the exact served model wins over the configured alias.
    assert result.model == "served-snapshot-2026"
    assert result.pricing_model == "configured-alias"


def test_provider_state_is_only_replayed_to_the_backend_that_created_it() -> None:
    class _StatefulProvider(_StubProvider):
        def __init__(self, key: str, state: dict[str, str]) -> None:
            super().__init__(key)
            self.state = state
            self.request_states: list[dict] = []

        async def run_turn(self, request: ProviderRequest) -> ProviderResponse:
            self.request_states.append(dict(request.provider_state))
            self.calls += 1
            if self._error is not None:
                raise self._error
            return ProviderResponse(
                content=f"{self._key}-response",
                provider_state=dict(self.state),
            )

    primary = _StatefulProvider("primary", {"cursor": "primary"})
    fallback = _StatefulProvider("fallback", {"cursor": "fallback"})
    chain = FailoverProvider([primary, fallback], retry_delay_seconds=0.0)

    first = asyncio.run(chain.run_turn(_REQUEST))
    same_backend = asyncio.run(
        chain.run_turn(replace(_REQUEST, provider_state=first.provider_state))
    )
    assert primary.request_states[-1] == {"cursor": "primary"}

    primary._error = _StatusError("primary down", 503)
    fallback_response = asyncio.run(
        chain.run_turn(replace(_REQUEST, provider_state=same_backend.provider_state))
    )
    assert fallback.request_states[-1] == {}

    continued_fallback = asyncio.run(
        chain.run_turn(replace(_REQUEST, provider_state=fallback_response.provider_state))
    )
    assert fallback.request_states[-1] == {"cursor": "fallback"}

    primary._error = None
    primary_calls = primary.calls
    asyncio.run(chain.run_turn(replace(_REQUEST, provider_state=continued_fallback.provider_state)))
    assert primary.calls == primary_calls
    assert fallback.request_states[-1] == {"cursor": "fallback"}


def test_empty_provider_state_starts_fresh() -> None:
    primary = _StubProvider("primary")
    chain = FailoverProvider([primary])

    result = asyncio.run(chain.run_turn(replace(_REQUEST, provider_state={})))

    assert result.content == "primary-response"
    assert primary.calls == 1


@pytest.mark.parametrize(
    "state",
    [
        {"cursor": "old"},
        {"_failover_state": None},
        {"_failover_state": {}},
        {"_failover_state": {"provider_index": 0}},
        {"_failover_state": {"provider_state": {}}},
        {"_failover_state": {"provider_index": True, "provider_state": {}}},
        {"_failover_state": {"provider_index": -1, "provider_state": {}}},
        {"_failover_state": {"provider_index": 1, "provider_state": {}}},
        {"_failover_state": {"provider_index": 0, "provider_state": []}},
        {
            "_failover_state": {
                "provider_index": 0,
                "provider_state": {},
                "legacy": True,
            }
        },
        {
            "_failover_state": {"provider_index": 0, "provider_state": {}},
            "legacy": True,
        },
    ],
)
def test_bare_or_malformed_provider_state_is_rejected(state: dict[str, object]) -> None:
    primary = _StubProvider("primary")
    chain = FailoverProvider([primary])

    with pytest.raises(ValueError, match="[Ff]ailover provider state"):
        asyncio.run(chain.run_turn(replace(_REQUEST, provider_state=state)))

    assert primary.calls == 0


def test_retries_same_backend_before_failing_over() -> None:
    primary = _StubProvider("primary", error=_StatusError("blip", 503), fail_times=1)
    fallback = _StubProvider("fallback")
    chain = FailoverProvider([primary, fallback], retry_delay_seconds=0.0)

    result = asyncio.run(chain.run_turn(_REQUEST))

    # The retry lands on the primary (better model, warm prompt cache); the
    # fallback is never consulted for a one-off transient failure.
    assert result.content == "primary-response"
    assert primary.calls == 2
    assert fallback.calls == 0


def test_cancelled_half_open_call_releases_probe() -> None:
    class _CancelledProvider(_StubProvider):
        async def run_turn(self, request: ProviderRequest) -> ProviderResponse:
            raise asyncio.CancelledError

    target = CircuitTarget.create(
        model_identity="primary/model",
        account_identity="primary",
        label="primary/model",
    )
    record = CircuitRecord(
        target.model_scope_key,
        CircuitScopeKind.MODEL,
        "primary/model",
        "outage",
        503,
        None,
        1,
        2,
        1,
    )

    class _Store:
        async def load(self) -> list[CircuitRecord]:
            return [record]

        async def upsert(self, value: CircuitRecord) -> None:
            return None

        async def delete(self, scope_key: str) -> None:
            return None

        async def reset_all(self) -> None:
            return None

    breaker = ProviderCircuitBreaker(clock=lambda: 10)

    async def run() -> None:
        await breaker.initialize(_Store(), {target.model_scope_key, target.account_scope_key})
        chain = FailoverProvider(
            [_CancelledProvider("primary")],
            retry_delay_seconds=0,
            circuit_breaker=breaker,
            backends=[FailoverBackend(target)],
        )
        with pytest.raises(asyncio.CancelledError):
            await chain.run_turn(_REQUEST)
        assert await breaker.allow(target) is not None

    asyncio.run(run())


def test_no_failover_on_capability_error() -> None:
    primary = _StubProvider("primary", error=ProviderCapabilityError("no image input"))
    fallback = _StubProvider("fallback")
    chain = FailoverProvider([primary, fallback], retry_delay_seconds=0.0)

    with pytest.raises(ProviderCapabilityError):
        asyncio.run(chain.run_turn(_REQUEST))
    assert fallback.calls == 0


def test_no_failover_on_4xx_request_error() -> None:
    primary = _StubProvider("primary", error=_StatusError("bad request", 400))
    fallback = _StubProvider("fallback")
    chain = FailoverProvider([primary, fallback], retry_delay_seconds=0.0)

    with pytest.raises(_StatusError):
        asyncio.run(chain.run_turn(_REQUEST))
    assert fallback.calls == 0


@pytest.mark.parametrize("status_code", [401, 404])
def test_access_or_model_error_skips_retry_and_fails_over(status_code: int) -> None:
    primary = _StubProvider("primary", error=_StatusError("provider rejected", status_code))
    fallback = _StubProvider("fallback")
    chain = FailoverProvider([primary, fallback], retry_delay_seconds=0.0)

    result = asyncio.run(chain.run_turn(_REQUEST))

    assert result.content == "fallback-response"
    assert primary.calls == 1
    assert fallback.calls == 1


def test_ambiguous_403_does_not_fail_over() -> None:
    primary = _StubProvider("primary", error=_StatusError("policy or access rejection", 403))
    fallback = _StubProvider("fallback")
    chain = FailoverProvider([primary, fallback], retry_delay_seconds=0.0)

    with pytest.raises(_StatusError):
        asyncio.run(chain.run_turn(_REQUEST))

    assert primary.calls == 1
    assert fallback.calls == 0


def test_provider_recognized_access_error_fails_over() -> None:
    primary = _StubProvider("primary", error=ProviderBackendAccessError("recognized access"))
    fallback = _StubProvider("fallback")
    chain = FailoverProvider([primary, fallback], retry_delay_seconds=0.0)

    result = asyncio.run(chain.run_turn(_REQUEST))

    assert result.content == "fallback-response"
    assert primary.calls == 1
    assert fallback.calls == 1


def test_access_error_from_last_backend_propagates_without_retry() -> None:
    primary = _StubProvider("primary", error=_StatusError("primary rejected", 401))
    last_error = _StatusError("fallback rejected", 401)
    fallback = _StubProvider("fallback", error=last_error)
    chain = FailoverProvider([primary, fallback], retry_delay_seconds=0.0)

    with pytest.raises(_StatusError) as excinfo:
        asyncio.run(chain.run_turn(_REQUEST))

    assert excinfo.value is last_error
    assert primary.calls == 1
    assert fallback.calls == 1


def test_last_error_propagates_when_whole_chain_is_down() -> None:
    primary = _StubProvider("primary", error=_StatusError("primary down", 530))
    last_error = _StatusError("fallback down", 503)
    fallback = _StubProvider("fallback", error=last_error)
    chain = FailoverProvider([primary, fallback], retry_delay_seconds=0.0)

    with pytest.raises(_StatusError) as excinfo:
        asyncio.run(chain.run_turn(_REQUEST))
    assert excinfo.value is last_error
    assert primary.calls == 2
    assert fallback.calls == 2


def test_capabilities_are_chain_intersection() -> None:
    primary = _StubProvider(
        "primary",
        capabilities={
            ProviderCapability.TEXT,
            ProviderCapability.TOOL_CALLING,
            ProviderCapability.IMAGE_INPUT,
        },
    )
    fallback = _StubProvider(
        "fallback",
        capabilities={ProviderCapability.TEXT, ProviderCapability.TOOL_CALLING},
    )
    chain = FailoverProvider([primary, fallback], retry_delay_seconds=0.0)

    assert chain.capabilities == {ProviderCapability.TEXT, ProviderCapability.TOOL_CALLING}


def test_provider_key_and_model_reflect_chain() -> None:
    chain = FailoverProvider(
        [_StubProvider("kimi", model="Kimi-K2.6"), _StubProvider("codex", model="gpt-5.5")]
    )

    assert chain.provider_key == "failover[kimi+codex]"
    assert chain.model == "Kimi-K2.6"


def test_close_does_not_close_underlying_providers() -> None:
    primary = _StubProvider("primary")
    fallback = _StubProvider("fallback")
    chain = FailoverProvider([primary, fallback], retry_delay_seconds=0.0)

    asyncio.run(chain.close())

    assert primary.closed is False
    assert fallback.closed is False


def test_empty_chain_is_rejected() -> None:
    with pytest.raises(ValueError):
        FailoverProvider([])


def test_availability_classifier_excludes_typed_provider_errors() -> None:
    # Capability/overflow are deterministic; they must never trigger failover.
    assert provider_failure_disposition(ProviderCapabilityError("x")) == "stop"
    assert provider_failure_disposition(_StatusError("bad request", 400)) == "stop"
    assert provider_failure_disposition(_StatusError("teapot", 418)) == "stop"
    assert provider_failure_disposition(_StatusError("server error", 500)) == "retry"
    assert (
        provider_failure_disposition(CodexWebSocketRequestError("bad request", retryable=False))
        == "stop"
    )


@pytest.mark.parametrize("status_code", range(500, 600))
def test_availability_classifier_includes_every_5xx(status_code: int) -> None:
    assert provider_failure_disposition(_StatusError("server error", status_code)) == "retry"


def test_httpx_status_errors_only_fail_over_for_availability_statuses() -> None:
    request = httpx.Request("POST", "https://provider.test/messages")
    bad_request = httpx.HTTPStatusError(
        "bad request",
        request=request,
        response=httpx.Response(400, request=request),
    )
    unavailable = httpx.HTTPStatusError(
        "unavailable",
        request=request,
        response=httpx.Response(503, request=request),
    )

    assert provider_failure_disposition(bad_request) == "stop"
    assert provider_failure_disposition(unavailable) == "retry"
