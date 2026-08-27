from __future__ import annotations

from typing import Any, cast

import pytest

from agent.core import ConversationRunResult
from agent.turn import TurnDependencies, TurnRequest, _TurnUsageRecorder
from config.model_config import ModelEntry, ModelPricing
from tests.helpers import make_turn_dependencies
from trust.tiers import TrustTier
from usage.normalization import LLMUsageCall, UsageBreakdown


class _FakeStore:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    async def record_turn(self, **kwargs: Any) -> None:
        self.rows.append(kwargs)


class _FakeModelConfig:
    def __init__(self, pricing: ModelPricing | None) -> None:
        self.models = {
            "minimax-m3": ModelEntry(
                provider="p",
                model="minimax-m3",
                pricing=pricing,
            )
        }


def _turn() -> TurnRequest:
    return TurnRequest(
        content="hi",
        context=cast(Any, None),
        trust_tier=TrustTier.MEMBER,
        user_id="u1",
        user_name="Ann",
        guild_id="g",
        channel_id="c",
        thread_id=None,
        channel_name="general",
    )


def _deps(
    store: Any,
    model_config: _FakeModelConfig,
    model_name: str,
) -> TurnDependencies:
    return make_turn_dependencies(
        usage_store=store,
        model_config=model_config,
        resolved_model_name=model_name,
    )


async def _record_usage(
    dependencies: TurnDependencies,
    turn: TurnRequest,
    run: ConversationRunResult,
) -> None:
    recorder = _TurnUsageRecorder(dependencies, turn, turn_id="turn-test")
    recorder.absorb(run.llm_calls)
    await recorder.flush()


@pytest.mark.asyncio
async def test_records_priced_row() -> None:
    store = _FakeStore()
    model_config = _FakeModelConfig(
        ModelPricing(input=0.6, output=2.4, cached_read=0.12, cache_write=0.75)
    )
    run = ConversationRunResult(
        text="ok",
        usage=UsageBreakdown(input_tokens=1_000_000),
        iterations=2,
        llm_calls=[
            LLMUsageCall(
                model="minimax-m3-2026",
                role="chat",
                usage=UsageBreakdown(input_tokens=1_000_000),
                pricing_model="minimax-m3",
            ),
            LLMUsageCall(
                model="minimax-m3-2026",
                role="chat",
                usage=UsageBreakdown(),
                pricing_model="minimax-m3",
            ),
        ],
    )

    await _record_usage(_deps(store, model_config, "minimax-m3"), _turn(), run)

    assert len(store.rows) == 1
    assert store.rows[0]["user_id"] == "u1"
    calls = store.rows[0]["calls"]
    assert len(calls) == 2
    assert calls[0].est_cost_usd == pytest.approx(0.6)
    assert calls[0].model == "minimax-m3-2026"
    assert calls[0].role == "chat"


@pytest.mark.asyncio
async def test_records_zero_usage_when_provider_returned_response() -> None:
    store = _FakeStore()
    model_config = _FakeModelConfig(ModelPricing(input=0.60, output=2.40))
    run = ConversationRunResult(
        text="ok",
        usage=UsageBreakdown(),
        llm_calls=[LLMUsageCall(model="minimax-m3", role="chat", usage=UsageBreakdown())],
        iterations=1,
    )

    await _record_usage(_deps(store, model_config, "minimax-m3"), _turn(), run)

    assert len(store.rows) == 1
    assert store.rows[0]["calls"][0].est_cost_usd == 0.0


@pytest.mark.asyncio
async def test_records_missing_usage_as_unpriced() -> None:
    store = _FakeStore()
    model_config = _FakeModelConfig(ModelPricing(input=0.60, output=2.40))
    run = ConversationRunResult(
        text="ok",
        usage=UsageBreakdown(),
        llm_calls=[
            LLMUsageCall(
                model="minimax-m3",
                role="chat",
                usage=UsageBreakdown(),
                usage_present=False,
            )
        ],
        iterations=1,
    )

    await _record_usage(_deps(store, model_config, "minimax-m3"), _turn(), run)

    assert store.rows[0]["calls"][0].est_cost_usd is None


@pytest.mark.asyncio
async def test_skips_when_no_provider_response_and_missing_deps() -> None:
    store = _FakeStore()
    model_config = _FakeModelConfig(None)
    empty = ConversationRunResult(text="ok", usage=UsageBreakdown(), iterations=0)

    await _record_usage(_deps(store, model_config, "minimax-m3"), _turn(), empty)
    await _record_usage(
        _deps(None, model_config, "minimax-m3"),
        _turn(),
        ConversationRunResult(
            text="ok",
            usage=UsageBreakdown(output_tokens=1),
            iterations=1,
        ),
    )

    assert store.rows == []


@pytest.mark.asyncio
async def test_store_failure_is_swallowed() -> None:
    class BoomStore:
        async def record_turn(self, **kwargs: Any) -> None:
            _ = kwargs
            raise RuntimeError("db down")

    model_config = _FakeModelConfig(ModelPricing(output=2.4))
    run = ConversationRunResult(
        text="ok",
        usage=UsageBreakdown(output_tokens=1),
        iterations=1,
        llm_calls=[
            LLMUsageCall(
                model="minimax-m3",
                role="chat",
                usage=UsageBreakdown(output_tokens=1),
            )
        ],
    )

    await _record_usage(_deps(BoomStore(), model_config, "minimax-m3"), _turn(), run)
