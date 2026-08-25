"""Cost and per-tool token accounting for eval runs."""

from __future__ import annotations

import json

from config.model_config import ModelPricing
from evals.capture import ToolCallRecord
from evals.cost import (
    ToolCost,
    mean_cost,
    recorded_call_sources,
    recorded_call_split,
    run_cost,
    sum_costs,
    tool_cost_table,
    tool_costs,
    usage_dict,
)
from evals.harness import ScenarioRun, TurnRecord
from usage.normalization import UsageBreakdown


def _record(
    tool: str,
    result: str,
    *,
    calls_before: int,
    ok: bool = True,
    source: str = "live",
) -> ToolCallRecord:
    return ToolCallRecord(
        tool=tool,
        args={},
        result=result,
        ok=ok,
        duration_ms=5,
        source=source,
        provider_calls_before=calls_before,
    )


def _turn(
    records: list[ToolCallRecord], *, provider_calls: int, usage: UsageBreakdown
) -> TurnRecord:
    return TurnRecord(
        user_message="q",
        final_text="a",
        tool_calls=records,
        tokens=usage.input_tokens + usage.output_tokens,
        latency_ms=10,
        usage=usage,
        provider_calls=provider_calls,
    )


def test_context_tokens_charge_a_result_for_every_later_provider_call() -> None:
    # The point of the metric: an identical result costs more the earlier it
    # lands, because every subsequent call re-sends it as input.
    body = "x" * 400  # 100 estimated tokens
    run = ScenarioRun(
        scenario_id="s",
        model_label="m",
        turns=[
            _turn(
                [
                    _record("early", body, calls_before=1),
                    _record("late", body, calls_before=3),
                ],
                provider_calls=4,
                usage=UsageBreakdown(input_tokens=100, output_tokens=10),
            )
        ],
    )

    by_tool = {entry.tool: entry for entry in tool_costs([run])}

    assert by_tool["early"].result_tokens == 100
    assert by_tool["late"].result_tokens == 100
    # early was carried by calls 2,3,4 -> 3x; late only by call 4 -> 1x.
    assert by_tool["early"].context_tokens == 300
    assert by_tool["late"].context_tokens == 100


def test_tool_costs_rank_by_context_cost_and_count_errors() -> None:
    run = ScenarioRun(
        scenario_id="s",
        model_label="m",
        turns=[
            _turn(
                [
                    _record("chatty", "y" * 4000, calls_before=1),
                    _record("terse", "ok", calls_before=1),
                    _record("terse", '{"error": "nope"}', calls_before=1, ok=False),
                ],
                provider_calls=2,
                usage=UsageBreakdown(input_tokens=10, output_tokens=1),
            )
        ],
    )

    costs = tool_costs([run])

    assert [entry.tool for entry in costs] == ["chatty", "terse"]
    assert costs[1].calls == 2
    assert costs[1].errors == 1


def test_a_result_from_the_final_call_is_never_re_sent() -> None:
    run = ScenarioRun(
        scenario_id="s",
        model_label="m",
        turns=[
            _turn(
                [_record("last", "z" * 400, calls_before=2)],
                provider_calls=2,
                usage=UsageBreakdown(input_tokens=10),
            )
        ],
    )

    [entry] = tool_costs([run])

    assert entry.result_tokens == 100
    assert entry.context_tokens == 0


def test_run_cost_prices_each_bucket_and_stays_none_when_unpriced() -> None:
    run = ScenarioRun(
        scenario_id="s",
        model_label="m",
        turns=[
            _turn(
                [],
                provider_calls=1,
                usage=UsageBreakdown(
                    input_tokens=1_000_000,
                    cached_read_tokens=1_000_000,
                    output_tokens=1_000_000,
                ),
            )
        ],
    )
    pricing = ModelPricing(input=2.0, cached_read=0.5, output=6.0)

    assert run_cost(run, pricing) == 8.5
    # A subscription-covered arm has no per-token price; reporting 0 would read
    # as "free" in a cost comparison rather than "not measured here".
    assert run_cost(run, None) is None


def test_usage_dict_exposes_every_priced_bucket() -> None:
    usage = UsageBreakdown(
        input_tokens=1, cached_read_tokens=2, cache_write_tokens=3, output_tokens=4
    )

    assert usage_dict(usage) == {
        "input": 1,
        "cached_read": 2,
        "cache_write": 3,
        "output": 4,
    }


def test_tool_cost_table_is_empty_when_no_tool_ran() -> None:
    assert tool_cost_table([]) == []


def test_sum_costs_keeps_real_zero() -> None:
    # A priced run that genuinely measured nothing costs $0.00; rendering it as
    # "unpriced" would hide the fact that it was measured at all.
    assert sum_costs([0.0, 0.0]) == 0.0


def test_sum_costs_returns_none_when_any_rep_unpriced() -> None:
    # Counting the unpriced rep as free would report a fraction of a mixed run's
    # bill as the whole of it.
    assert sum_costs([0.5, None]) is None
    assert sum_costs([]) is None
    assert sum_costs([0.25, 0.25]) == 0.5


def test_mean_cost_follows_the_same_unpriced_rule_as_the_total() -> None:
    # The ordinary caching case: the rep that writes cache is unpriced (no
    # cache_write rate), the reps that only read cache price fine. Averaging the
    # priced two printed a concrete per-scenario figure under an "unpriced"
    # header, over a subset the reader could not see.
    assert mean_cost([None, 0.00205, 0.00205]) is None
    assert mean_cost([]) is None
    assert mean_cost([0.1, 0.2]) == 0.15
    # A priced run that measured nothing still reads as measured.
    assert mean_cost([0.0, 0.0]) == 0.0


def test_recorded_call_split_names_every_source_present() -> None:
    # live/replay stay visible at zero ("nothing was replayed" is information);
    # a source the report never names is a call missing from a denominator the
    # reader is invited to add up.
    assert recorded_call_split({"live": 2, "replay": 0}) == "2 live / 0 replayed"
    assert (
        recorded_call_split({"live": 2, "replay": 1, "fault": 1, "miss": 3})
        == "2 live / 1 replayed / 1 faulted / 3 missed"
    )
    # An unrecognized source is rendered rather than dropped.
    assert recorded_call_split({"live": 1, "replay": 0, "wat": 2}).endswith("/ 2 wat")


def test_tool_cost_from_old_summary_dict_without_live_calls() -> None:
    # summary.json files written before the Live column must still load, since
    # evals.compare and the report renderer both reconstruct ToolCost from them.
    old_entry = json.loads(
        '{"tool": "internet_search", "calls": 3, "errors": 0, "result_chars": 100, '
        '"result_tokens": 25, "context_tokens": 50, "duration_ms": 12}'
    )

    assert ToolCost(**old_entry).live_calls == 0


def test_live_calls_count_only_dispatches_that_reached_a_backend() -> None:
    run = ScenarioRun(
        scenario_id="s",
        model_label="m",
        turns=[
            _turn(
                [
                    _record("discord_text_search", "hit", calls_before=1, source="live"),
                    _record("discord_text_search", "hit", calls_before=1, source="replay"),
                    _record("plan", "ok", calls_before=1, source="live"),
                ],
                provider_calls=2,
                usage=UsageBreakdown(input_tokens=10),
            )
        ],
    )

    by_tool = {entry.tool: entry for entry in tool_costs([run])}

    assert by_tool["discord_text_search"].calls == 2
    assert by_tool["discord_text_search"].live_calls == 1
    # Local plan calls are not cassette-eligible and stay out of this split.
    assert recorded_call_sources([run]) == {"live": 1, "replay": 1}
