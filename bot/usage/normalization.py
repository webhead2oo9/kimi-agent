from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class UsageBreakdown:
    input_tokens: int = 0
    cached_read_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0

    def __add__(self, other: UsageBreakdown) -> UsageBreakdown:
        return UsageBreakdown(
            input_tokens=self.input_tokens + other.input_tokens,
            cached_read_tokens=self.cached_read_tokens + other.cached_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
        )


@dataclass(frozen=True)
class LLMUsageCall:
    """Normalized accounting for one completed provider call."""

    model: str
    role: str
    usage: UsageBreakdown
    pricing_model: str | None = None
    est_cost_usd: float | None = None


def normalize_usage(raw: dict[str, Any] | None) -> UsageBreakdown:
    if not isinstance(raw, dict):
        return UsageBreakdown()

    input_tokens = _as_int(raw.get("input_tokens"))
    cached_read = _as_int(raw.get("cached_read_tokens"))
    if cached_read == 0:
        cached_read = _as_int(raw.get("cache_read_input_tokens"))
    cache_write = _as_int(raw.get("cache_write_tokens"))
    if cache_write == 0:
        cache_write = _as_int(raw.get("cache_creation_input_tokens"))
    output_tokens = _as_int(raw.get("output_tokens"))

    input_details = raw.get("input_tokens_details")
    if cached_read == 0 and isinstance(input_details, dict):
        cached = min(_as_int(input_details.get("cached_tokens")), input_tokens)
        input_tokens -= cached
        cached_read = cached

    if input_tokens == 0 and "prompt_tokens" in raw:
        prompt = _as_int(raw.get("prompt_tokens"))
        details = raw.get("prompt_tokens_details")
        cached = _as_int(details.get("cached_tokens")) if isinstance(details, dict) else 0
        cached = min(cached, prompt)
        input_tokens = prompt - cached
        cached_read = cached
    if output_tokens == 0 and "completion_tokens" in raw:
        output_tokens = _as_int(raw.get("completion_tokens"))

    return UsageBreakdown(
        input_tokens=input_tokens,
        cached_read_tokens=cached_read,
        cache_write_tokens=cache_write,
        output_tokens=output_tokens,
    )


def _as_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(0, value)
