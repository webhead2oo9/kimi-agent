import json

import pytest

from evals.compare import (
    compare_summaries,
    load_summary,
    render_comparison,
    scenario_pass_rate,
    shared_tapes,
)


def _summary(run_id, scores, total, model="gpt-5.6-sol"):
    return {
        "kind": "harness-eval",
        "version": 1,
        "run_id": run_id,
        "git_sha": "abc",
        "model": model,
        "scenarios": {
            scenario_id: {"aggregate": {"score_mean": score}}
            for scenario_id, score in scores.items()
        },
        "totals": {"score_mean": total},
    }


def test_compare_summaries_computes_deltas_and_orphans():
    a = _summary("runA", {"s1": 90.0, "s2": 80.0, "gone": 50.0}, total=73.3)
    b = _summary("runB", {"s1": 95.0, "s2": 70.0, "new": 60.0}, total=75.0)
    comparison = compare_summaries(a, b)

    by_id = {d.scenario_id: d for d in comparison.deltas}
    assert by_id["s1"].delta == 5.0
    assert by_id["s2"].delta == -10.0
    assert comparison.only_in_a == ["gone"]
    assert comparison.only_in_b == ["new"]
    assert comparison.total_delta == 1.7
    assert [d.scenario_id for d in comparison.improved(2.0)] == ["s1"]
    assert [d.scenario_id for d in comparison.regressed(2.0)] == ["s2"]


def test_render_comparison_marks_regressions_and_model_mismatch():
    a = _summary("runA", {"s1": 90.0}, total=90.0)
    b = _summary("runB", {"s1": 80.0}, total=80.0, model="other-model")
    comparison = compare_summaries(a, b)
    text = render_comparison(a, b, comparison, epsilon=2.0)
    assert "-10.0 (REGRESSED)" in text
    assert "WARNING: different models" in text


def _rated(run_id, rates, *, model="gpt-5.6-sol", repeat=3, **extra):
    """A summary carrying per-scenario pass rates (the post-change shape)."""
    summary = {
        "kind": "harness-eval",
        "version": 1,
        "run_id": run_id,
        "git_sha": "abc",
        "model": model,
        "repeat": repeat,
        "scenarios": {
            scenario_id: {"aggregate": {"score_mean": 50.0, "pass_rate": rate}}
            for scenario_id, rate in rates.items()
        },
        "totals": {"score_mean": 50.0},
    }
    summary.update(extra)
    return summary


def _tapes(run_id, rates, *, model, key, provenance="model", **extra):
    """A run whose tape provenance is fully recorded (the post-change shape)."""
    return _rated(
        run_id,
        rates,
        model=model,
        cassette_model_key=key,
        cassette_tapes=dict.fromkeys(rates, provenance),
        **extra,
    )


def test_failed_in_both_flagged_harness_suspect_across_two_models():
    rates = {"wolfram-derivative": 0.0, "ok": 1.0}
    a = _tapes("runA", rates, model="deepseek", key="deepseek-v4-flash")
    b = _tapes("runB", rates, model="minimax", key="minimax-m3")
    comparison = compare_summaries(a, b)

    assert comparison.failed_both == ["wolfram-derivative"]
    text = render_comparison(a, b, comparison, epsilon=2.0)
    assert "harness-suspect across deepseek + minimax" in text
    assert "wolfram-derivative" in text
    assert "LOW CONFIDENCE" not in text


def test_failed_in_both_is_not_harness_suspect_for_one_model():
    a = _rated("runA", {"s1": 0.0}, model="deepseek")
    b = _rated("runB", {"s1": 0.0}, model="deepseek")
    comparison = compare_summaries(a, b)

    assert comparison.failed_both == ["s1"]
    text = render_comparison(a, b, comparison, epsilon=2.0)
    assert "harness-suspect" not in text
    assert "same model (deepseek) in both arms" in text


def test_failed_in_both_marked_low_confidence_for_single_rep_runs():
    a = _tapes("runA", {"s1": 0.0}, model="deepseek", key="deepseek-v4-flash", repeat=1)
    b = _tapes("runB", {"s1": 0.0}, model="minimax", key="minimax-m3", repeat=3)
    text = render_comparison(a, b, compare_summaries(a, b), epsilon=2.0)

    assert "LOW CONFIDENCE" in text
    assert "single-rep run(s) runA" in text
    # Provenance is recorded on both sides, so only the rep-count caveat fires.
    assert "unrecorded cassette provenance" not in text


def test_failed_in_both_marked_low_confidence_without_provenance():
    # A summary without cassette provenance cannot prove independent recordings,
    # so the comparison must surface that uncertainty.
    a = _rated("runA", {"s1": 0.0}, model="deepseek")
    b = _tapes("runB", {"s1": 0.0}, model="minimax", key="minimax-m3")
    text = render_comparison(a, b, compare_summaries(a, b), epsilon=2.0)

    assert "harness-suspect across deepseek + minimax" in text
    assert "LOW CONFIDENCE" in text
    assert "unrecorded cassette provenance in runA" in text


def test_failed_in_both_marked_low_confidence_for_shared_cassette_tape():
    a = _tapes("runA", {"s1": 0.0}, model="deepseek", key="a", provenance="shared")
    b = _tapes("runB", {"s1": 0.0}, model="minimax", key="b", provenance="shared")
    text = render_comparison(a, b, compare_summaries(a, b), epsilon=2.0)

    assert "LOW CONFIDENCE" in text
    assert "shared cassette recording(s) for s1" in text


def test_promoted_baseline_tapes_are_still_one_observation():
    # The steady state after the promotion mechanism: each arm has its own tape
    # file with its own model key, and both replay the byte-identical baseline
    # recordings that were copied into them. Separate files are not separate
    # observations, so "harness-suspect across two models" needs the marker.
    a = _rated(
        "runA",
        {"s1": 0.0},
        model="deepseek",
        cassette_model_key="deepseek-v4-flash",
        cassette_tapes={"s1": "promoted"},
    )
    b = _rated(
        "runB",
        {"s1": 0.0},
        model="minimax",
        cassette_model_key="minimax-m3",
        cassette_tapes={"s1": "promoted"},
    )
    assert shared_tapes(a, b, ["s1"]) == ["s1"]
    text = render_comparison(a, b, compare_summaries(a, b), epsilon=2.0)
    assert "LOW CONFIDENCE" in text
    assert "shared cassette recording(s) for s1" in text

    # One arm promoted, the other read the baseline directly: same bytes.
    assert shared_tapes(a, {**b, "cassette_tapes": {"s1": "shared"}}, ["s1"]) == ["s1"]
    # An arm that recorded the scenario itself is an independent observation.
    assert shared_tapes(a, {**b, "cassette_tapes": {"s1": "model"}}, ["s1"]) == []


def test_shared_tapes_flags_one_tape_file_read_under_two_provenances():
    # One tape file (same cassette_model_key), read by a run that replayed its own
    # recordings and by a run that replayed a promoted baseline entry out of it.
    # The bytes are literally the same file on disk, so requiring "model" on both
    # sides let the correlated pair print unmarked.
    a = _rated(
        "runA",
        {"s1": 0.0},
        model="deepseek",
        cassette_model_key="one-key",
        cassette_tapes={"s1": "model"},
    )
    b = _rated(
        "runB",
        {"s1": 0.0},
        model="minimax",
        cassette_model_key="one-key",
        cassette_tapes={"s1": "promoted"},
    )
    assert shared_tapes(a, b, ["s1"]) == ["s1"]
    assert shared_tapes(b, a, ["s1"]) == ["s1"]
    text = render_comparison(a, b, compare_summaries(a, b), epsilon=2.0)
    assert "LOW CONFIDENCE" in text
    assert "shared cassette recording(s) for s1" in text

    # "none" is no tape at all: every call went live, so nothing is correlated.
    no_tape = {**b, "cassette_tapes": {"s1": "none"}}
    assert shared_tapes(a, no_tape, ["s1"]) == []


def test_shared_tapes_ignores_distinct_per_model_tapes():
    a = _rated(
        "runA",
        {"s1": 0.0},
        model="deepseek",
        cassette_model_key="deepseek-v4-flash",
        cassette_tapes={"s1": "model"},
    )
    b = _rated(
        "runB",
        {"s1": 0.0},
        model="minimax",
        cassette_model_key="minimax-m3",
        cassette_tapes={"s1": "model"},
    )
    assert shared_tapes(a, b, ["s1"]) == []
    assert shared_tapes(a, {**b, "cassette_model_key": "deepseek-v4-flash"}, ["s1"]) == ["s1"]


def test_flipped_scenarios_listed_when_pass_flips():
    a = _rated("runA", {"s1": 1.0, "s2": 0.0}, model="deepseek")
    b = _rated("runB", {"s1": 0.0, "s2": 0.5}, model="minimax")
    comparison = compare_summaries(a, b)

    assert comparison.flipped == ["s1"]
    assert comparison.failed_both == []
    text = render_comparison(a, b, comparison, epsilon=2.0)
    assert "Flipped (passed in one run, failed in the other): s1" in text


def test_minimal_summary_without_pass_rate_or_reps_is_not_a_failure():
    # Pre-change run dirs carry only aggregate.score_mean; inventing 0.0 there
    # would manufacture a harness-suspect list out of missing data.
    a = _summary("runA", {"s1": 10.0}, total=10.0)
    b = _summary("runB", {"s1": 10.0}, total=10.0, model="other-model")
    comparison = compare_summaries(a, b)

    assert comparison.failed_both == []
    assert comparison.flipped == []
    text = render_comparison(a, b, comparison, epsilon=2.0)
    assert "Failed in both runs" not in text


def test_pass_rate_derived_from_reps_when_aggregate_lacks_it():
    entry = {"aggregate": {"score_mean": 40.0}, "reps": [{"passed": False}, {"passed": True}]}
    assert scenario_pass_rate(entry) == 0.5
    assert scenario_pass_rate({"aggregate": {"score_mean": 40.0}}) is None
    assert scenario_pass_rate({"aggregate": {"pass_rate": 0.0}, "reps": [{"passed": True}]}) == 0.0


def test_load_summary_rejects_non_harness_files(tmp_path):
    path = tmp_path / "summary.json"
    path.write_text(json.dumps({"kind": "something-else"}))
    with pytest.raises(ValueError):
        load_summary(path)

    good = tmp_path / "good.json"
    good.write_text(json.dumps(_summary("r", {}, total=0.0)))
    assert load_summary(good)["run_id"] == "r"
