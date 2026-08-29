"""Diff two harness-eval runs: the accept/reject math for a harness change.

Usage:
    .venv/bin/python -m evals.compare runA/summary.json runB/summary.json

Run A is the reference (e.g. main), run B the variant. Exit code 1 when the
variant regresses the overall mean score by more than --epsilon, so a driver
loop can gate on it directly.

Beyond the score delta it splits the failures two ways: scenarios both arms
failed (harness-suspect, when the arms are different models) and scenarios one
arm passed and the other failed. Equal overall means routinely hide a set of
flips, and a scenario no model can pass is a tool-description or prompt defect
wearing a model's score.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from evals.cassette import BASELINE_PROVENANCE
from evals.harness_run import SUMMARY_KIND


@dataclass(frozen=True)
class ScenarioDelta:
    scenario_id: str
    score_a: float
    score_b: float

    @property
    def delta(self) -> float:
        return round(self.score_b - self.score_a, 1)


@dataclass
class Comparison:
    deltas: list[ScenarioDelta]
    only_in_a: list[str]
    only_in_b: list[str]
    total_a: float
    total_b: float
    # Scenarios every arm failed outright, and scenarios one arm passed and the
    # other failed. A score delta alone cannot tell those apart: two runs can
    # report the same overall mean while disagreeing on which scenarios failed.
    failed_both: list[str] = field(default_factory=list)
    flipped: list[str] = field(default_factory=list)

    @property
    def total_delta(self) -> float:
        return round(self.total_b - self.total_a, 1)

    def improved(self, epsilon: float) -> list[ScenarioDelta]:
        return [d for d in self.deltas if d.delta > epsilon]

    def regressed(self, epsilon: float) -> list[ScenarioDelta]:
        return [d for d in self.deltas if d.delta < -epsilon]


def load_summary(path: str | Path) -> dict:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if raw.get("kind") != SUMMARY_KIND:
        raise ValueError(f"{path} is not a harness-eval summary (kind={raw.get('kind')!r})")
    return raw


def scenario_pass_rate(entry: dict) -> float | None:
    """One scenario's pass rate, or None when the summary omits the metric.

    Summaries with only `aggregate.score_mean` use their reps as the fallback.
    With neither source, "unknown" is the real answer; deriving 0.0 from an
    absent key would invent a failure.
    """
    aggregate = entry.get("aggregate") or {}
    value = aggregate.get("pass_rate")
    if value is not None:
        return float(value)
    reps = entry.get("reps") or []
    if not reps:
        return None
    return sum(1.0 for rep in reps if rep.get("passed")) / len(reps)


# Provenance values whose results were served out of a tape *file*. Two runs
# sharing a `cassette_model_key` read the same file, whether its entries are the
# arm's own recordings ("model") or copies of the baseline ("promoted"). Any
# such mix is one observation and trips the harness-suspect marker.
TAPE_FILE_PROVENANCE = ("model", "promoted")


def shared_tapes(a: dict, b: dict, scenario_ids: Sequence[str]) -> list[str]:
    """Scenarios where both runs replayed the same recording.

    Two arms reading one tape are not two independent observations: the tool
    results were byte-identical, so a shared failure can be a single sample
    wearing two hats.

    Separate tape *files* are not enough to clear a scenario, which is why
    `promoted` counts: `Cassette.save()` copies replayed baseline entries into
    the arm's own tape, so in the steady state two arms replay the same baseline
    bytes out of two files. Keying only on `shared` would make the guard stop
    firing exactly once the correlation was permanent.
    """
    same_key = bool(a.get("cassette_model_key")) and (
        a.get("cassette_model_key") == b.get("cassette_model_key")
    )
    tapes_a = a.get("cassette_tapes") or {}
    tapes_b = b.get("cassette_tapes") or {}
    matched = []
    for scenario_id in scenario_ids:
        provenance_a = tapes_a.get(scenario_id)
        provenance_b = tapes_b.get(scenario_id)
        both_baseline = provenance_a in BASELINE_PROVENANCE and provenance_b in BASELINE_PROVENANCE
        one_tape = (
            same_key
            and provenance_a in TAPE_FILE_PROVENANCE
            and provenance_b in TAPE_FILE_PROVENANCE
        )
        if both_baseline or one_tape:
            matched.append(scenario_id)
    return matched


def unrecorded_tape_provenance(a: dict, b: dict) -> list[str]:
    """Run ids whose summary lacks the per-scenario `cassette_tapes` map.

    Absent provenance is unknown, not cleared. A summary without this map may
    have replayed the shared flat-tree tape, so comparisons can contain
    correlated recordings. `shared_tapes` cannot identify that from two missing
    values; this check marks the run instead.

    Key *presence* is the version signal: `build_summary` always writes the key,
    empty map and all.
    """
    return [summary.get("run_id", "?") for summary in (a, b) if "cassette_tapes" not in summary]


def compare_summaries(a: dict, b: dict) -> Comparison:
    scenarios_a = a.get("scenarios", {})
    scenarios_b = b.get("scenarios", {})
    shared = sorted(set(scenarios_a) & set(scenarios_b))
    deltas = [
        ScenarioDelta(
            scenario_id=scenario_id,
            score_a=float(scenarios_a[scenario_id]["aggregate"]["score_mean"]),
            score_b=float(scenarios_b[scenario_id]["aggregate"]["score_mean"]),
        )
        for scenario_id in shared
    ]
    failed_both: list[str] = []
    flipped: list[str] = []
    for scenario_id in shared:
        rate_a = scenario_pass_rate(scenarios_a[scenario_id])
        rate_b = scenario_pass_rate(scenarios_b[scenario_id])
        if rate_a is None or rate_b is None:
            continue
        if rate_a == 0.0 and rate_b == 0.0:
            failed_both.append(scenario_id)
        elif {rate_a, rate_b} == {0.0, 1.0}:
            flipped.append(scenario_id)
    return Comparison(
        deltas=deltas,
        only_in_a=sorted(set(scenarios_a) - set(scenarios_b)),
        only_in_b=sorted(set(scenarios_b) - set(scenarios_a)),
        total_a=float(a.get("totals", {}).get("score_mean", 0.0)),
        total_b=float(b.get("totals", {}).get("score_mean", 0.0)),
        failed_both=failed_both,
        flipped=flipped,
    )


def model_arm_identity(summary: dict) -> tuple[str, str, str, str]:
    """Provider-aware identity; missing provider metadata falls back to model id."""
    model = str(summary.get("model") or "?")
    return (
        str(summary.get("model_label") or model),
        str(summary.get("provider_name") or ""),
        str(summary.get("provider_base_url") or ""),
        model,
    )


def model_arm_display(summary: dict) -> str:
    label, provider, endpoint, model = model_arm_identity(summary)
    details = "/".join(part for part in (provider, model) if part)
    endpoint_text = f" @ {endpoint}" if endpoint else ""
    if label == model and not provider:
        return model
    return f"{label} ({details or model}{endpoint_text})"


def _failed_both_line(a: dict, b: dict, comparison: Comparison) -> str:
    """Name what a both-arms failure is evidence *of*, not just that it happened.

    "Every run failed this" only implicates the harness when the runs were
    independent observations of different models. Two runs of one model, two
    single-rep runs, or two runs replaying one tape are correlated evidence, and
    the heading an operator acts on has to say so or it manufactures the bad
    inference it exists to prevent.
    """
    ids = ", ".join(comparison.failed_both)
    arm_a = model_arm_display(a)
    arm_b = model_arm_display(b)
    if model_arm_identity(a) == model_arm_identity(b):
        return (
            f"Failed in both runs: {ids}; same model arm ({arm_a}) in both runs, "
            "so this is that model's result, not a harness signal."
        )
    caveats = []
    single_rep = [s.get("run_id", "?") for s in (a, b) if s.get("repeat") == 1]
    if single_rep:
        caveats.append(f"single-rep run(s) {', '.join(single_rep)}")
    tapes = shared_tapes(a, b, comparison.failed_both)
    if tapes:
        # "recording", not "tape": after promotion the two arms hold separate
        # tape files carrying the same baseline bytes, and naming the file would
        # read as cleared to anyone who checked that the paths differ.
        caveats.append(f"shared cassette recording(s) for {', '.join(tapes)}")
    unknown = unrecorded_tape_provenance(a, b)
    if unknown:
        caveats.append(
            f"unrecorded cassette provenance in {', '.join(unknown)} "
            "(independent recordings cannot be verified)"
        )
    suffix = f" (LOW CONFIDENCE: {'; '.join(caveats)})" if caveats else ""
    return f"Failed in both runs (harness-suspect across {arm_a} + {arm_b}): {ids}{suffix}"


def render_comparison(a: dict, b: dict, comparison: Comparison, epsilon: float) -> str:
    arm_a = model_arm_display(a)
    arm_b = model_arm_display(b)
    lines = [
        (
            f"A (reference): {a['run_id']} | {arm_a} @ {a.get('git_sha', '?')} "
            f"(score {comparison.total_a})"
        ),
        (
            f"B (variant):   {b['run_id']} | {arm_b} @ {b.get('git_sha', '?')} "
            f"(score {comparison.total_b})"
        ),
        "",
        f"Overall delta: {comparison.total_delta:+.1f} (epsilon {epsilon})",
        "",
        "| Scenario | A | B | Delta |",
        "| --- | --- | --- | --- |",
    ]
    for d in comparison.deltas:
        marker = ""
        if d.delta > epsilon:
            marker = " (improved)"
        elif d.delta < -epsilon:
            marker = " (REGRESSED)"
        lines.append(f"| {d.scenario_id} | {d.score_a} | {d.score_b} | {d.delta:+.1f}{marker} |")
    improved = comparison.improved(epsilon)
    regressed = comparison.regressed(epsilon)
    lines += [
        "",
        (
            f"Improved: {len(improved)} | Regressed: {len(regressed)} | "
            f"Within epsilon: {len(comparison.deltas) - len(improved) - len(regressed)}"
        ),
    ]
    if comparison.failed_both:
        lines.append(_failed_both_line(a, b, comparison))
    if comparison.flipped:
        lines.append(
            f"Flipped (passed in one run, failed in the other): {', '.join(comparison.flipped)}"
        )
    if comparison.only_in_a:
        lines.append(f"Only in A (not compared): {', '.join(comparison.only_in_a)}")
    if comparison.only_in_b:
        lines.append(f"Only in B (not compared): {', '.join(comparison.only_in_b)}")
    if model_arm_identity(a) != model_arm_identity(b):
        lines.append(
            f"WARNING: different model arms ({arm_a} vs {arm_b}). "
            "This diff mixes model and harness effects."
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare two harness-eval summary.json files.")
    parser.add_argument("reference", help="summary.json of the reference run (A)")
    parser.add_argument("variant", help="summary.json of the variant run (B)")
    parser.add_argument(
        "--epsilon",
        type=float,
        default=2.0,
        help="score-delta noise floor; regressions beyond it fail the compare",
    )
    args = parser.parse_args()
    a = load_summary(args.reference)
    b = load_summary(args.variant)
    comparison = compare_summaries(a, b)
    print(render_comparison(a, b, comparison, args.epsilon))
    failed = comparison.total_delta < -args.epsilon
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
