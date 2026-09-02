from __future__ import annotations

from types import SimpleNamespace

import pytest

from config.model_config import ModelPricing
from usage.normalization import LLMUsageCall, UsageBreakdown
from usage.pricing import estimate_cost, price_usage_call

FULL = ModelPricing(input=0.60, output=2.40, cached_read=0.12, cache_write=0.75)


def test_prices_each_bucket_at_its_rate() -> None:
    usage = UsageBreakdown(
        input_tokens=1_000_000,
        cached_read_tokens=1_000_000,
        cache_write_tokens=1_000_000,
        output_tokens=1_000_000,
    )

    assert estimate_cost(FULL, usage) == pytest.approx(3.87)


def test_no_pricing_block_returns_none() -> None:
    assert estimate_cost(None, UsageBreakdown(input_tokens=100)) is None


def test_nonzero_bucket_without_rate_returns_none() -> None:
    partial = ModelPricing(input=0.60, output=2.40)

    assert estimate_cost(partial, UsageBreakdown(cached_read_tokens=5)) is None


def test_zero_token_bucket_without_rate_ignored() -> None:
    partial = ModelPricing(input=0.60, output=2.40)

    assert estimate_cost(partial, UsageBreakdown(input_tokens=1_000_000)) == pytest.approx(0.60)


def test_zero_usage_with_pricing_is_zero() -> None:
    assert estimate_cost(FULL, UsageBreakdown()) == 0.0


def test_missing_usage_stays_unpriced_but_reported_zero_is_priceable() -> None:
    config = SimpleNamespace(models={"m": SimpleNamespace(pricing=FULL)})
    missing = price_usage_call(
        LLMUsageCall(
            model="m",
            role="chat",
            usage=UsageBreakdown(),
            usage_present=False,
        ),
        config,
    )
    reported_zero = price_usage_call(
        LLMUsageCall(model="m", role="chat", usage=UsageBreakdown()),
        config,
    )

    assert missing.est_cost_usd is None
    assert reported_zero.est_cost_usd == 0.0


def test_explicit_specialist_cost_is_preserved_without_catalog_pricing() -> None:
    call = LLMUsageCall(
        model="gemini-3.7-flash-001",
        pricing_model="gemini-3.7-flash",
        role="video_analysis",
        usage=UsageBreakdown(input_tokens=100),
        est_cost_usd=0.123,
    )

    priced = price_usage_call(call, model_config=None)

    assert priced.est_cost_usd == 0.123
    assert priced.pricing_model == "gemini-3.7-flash"


def test_pricing_preserves_provider_attribution() -> None:
    call = LLMUsageCall(
        model="openai/gpt-5",
        role="chat",
        usage=UsageBreakdown(input_tokens=100),
        upstream_provider="OpenAI",
        service_tier="priority",
        openrouter_charge_usd=0.02,
        is_byok=False,
    )

    priced = price_usage_call(call, model_config=None)

    assert priced.upstream_provider == "OpenAI"
    assert priced.service_tier == "priority"
    assert priced.openrouter_charge_usd == 0.02
    assert priced.is_byok is False


def test_video_specialist_pricing_resolves_via_pricing_model() -> None:
    config = SimpleNamespace(
        models={
            "gemini-video-flash": SimpleNamespace(
                pricing=ModelPricing(input=0.75, output=3.75, cached_read=0.075)
            )
        }
    )
    call = LLMUsageCall(
        model="gemini-3.7-flash",
        pricing_model="gemini-video-flash",
        role="video_analysis",
        usage=UsageBreakdown(
            input_tokens=1_000_000,
            output_tokens=100_000,
            cached_read_tokens=500_000,
        ),
    )
    priced = price_usage_call(call, config)
    assert priced.est_cost_usd == pytest.approx(0.75 + 0.375 + 0.0375)
    assert priced.model == "gemini-3.7-flash"
    assert priced.pricing_model == "gemini-video-flash"

