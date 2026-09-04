from __future__ import annotations

from typing import cast

import pytest

from agent.context import ConversationContext
from agent.core import ConversationRunRequest, ConversationRunResult, run_conversation
from providers.base import LLMProvider
from providers.types import ProviderCapability, ProviderRequest, ProviderResponse, ToolCall
from tools.registry import MessageContext, ToolRegistry
from trust.tiers import TrustTier
from usage.normalization import LLMUsageCall, UsageBreakdown


class ScriptedProvider(LLMProvider):
    provider_key = "scripted"
    model = "test-model"
    capabilities = {ProviderCapability.TEXT, ProviderCapability.TOOL_CALLING}

    def __init__(self, responses: list[ProviderResponse | Exception]) -> None:
        self._responses = list(responses)

    async def run_turn(self, request: ProviderRequest) -> ProviderResponse:
        _ = request
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


async def lookup(args: dict, ctx: MessageContext) -> str:
    _ = ctx
    return f"found {args['query']}"


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        name="lookup",
        description="Lookup a value",
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        handler=lookup,
    )
    return registry


def test_result_carries_text_and_usage_as_separate_fields() -> None:
    result = ConversationRunResult(text="hi")

    assert result.text == "hi"
    assert result.usage == UsageBreakdown()


def test_result_equality_includes_completion_and_usage_metadata() -> None:
    result = ConversationRunResult(text="hi")

    assert result == ConversationRunResult(text="hi")
    assert result != ConversationRunResult(text="hi", timed_out=True)
    assert result != ConversationRunResult(text="hi", usage=UsageBreakdown(input_tokens=1))


@pytest.mark.asyncio
async def test_core_preserves_missing_usage_vs_explicit_zero_usage() -> None:
    missing = await run_conversation(
        request=ConversationRunRequest(
            user_message="first",
            context=ConversationContext(key="missing"),
            trust_tier=TrustTier.MEMBER,
            user_name="Ann",
            user_id="u1",
            provider=cast(LLMProvider, ScriptedProvider([ProviderResponse(content="done")])),
            registry=_registry(),
        )
    )
    reported_zero = await run_conversation(
        request=ConversationRunRequest(
            user_message="second",
            context=ConversationContext(key="reported-zero"),
            trust_tier=TrustTier.MEMBER,
            user_name="Ann",
            user_id="u1",
            provider=cast(
                LLMProvider,
                ScriptedProvider(
                    [
                        ProviderResponse(
                            content="done",
                            usage={"input_tokens": 0, "output_tokens": 0},
                            usage_present=True,
                        )
                    ]
                ),
            ),
            registry=_registry(),
        )
    )

    assert missing.llm_calls[0].usage_present is False
    assert reported_zero.llm_calls[0].usage_present is True


@pytest.mark.asyncio
async def test_core_preserves_openrouter_attribution_on_usage_calls() -> None:
    result = await run_conversation(
        request=ConversationRunRequest(
            user_message="hello",
            context=ConversationContext(key="attribution"),
            trust_tier=TrustTier.MEMBER,
            user_name="Ann",
            user_id="u1",
            provider=cast(
                LLMProvider,
                ScriptedProvider(
                    [
                        ProviderResponse(
                            content="done",
                            usage={"input_tokens": 1, "output_tokens": 2},
                            usage_present=True,
                            upstream_provider="Anthropic",
                            service_tier="priority",
                            openrouter_charge_usd=0.003,
                            is_byok=False,
                        )
                    ]
                ),
            ),
            registry=_registry(),
        )
    )

    call = result.llm_calls[0]
    assert call.upstream_provider == "Anthropic"
    assert call.service_tier == "priority"
    assert call.openrouter_charge_usd == 0.003
    assert call.is_byok is False


@pytest.mark.asyncio
async def test_run_conversation_accumulates_multi_iteration_usage() -> None:
    provider = ScriptedProvider(
        [
            ProviderResponse(
                content="",
                tool_calls=[ToolCall(id="call_1", name="lookup", arguments={"query": "vr"})],
                usage={"input_tokens": 10, "output_tokens": 2},
                usage_present=True,
            ),
            ProviderResponse(
                content="done",
                usage={
                    "prompt_tokens": 20,
                    "completion_tokens": 5,
                    "prompt_tokens_details": {"cached_tokens": 8},
                },
                usage_present=True,
            ),
        ]
    )

    result = await run_conversation(
        request=ConversationRunRequest(
            user_message="look this up",
            context=ConversationContext(key="test"),
            trust_tier=TrustTier.MEMBER,
            user_name="Ann",
            user_id="u1",
            provider=cast(LLMProvider, provider),
            registry=_registry(),
        )
    )

    assert result.text == "done"
    assert result.usage == UsageBreakdown(
        input_tokens=22,
        cached_read_tokens=8,
        output_tokens=7,
    )


@pytest.mark.asyncio
async def test_model_backed_tool_appends_to_shared_call_ledger() -> None:
    async def model_tool(_args: dict, ctx: MessageContext) -> str:
        assert ctx.usage_sink is not None
        ctx.usage_sink.append(
            LLMUsageCall(
                model="tool-served",
                pricing_model="tool-priced",
                role="tool_model",
                usage=UsageBreakdown(input_tokens=7, output_tokens=3),
            )
        )
        return "tool result"

    registry = ToolRegistry()
    registry.register(
        name="model_tool",
        description="Use another model",
        parameters={"type": "object", "properties": {}},
        handler=model_tool,
    )
    provider = ScriptedProvider(
        [
            ProviderResponse(
                tool_calls=[ToolCall(id="call_1", name="model_tool", arguments={})],
                usage={"input_tokens": 10, "output_tokens": 2},
                usage_present=True,
            ),
            ProviderResponse(
                content="done",
                usage={"input_tokens": 20, "output_tokens": 5},
                usage_present=True,
            ),
        ]
    )
    sink: list[LLMUsageCall] = []

    result = await run_conversation(
        request=ConversationRunRequest(
            user_message="use it",
            context=ConversationContext(key="test"),
            trust_tier=TrustTier.MEMBER,
            user_name="Ann",
            user_id="u1",
            provider=cast(LLMProvider, provider),
            registry=registry,
            usage_sink=sink,
        )
    )

    assert result.llm_calls == sink
    assert [call.role for call in sink] == ["chat", "tool_model", "chat"]
    assert sink[1].usage == UsageBreakdown(input_tokens=7, output_tokens=3)
    assert result.usage == UsageBreakdown(input_tokens=37, output_tokens=10)


@pytest.mark.asyncio
async def test_later_provider_error_preserves_prior_usage() -> None:
    provider = ScriptedProvider(
        [
            ProviderResponse(
                content="",
                tool_calls=[ToolCall(id="call_1", name="lookup", arguments={"query": "vr"})],
                usage={"input_tokens": 10, "output_tokens": 2},
                usage_present=True,
            ),
            RuntimeError("boom"),
        ]
    )

    result = await run_conversation(
        request=ConversationRunRequest(
            user_message="look this up",
            context=ConversationContext(key="test"),
            trust_tier=TrustTier.MEMBER,
            user_name="Ann",
            user_id="u1",
            provider=cast(LLMProvider, provider),
            registry=_registry(),
        )
    )

    assert "internal error" in result.text
    assert result.usage == UsageBreakdown(input_tokens=10, output_tokens=2)
