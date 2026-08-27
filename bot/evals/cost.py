"""Cost and per-tool token accounting for eval runs.

Two questions this answers, which the mechanical score deliberately does not:

1. What did a run cost in LLM tokens? Real per-bucket usage from the provider,
   priced with the same `usage/pricing.py:estimate_cost` the bot's `/usage`
   command uses, so eval token dollars and production token dollars mean the
   same thing.
2. Which tools are expensive? A tool is not billed for running; it is billed for
   what its result adds to the context, *and* for every later provider call in
   that turn that carries the result again. A cheap-looking tool called early in
   a long loop can cost more than an expensive one called last.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from config.model_config import ModelPricing
from evals.cassette import cassette_records
from usage.normalization import UsageBreakdown
from usage.pricing import estimate_cost

if TYPE_CHECKING:  # pragma: no cover - typing only
    from evals.harness import ScenarioRun


@dataclass(frozen=True)
class ToolCost:
    tool: str
    calls: int
    errors: int
    result_chars: int
    # Tokens the results themselves are worth, once.
    result_tokens: int
    # Result tokens multiplied by the provider calls that carried them
    # afterwards: the context tax a tool imposes for the rest of the turn.
    context_tokens: int
    duration_ms: int
    # Calls that reached the real handler. The default preserves deserialization
    # of summaries that omit per-source call counts.
    live_calls: int = 0
    # Calls served from a cassette. Per tool rather than run-wide, because "did a
    # replay hide spend" is a question about *which* tool replayed. Defaulted
    # for the same compatibility reason as `live_calls`.
    replay_calls: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "calls": self.calls,
            "errors": self.errors,
            "result_chars": self.result_chars,
            "result_tokens": self.result_tokens,
            "context_tokens": self.context_tokens,
            "duration_ms": self.duration_ms,
            "live_calls": self.live_calls,
            "replay_calls": self.replay_calls,
        }


def usage_dict(usage: UsageBreakdown) -> dict[str, int]:
    return {
        "input": usage.input_tokens,
        "cached_read": usage.cached_read_tokens,
        "cache_write": usage.cache_write_tokens,
        "output": usage.output_tokens,
    }


def run_cost(run: ScenarioRun, pricing: ModelPricing | None) -> float | None:
    """USD for one scenario run, or None when the arm is unpriced."""
    return estimate_cost(pricing, run.total_usage)


def sum_costs(costs: Sequence[float | None]) -> float | None:
    """Total of per-rep costs, or None when any rep was unpriced.

    Summing with `cost or 0.0` would count an unpriced rep as free, which in a
    mixed run silently reports a fraction of the bill as the whole of it. A run
    that really measured $0.00 keeps its 0.0. Only absent pricing is None.
    """
    if not costs or any(cost is None for cost in costs):
        return None
    return round(sum(cost for cost in costs if cost is not None), 6)


def mean_cost(costs: Sequence[float | None]) -> float | None:
    """Mean per-rep cost, or None when any rep was unpriced.

    Same rule as `sum_costs`, because the two are read side by side: averaging
    only the priced reps prints a concrete per-scenario figure under a header
    that reads `unpriced`, and that figure is a mean over a subset the reader
    cannot see. Mixed pricing inside one arm is ordinary rather than exotic: the
    rep that writes cache is unpriced when no `cache_write` rate is configured
    while the reps that only read cache price fine.
    """
    if not costs or any(cost is None for cost in costs):
        return None
    priced = [cost for cost in costs if cost is not None]
    return round(sum(priced) / len(priced), 6)


def recorded_call_sources(runs: list[ScenarioRun]) -> dict[str, int]:
    """Per-source split over cassette-eligible tool calls only.

    The rep-level `sources` dict counts every dispatch. This split isolates calls
    whose results can come from a cassette while preserving all four source
    values: live, replay, fault, and miss.
    """
    counts: dict[str, int] = {"live": 0, "replay": 0}
    for run in runs:
        for record in run.all_tool_calls:
            if not cassette_records(record.tool):
                continue
            counts[record.source] = counts.get(record.source, 0) + 1
    return counts


# `ToolCallRecord.source` -> the word the report prints for it.
_SOURCE_LABELS = {"live": "live", "replay": "replayed", "fault": "faulted", "miss": "missed"}
# Rendered even at zero: "0 replayed" is information (nothing was replayed),
# where "0 faulted" is noise on the runs that inject no faults.
_ALWAYS_SHOWN = ("live", "replay")


def recorded_call_split(counts: Mapping[str, int]) -> str:
    """Render `recorded_call_sources` as the report's denominator phrase.

    Build from the dict so fault and miss sources remain in the denominator; two
    named keys would undercount cassette-recorded calls.
    """
    order = [*_SOURCE_LABELS, *sorted(key for key in counts if key not in _SOURCE_LABELS)]
    return " / ".join(
        f"{int(counts.get(source, 0))} {_SOURCE_LABELS.get(source, source)}"
        for source in order
        if source in _ALWAYS_SHOWN or int(counts.get(source, 0)) > 0
    )


def tool_costs(runs: list[ScenarioRun]) -> list[ToolCost]:
    """Per-tool aggregate across runs, most context-expensive first."""
    calls: dict[str, int] = defaultdict(int)
    live: dict[str, int] = defaultdict(int)
    replay: dict[str, int] = defaultdict(int)
    errors: dict[str, int] = defaultdict(int)
    chars: dict[str, int] = defaultdict(int)
    result_tokens: dict[str, int] = defaultdict(int)
    context_tokens: dict[str, int] = defaultdict(int)
    duration: dict[str, int] = defaultdict(int)

    for run in runs:
        for turn in run.turns:
            for record in turn.tool_calls:
                name = record.tool
                calls[name] += 1
                if record.source == "live":
                    live[name] += 1
                if record.source == "replay":
                    replay[name] += 1
                if not record.ok:
                    errors[name] += 1
                chars[name] += record.result_chars
                tokens = record.estimated_tokens
                result_tokens[name] += tokens
                # Every provider call after this dispatch re-sends the result as
                # input. A result produced by the final call is carried by none.
                resent = max(0, turn.provider_calls - record.provider_calls_before)
                context_tokens[name] += tokens * resent
                duration[name] += record.duration_ms

    out = [
        ToolCost(
            tool=name,
            calls=calls[name],
            errors=errors[name],
            result_chars=chars[name],
            result_tokens=result_tokens[name],
            context_tokens=context_tokens[name],
            duration_ms=duration[name],
            live_calls=live[name],
            replay_calls=replay[name],
        )
        for name in calls
    ]
    out.sort(key=lambda entry: (-entry.context_tokens, entry.tool))
    return out


def tool_cost_table(costs: list[ToolCost]) -> list[str]:
    """Markdown rows for report.md; empty when no tool ran."""
    if not costs:
        return []
    lines = [
        "",
        "## Tool cost",
        "",
        (
            "`context tokens` = result tokens × the provider calls that re-sent them "
            "afterwards. Result sizes are estimated at ~4 chars/token, so treat these as "
            "relative weights rather than billing figures."
        ),
        "",
        "| Tool | Calls | Live | Errors | Result tokens | Context tokens | Time (ms) |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    lines.extend(
        f"| {entry.tool} | {entry.calls} | {entry.live_calls} | {entry.errors} "
        f"| {entry.result_tokens} | {entry.context_tokens} | {entry.duration_ms} |"
        for entry in costs
    )
    return lines
