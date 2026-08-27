from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from app.tools import _register_wolfram_alpha
from config.settings import Settings
from tools import wolfram_alpha as wolfram_alpha_module
from tools.registry import MessageContext, ToolRegistry
from tools.wolfram_alpha import (
    MAX_INPUT_WORDS,
    TOOL_NAME,
    WolframAlphaClient,
    WolframAlphaConfig,
    WolframAlphaResponse,
    init_wolfram_alpha_tool,
    request_llm_result,
)
from trust.tiers import TrustTier


class RecordingRequest:
    def __init__(self, response: WolframAlphaResponse | Exception) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> WolframAlphaResponse:
        self.calls.append(kwargs)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class RecordingUsageStore:
    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def record_paid_usage(self, **kwargs: Any) -> None:
        self.calls.extend(kwargs["calls"])


class FakeHttpContent:
    def __init__(self, text: str) -> None:
        self._body = text.encode()

    async def iter_chunked(self, size: int):
        for offset in range(0, len(self._body), size):
            yield self._body[offset : offset + size]


class FakeHttpResponse:
    charset = "utf-8"

    def __init__(self, status: int, text: str) -> None:
        self.status = status
        self.content = FakeHttpContent(text)

    async def __aenter__(self) -> FakeHttpResponse:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None


class FakeClientSession:
    def __init__(self, response: FakeHttpResponse, calls: list[dict[str, Any]]) -> None:
        self._response = response
        self._calls = calls

    async def __aenter__(self) -> FakeClientSession:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    def get(self, url: str, **kwargs: Any) -> FakeHttpResponse:
        self._calls.append({"url": url, **kwargs})
        return self._response


class FakeSessionFactory:
    def __init__(self, *responses: FakeHttpResponse) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> FakeClientSession:
        return FakeClientSession(self._responses.pop(0), self.calls)


def _context(*, usage_store: Any = None) -> MessageContext:
    return MessageContext(
        user_id="u1",
        user_name="Tester",
        guild_id="g1",
        channel_id="c1",
        thread_id=None,
        trust_tier=TrustTier.MEMBER,
        usage_store=usage_store,
        activated_tools={TOOL_NAME},
    )


def _registry(
    request: RecordingRequest,
    *,
    limit: int = 3,
    max_output_chars: int = 6_800,
    cost: float | None = None,
) -> ToolRegistry:
    registry = ToolRegistry()
    init_wolfram_alpha_tool(
        registry,
        WolframAlphaConfig(
            client=WolframAlphaClient("secret-app-id", request=request),
            max_calls_per_turn=limit,
            max_output_chars=max_output_chars,
            timeout_seconds=1.0,
            call_cost_usd=cost,
        ),
    )
    return registry


def test_tool_is_searchable_member_computation_surface() -> None:
    registry = _registry(RecordingRequest(WolframAlphaResponse(200, "4")))
    entry = next(item for item in registry.get_all_tools() if item.name == TOOL_NAME)

    assert entry.searchable is True
    assert entry.min_tier is TrustTier.MEMBER
    assert entry.category == "Computation"
    assert entry.parameters["required"] == ["input"]
    assert entry.parameters["properties"]["units"]["enum"] == ["metric", "nonmetric"]


def test_registration_follows_environment_app_id() -> None:
    disabled = ToolRegistry()
    _register_wolfram_alpha(
        Settings(_env_file=None, wolfram_alpha_app_id=SecretStr("")),  # type: ignore[call-arg]
        disabled,
    )
    assert not disabled.is_registered(TOOL_NAME)

    enabled = ToolRegistry()
    _register_wolfram_alpha(
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            wolfram_alpha_app_id=SecretStr("wa-secret"),
        ),
        enabled,
    )
    assert enabled.is_registered(TOOL_NAME)


@pytest.mark.asyncio
async def test_success_is_bounded_untrusted_and_records_configured_cost() -> None:
    request = RecordingRequest(WolframAlphaResponse(200, "Result: 2464 miles"))
    usage = RecordingUsageStore()
    ctx = _context(usage_store=usage)

    raw = await _registry(request, cost=0.01).dispatch(
        TOOL_NAME,
        {"input": "distance Los Angeles to New York", "units": "nonmetric"},
        ctx,
    )
    payload = json.loads(raw)

    assert payload["query"] == "distance Los Angeles to New York"
    assert payload["result"] == "Result: 2464 miles"
    assert payload["context_is_untrusted"] is True
    assert "secret-app-id" not in raw
    assert request.calls == [
        {
            "app_id": "secret-app-id",
            "input_text": "distance Los Angeles to New York",
            "units": "nonmetric",
            "max_chars": 6_800,
            "timeout_seconds": 1.0,
        }
    ]
    assert ctx.wolfram_alpha_calls_this_turn == 1
    assert [(call.provider, call.cost_usd) for call in usage.calls] == [("wolfram_alpha", 0.01)]


@pytest.mark.asyncio
async def test_validation_rejects_multiline_long_and_invalid_unit_without_spending_call() -> None:
    request = RecordingRequest(WolframAlphaResponse(200, "unused"))
    registry = _registry(request)
    ctx = _context()

    multiline = json.loads(await registry.dispatch(TOOL_NAME, {"input": "first\nsecond"}, ctx))
    too_many_words = json.loads(
        await registry.dispatch(
            TOOL_NAME,
            {"input": " ".join(["x"] * (MAX_INPUT_WORDS + 1))},
            ctx,
        )
    )
    bad_units = json.loads(
        await registry.dispatch(TOOL_NAME, {"input": "2+2", "units": "imperial"}, ctx)
    )

    assert multiline["error"] == "input must be a single-line string"
    assert too_many_words["error"] == "input must be 100 words or fewer"
    assert bad_units["error"] == "units must be one of: metric, nonmetric"
    assert ctx.wolfram_alpha_calls_this_turn == 0
    assert request.calls == []


@pytest.mark.asyncio
async def test_call_budget_is_scoped_to_message_context() -> None:
    request = RecordingRequest(WolframAlphaResponse(200, "4"))
    registry = _registry(request, limit=2)
    ctx = _context()

    for _ in range(2):
        assert "error" not in json.loads(await registry.dispatch(TOOL_NAME, {"input": "2+2"}, ctx))
    exhausted = json.loads(await registry.dispatch(TOOL_NAME, {"input": "2+2"}, ctx))
    assert exhausted["error"] == "Wolfram|Alpha call limit reached for this turn."

    fresh = _context()
    assert "error" not in json.loads(await registry.dispatch(TOOL_NAME, {"input": "2+2"}, fresh))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "message"),
    [
        (400, "Wolfram|Alpha rejected the query."),
        (401, "Wolfram|Alpha credentials were rejected."),
        (429, "Wolfram|Alpha rate limit or quota reached."),
        (503, "Wolfram|Alpha is temporarily unavailable."),
    ],
)
async def test_provider_statuses_return_safe_errors(status: int, message: str) -> None:
    registry = _registry(RecordingRequest(WolframAlphaResponse(status, "secret body")))

    result = json.loads(await registry.dispatch(TOOL_NAME, {"input": "2+2"}, _context()))

    assert result == {"error": message}
    assert "secret body" not in json.dumps(result)


@pytest.mark.asyncio
async def test_501_suggestions_are_returned_as_untrusted_recovery_context() -> None:
    registry = _registry(RecordingRequest(WolframAlphaResponse(501, "Try: population France")))

    result = json.loads(await registry.dispatch(TOOL_NAME, {"input": "pop frnce"}, _context()))

    assert result["context_is_untrusted"] is True
    assert "could not interpret" in result["result"]
    assert "population France" in result["result"]


@pytest.mark.asyncio
async def test_transport_uses_bearer_auth_retries_and_bounds_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = FakeSessionFactory(
        FakeHttpResponse(503, "temporary"),
        FakeHttpResponse(200, "x" * 3_000),
    )

    async def no_wait(_: float) -> None:
        return None

    monkeypatch.setattr(wolfram_alpha_module.aiohttp, "ClientSession", factory)
    monkeypatch.setattr(wolfram_alpha_module.asyncio, "sleep", no_wait)

    response = await request_llm_result(
        app_id="transport-secret",
        input_text="2+2",
        units="metric",
        max_chars=500,
        timeout_seconds=1.0,
    )

    assert response.status == 200
    assert len(response.text) == 500
    assert response.text.endswith("[truncated]")
    assert len(factory.calls) == 2
    for call in factory.calls:
        assert call["url"] == wolfram_alpha_module.API_URL
        assert "appid" not in call["params"]
        assert call["params"] == {"input": "2+2", "maxchars": 500, "units": "metric"}
        assert call["headers"]["Authorization"] == "Bearer transport-secret"


@pytest.mark.asyncio
async def test_result_is_defensively_truncated() -> None:
    registry = _registry(
        RecordingRequest(WolframAlphaResponse(200, "x" * 1_000)),
        max_output_chars=500,
    )

    result = json.loads(await registry.dispatch(TOOL_NAME, {"input": "large result"}, _context()))

    assert len(result["result"]) == 500
    assert result["result"].endswith("[truncated]")


def test_settings_defaults_and_bounds() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.wolfram_alpha_app_id.get_secret_value() == ""
    assert settings.wolfram_alpha_timeout_seconds == 30.0
    assert settings.wolfram_alpha_max_calls_per_turn == 3
    assert settings.wolfram_alpha_max_output_chars == 6_800
    assert settings.wolfram_alpha_call_cost_usd is None

    for field, value in [
        ("wolfram_alpha_timeout_seconds", 0),
        ("wolfram_alpha_max_calls_per_turn", 11),
        ("wolfram_alpha_max_output_chars", 499),
        ("wolfram_alpha_call_cost_usd", -0.01),
    ]:
        with pytest.raises(ValidationError):
            setattr(settings, field, value)
