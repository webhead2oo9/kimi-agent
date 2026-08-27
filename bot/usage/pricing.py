from __future__ import annotations
from typing import Any

from config.model_config import ModelPricing
from usage.normalization import LLMUsageCall, UsageBreakdown


def estimate_cost(pricing: ModelPricing | None, usage: UsageBreakdown) -> float | None:
    if pricing is None:
        return None

    buckets = (
        (usage.input_tokens, pricing.input),
        (usage.cached_read_tokens, pricing.cached_read),
        (usage.cache_write_tokens, pricing.cache_write),
        (usage.output_tokens, pricing.output),
    )
    total = 0.0
    for tokens, rate in buckets:
        if tokens == 0:
            continue
        if rate is None:
            return None
        total += tokens * rate / 1_000_000
    return total


def price_usage_call(
    call: LLMUsageCall,
    model_config: Any | None,
) -> LLMUsageCall:
    pricing_model = call.pricing_model or call.model
    estimated = call.est_cost_usd
    if estimated is None:
        estimated = estimate_cost(
            pricing_for_model(model_config, pricing_model),
            call.usage,
        )
    return LLMUsageCall(
        model=call.model,
        role=call.role,
        usage=call.usage,
        pricing_model=pricing_model,
        est_cost_usd=estimated,
    )


def pricing_for_model(model_config: Any | None, model: str) -> ModelPricing | None:
    """Resolve a rate card without guessing across ambiguous model aliases."""
    if model_config is None:
        return None
    exact = model_config.models.get(model)
    if exact is not None:
        return exact.pricing
    matches = [entry.pricing for entry in model_config.models.values() if entry.model == model]
    if not matches:
        return None
    first = matches[0]
    return first if all(pricing == first for pricing in matches[1:]) else None
