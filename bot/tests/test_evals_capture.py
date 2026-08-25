import asyncio
import json
from typing import Any, cast

from evals.capture import InstrumentedProvider, InstrumentedRegistry, sum_tokens
from providers.base import LLMProvider
from providers.types import ProviderRequest, ProviderResponse
from tools.registry import MessageContext
from trust.tiers import TrustTier


class _Scripted(LLMProvider):
    model = "inner-model"

    def __init__(self, response):
        self._response = response

    async def run_turn(self, request: ProviderRequest) -> ProviderResponse:
        return self._response


def _ctx():
    return MessageContext(
        user_id="u",
        user_name="n",
        guild_id=None,
        channel_id="c",
        thread_id=None,
        trust_tier=TrustTier.MEMBER,
    )


def _req():
    return ProviderRequest(
        conversation_id=0,
        system_prompt="",
        messages=[],
        current_user_parts=[],
        tools=[],
        max_tokens=16,
    )


def test_instrumented_provider_passes_through_and_records_usage():
    inner = _Scripted(ProviderResponse(content="hi", usage={"total_tokens": 42}))
    provider = InstrumentedProvider(inner)
    assert provider.model == "inner-model"

    result = asyncio.run(provider.run_turn(_req()))
    assert result.content == "hi"
    assert provider.total_tokens == 42
    assert len(provider.calls) == 1
    assert provider.calls[0].latency_ms >= 0


def test_sum_tokens_handles_prompt_completion_split():
    assert sum_tokens({"prompt_tokens": 10, "completion_tokens": 5}) == 15
    assert sum_tokens({"total_tokens": 99}) == 99
    assert sum_tokens({}) == 0


def test_instrumented_provider_close_delegates_to_inner_and_noops_when_absent():
    closed = {"called": False}

    class _Closable(LLMProvider):
        model = "m"

        async def run_turn(self, request):
            return ProviderResponse(content="x")

        async def close(self):
            closed["called"] = True

    asyncio.run(InstrumentedProvider(_Closable()).close())
    assert closed["called"] is True

    # No close() on the inner provider -> close() must no-op, not raise.
    asyncio.run(InstrumentedProvider(_Scripted(ProviderResponse(content="x"))).close())


def test_instrumented_registry_records_dispatch():
    async def handler(args, ctx):
        return json.dumps({"ok": True})

    registry = InstrumentedRegistry()
    registry.register(
        name="probe",
        description="d",
        parameters={"type": "object", "properties": {}},
        handler=handler,
    )
    registry.reset_sink()
    result = asyncio.run(registry.dispatch("probe", {"x": 1}, _ctx()))
    assert json.loads(result) == {"ok": True}
    assert len(registry.sink) == 1
    record = registry.sink[0]
    assert record.tool == "probe"
    assert record.args == {"x": 1}
    assert record.ok is True


def test_instrumented_registry_records_errors_and_coerces_nondict_args():
    async def boom(args, ctx):
        return json.dumps({"error": "boom"})

    registry = InstrumentedRegistry()
    registry.register(
        name="boom",
        description="d",
        parameters={"type": "object", "properties": {}},
        handler=boom,
    )
    asyncio.run(registry.dispatch("boom", cast(dict[str, Any], "not-a-dict"), _ctx()))
    record = registry.sink[-1]
    # ok=False is what the Task 7 mechanical layer reads to count tool errors.
    assert record.ok is False
    assert record.args == {"_raw": "not-a-dict"}
