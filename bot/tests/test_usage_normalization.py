from __future__ import annotations

from usage.normalization import UsageBreakdown, normalize_usage


def test_anthropic_shape_maps_cache_fields() -> None:
    raw = {
        "input_tokens": 100,
        "output_tokens": 40,
        "cache_read_input_tokens": 200,
        "cache_creation_input_tokens": 10,
    }

    assert normalize_usage(raw) == UsageBreakdown(
        input_tokens=100,
        cached_read_tokens=200,
        cache_write_tokens=10,
        output_tokens=40,
    )


def test_canonical_shape_passes_through() -> None:
    raw = {
        "input_tokens": 100,
        "cached_read_tokens": 200,
        "cache_write_tokens": 10,
        "output_tokens": 40,
    }

    assert normalize_usage(raw) == UsageBreakdown(
        input_tokens=100,
        cached_read_tokens=200,
        cache_write_tokens=10,
        output_tokens=40,
    )


def test_openai_shape_splits_cached_prompt_tokens() -> None:
    raw = {
        "prompt_tokens": 1000,
        "completion_tokens": 50,
        "prompt_tokens_details": {"cached_tokens": 300},
    }

    assert normalize_usage(raw) == UsageBreakdown(
        input_tokens=700,
        cached_read_tokens=300,
        cache_write_tokens=0,
        output_tokens=50,
    )


def test_missing_and_garbage_become_zero() -> None:
    assert normalize_usage(None) == UsageBreakdown()
    assert normalize_usage({}) == UsageBreakdown()
    assert normalize_usage({"input_tokens": "x", "output_tokens": -5}) == UsageBreakdown()


def test_add_accumulates_fieldwise() -> None:
    a = UsageBreakdown(input_tokens=1, cached_read_tokens=2, output_tokens=3)
    b = UsageBreakdown(input_tokens=10, cache_write_tokens=5, output_tokens=7)

    assert (a + b) == UsageBreakdown(
        input_tokens=11,
        cached_read_tokens=2,
        cache_write_tokens=5,
        output_tokens=10,
    )
