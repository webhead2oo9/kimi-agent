from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from config.model_config import load_model_config


def _cfg(body: str, tmp_path: Path) -> Path:
    path = tmp_path / "models.yaml"
    path.write_text(body, encoding="utf-8")
    return path


_BASE = """
providers:
  p:
    type: anthropic_compat
    base_url: https://opencode.ai/zen/go/v1
    api_key_env: OPENCODE_GO_API_KEY
models:
  m:
    provider: p
    model: minimax-m3
    pricing:
      input: 0.60
      output: 2.40
      cached_read: 0.12
      cache_write: 0.75
roles:
  chat: m
  compaction: m
"""


def test_pricing_block_parses(tmp_path: Path) -> None:
    cfg = load_model_config(_cfg(_BASE, tmp_path))

    pricing = cfg.models["m"].pricing

    assert pricing is not None
    assert pricing.input == 0.60
    assert pricing.cached_read == 0.12


def test_pricing_optional(tmp_path: Path) -> None:
    body = _BASE.replace(
        "    pricing:\n"
        "      input: 0.60\n"
        "      output: 2.40\n"
        "      cached_read: 0.12\n"
        "      cache_write: 0.75\n",
        "",
    )

    cfg = load_model_config(_cfg(body, tmp_path))

    assert cfg.models["m"].pricing is None


def test_negative_rate_rejected(tmp_path: Path) -> None:
    body = _BASE.replace("input: 0.60", "input: -1")

    with pytest.raises(ValidationError, match=">= 0"):
        load_model_config(_cfg(body, tmp_path))


def test_configured_pricing_drives_cost_estimation(tmp_path: Path) -> None:
    from usage.normalization import UsageBreakdown
    from usage.pricing import estimate_cost

    cfg = load_model_config(_cfg(_BASE, tmp_path))
    pricing = cfg.models["m"].pricing

    assert pricing is not None
    assert pricing.input == 0.60
    assert pricing.output == 2.40
    assert pricing.cached_read == 0.12
    assert pricing.cache_write == 0.75
    cost = estimate_cost(
        pricing,
        UsageBreakdown(input_tokens=1_000_000, cached_read_tokens=1_000_000),
    )
    assert cost == pytest.approx(0.72)
    assert estimate_cost(
        pricing,
        UsageBreakdown(cache_write_tokens=1_000_000),
    ) == pytest.approx(0.75)
