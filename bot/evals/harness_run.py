"""Harness-eval runner: one model, N repetitions, mechanical scoring.

Where `evals.run` qualifies a candidate *model* against the baseline (judge
included), this runner holds the model fixed and measures the *harness*
(prompts, tool descriptions, error text), so two runs on different working
trees can be diffed with `evals.compare`. No judge: the reward is the
deterministic mechanical score, repeated `--repeat` times per scenario so a
delta between runs is signal rather than sampling noise.

Tool calls flow through the per-scenario cassette (evals/cassette.py): the
default hybrid `replay` mode replays recorded results and records misses live,
so the first run captures the backends and later runs are deterministic and
free. `record` re-records from scratch; `strict` fails misses instead of
falling through (CI determinism); `off` is fully live. Tapes are keyed by model
(`<cassettes>/<model-key>/<scenario>.json`) over the read-only shared baseline
in the flat tree, so a run never rewrites another arm's recordings.

Run: uv run python -m evals.harness_run --model sol --repeat 3
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import subprocess
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean
from typing import Any

from app.providers import close_provider
from config.settings import settings
from evals.capture import InstrumentedProvider
from evals.cassette import (
    CASSETTE_MODES,
    assert_unique_model_keys,
    cassette_model_key,
    load_cassette,
    tape_provenance,
)
from evals.harness import ScenarioRun, run_scenario_for_model
from evals.identity import EvalIdentity, new_eval_run_nonce
from evals.mechanical import MechanicalResult, compute_mechanical
from evals.models import ModelsConfig, ModelSpec, build_eval_provider, load_models
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
from evals.registry import build_eval_registry
from usage.normalization import UsageBreakdown

from evals.scenario import (
    Scenario,
    load_scenarios,
    split_gated_scenarios,
    split_image_scenarios,
)
from evals.stub_gateway import StubGateway

log = logging.getLogger("evals.harness")

EVALS_DIR = Path(__file__).resolve().parent
SUMMARY_KIND = "harness-eval"
SUMMARY_VERSION = 2
_TRANSCRIPT_RESULT_CAP = 2000


def resolve_model_spec(models: ModelsConfig, name: str) -> ModelSpec | None:
    """Look up a single-arm model by candidate name or the baseline's label."""
    if name in models.candidates:
        return models.candidates[name]
    if name == models.baseline.label:
        return models.baseline
    return None


def _git(repo_dir: Path, args: list[str]) -> str | None:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=10,
        )
    # TimeoutExpired subclasses SubprocessError, not OSError, so it needs naming.
    except OSError, UnicodeError, subprocess.TimeoutExpired:
        return None
    return proc.stdout if proc.returncode == 0 else None


def _git_bytes(repo_dir: Path, args: list[str]) -> bytes | None:
    """Run git without decoding output, for diffs that may contain binary data."""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=repo_dir,
            capture_output=True,
            timeout=10,
        )
    except OSError, subprocess.TimeoutExpired:
        return None
    return proc.stdout if proc.returncode == 0 else None


def run_data_paths(repo_dir: Path, cassette_dir: Path | str) -> tuple[str, ...]:
    """Repo-relative pathspec(s) holding run *data*, excluded from the dirty check.

    A `replay` run records misses and promotes baseline entries into
    `<cassettes>/<model-key>/`. The default tree is ignored, but callers may put
    tapes in another tracked path; without the exclusion that run stamps itself
    `-dirty` from its own write and two runs of identical code report different
    trees, which is the marker-loses-meaning failure the untracked exclusion
    already guards against.

    Derived from the tape directory the run actually uses rather than hardcoded
    to the default: `--cassettes <some other tracked path>` writes there instead,
    so a fixed `evals/cassettes` would stop covering the run's own writes (the
    original self-dirtying bug, back) while excusing a tree the run never touched.
    Returns nothing when the tapes live outside the repo (they cannot dirty it)
    or when git cannot locate the repo root, which leaves the check at its
    cautious setting.
    """
    top = _git(repo_dir, ["rev-parse", "--show-toplevel"])
    if top is None or not top.strip():
        return ()
    try:
        relative = Path(cassette_dir).resolve().relative_to(Path(top.strip()).resolve())
    except OSError, ValueError:
        return ()
    text = relative.as_posix()
    return (text,) if text and text != "." else ()


def git_short_sha(repo_dir: Path, *, data_paths: Sequence[str] = ()) -> str:
    """HEAD's short sha, with a hash of effective tracked worktree changes.

    The sha is the run's identity: two runs stamped the same sha are supposed to
    have executed the same code, and without the dirty check a run against an
    edited working tree claims a tree it never ran. So the marker has to mean
    "the executed tracked bytes differ" and nothing else: a diff from HEAD
    naturally excludes untracked files and index-only state, and everything
    under `data_paths` is excluded because a run writes it as part of doing its
    job (see `run_data_paths`). A bare `-dirty` makes every edited tree look
    identical, so the suffix includes the first 12 hex characters of SHA-256
    over a configuration-independent binary diff from HEAD. Pathspecs are
    anchored at the repo root (`:(top)`) because the check runs from evals/.

    Failure is cautious, not clean: `rev-parse` succeeding proves git works, so a
    diff that then fails (the timeout on a slow tree, a permissions error) is the
    *ambiguous* case, and answering it with a bare sha is exactly the "claimed a
    commit it never executed" failure the marker exists to prevent. `-unknown`
    says the check could not be run.
    """
    sha = _git(repo_dir, ["rev-parse", "--short", "HEAD"])
    if sha is None or not sha.strip():
        return "nogit"
    pathspecs = [
        ":(top)",
        *(f":(top,exclude){path}" for path in data_paths),
    ]
    diff = _git_bytes(
        repo_dir,
        [
            "-c",
            "core.quotePath=true",
            "-c",
            "diff.ignoreSubmodules=none",
            "diff",
            "--binary",
            "--full-index",
            "--no-ext-diff",
            "--no-textconv",
            "--no-color",
            "--no-renames",
            "--diff-algorithm=myers",
            "--no-indent-heuristic",
            "--unified=3",
            "--inter-hunk-context=0",
            "-O/dev/null",
            "--submodule=short",
            "--ignore-submodules=none",
            "--src-prefix=a/",
            "--dst-prefix=b/",
            "HEAD",
            "--",
            *pathspecs,
        ],
    )
    if diff is None:
        return f"{sha.strip()}-unknown"
    if not diff:
        return sha.strip()
    digest = hashlib.sha256(diff).hexdigest()[:12]
    return f"{sha.strip()}-dirty-{digest}"


def make_run_dir(base: Path, *, sha: str, now: datetime | None = None) -> Path:
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%d-%H%M%S")
    run_dir = base / f"{stamp}-{sha}"
    suffix = 1
    while run_dir.exists():
        run_dir = base / f"{stamp}-{sha}-{suffix}"
        suffix += 1
    run_dir.mkdir(parents=True)
    return run_dir


def missing_expected_tools(scenarios: list[Scenario], registered: set[str]) -> dict[str, list[str]]:
    """Expected tools that are not registered at all (env gate off, missing key...).

    Without this check a scenario 'fails' because the dev box lacks an API key
    and the loop optimizes against a phantom regression.
    """
    problems: dict[str, list[str]] = {}
    for scenario in scenarios:
        missing = [t for t in scenario.expect.should_use_tools if t not in registered]
        if missing:
            problems[scenario.id] = missing
    return problems


@dataclass
class RepResult:
    mechanical: MechanicalResult
    sources: dict[str, int]
    run: ScenarioRun
    # None when the arm declares no pricing (subscription-covered), which reads
    # as "unpriced" everywhere downstream rather than as free.
    cost: float | None = None


def _rep_row(rep: RepResult) -> dict:
    row = asdict(rep.mechanical)
    row["passed"] = rep.mechanical.passed
    row["eval_identity"] = rep.run.identity.as_dict() if rep.run.identity else None
    row["sources"] = rep.sources
    row["usage"] = usage_dict(rep.run.total_usage)
    row["user_turns"] = len(rep.run.turns)
    row["model_turns"] = rep.run.provider_calls
    row["provider_calls"] = rep.run.provider_calls
    row["effective_output_tokens_per_second"] = _effective_output_tokens_per_second(
        output_tokens=rep.run.total_usage.output_tokens,
        provider_latency_ms=rep.run.total_latency_ms,
    )
    row["est_cost_usd"] = rep.cost
    row["recorded_tool_calls"] = recorded_call_sources([rep.run])
    return row


def _effective_output_tokens_per_second(
    *, output_tokens: int, provider_latency_ms: int
) -> float | None:
    """Return user-observed throughput across complete provider calls."""
    if output_tokens <= 0 or provider_latency_ms <= 0:
        return None
    return round(output_tokens * 1000 / provider_latency_ms, 2)


def _aggregate(reps: list[RepResult]) -> dict:
    scores = [rep.mechanical.score for rep in reps]
    wall_times = [rep.run.wall_time_ms for rep in reps]
    provider_times = [rep.run.total_latency_ms for rep in reps]
    output_tokens = sum(rep.run.total_usage.output_tokens for rep in reps)
    return {
        "score_mean": round(mean(scores), 1),
        "score_min": min(scores),
        "score_max": max(scores),
        "pass_rate": round(sum(1 for r in reps if r.mechanical.passed) / len(reps), 2),
        "tool_calls_mean": round(mean(r.mechanical.tool_call_count for r in reps), 1),
        "tokens_mean": round(mean(r.mechanical.tokens for r in reps), 1),
        "user_turns_mean": round(mean(len(r.run.turns) for r in reps), 1),
        "model_turns_mean": round(mean(r.run.provider_calls for r in reps), 1),
        "provider_calls_mean": round(mean(r.run.provider_calls for r in reps), 1),
        # Timing is diagnostic only. Wall time includes tools and local harness
        # work; provider latency is the summed time inside model API calls.
        "wall_time_mean_ms": round(mean(wall_times)),
        "wall_time_min_ms": min(wall_times),
        "wall_time_max_ms": max(wall_times),
        "provider_latency_mean_ms": round(mean(provider_times)),
        "effective_output_tokens_per_second": _effective_output_tokens_per_second(
            output_tokens=output_tokens,
            provider_latency_ms=sum(provider_times),
        ),
        # Absent pricing stays None all the way up rather than collapsing to 0,
        # and one unpriced rep makes the whole mean None, matching `sum_costs` at
        # run scope. Averaging the priced reps alone printed a concrete Cost cell
        # under an `unpriced` header, over a subset the reader could not see.
        "cost_mean_usd": mean_cost([rep.cost for rep in reps]),
    }


def build_summary(
    *,
    run_id: str,
    git_sha: str,
    model: str,
    repeat: int,
    cassette_mode: str,
    registered_tools: list[str],
    results: dict[str, tuple[Scenario, list[RepResult]]],
    max_tokens: int = 65_536,
    requested_max_tokens: int | None = None,
    cassette_dir: str = "",
    cassette_model_key: str = "",
    cassette_tapes: dict[str, str] | None = None,
    skipped_scenarios: dict[str, list[str]] | None = None,
    eval_run_nonce: str = "",
) -> dict:
    scenarios: dict[str, dict[str, Any]] = {
        scenario_id: {
            "category": scenario.category,
            "reps": [_rep_row(rep) for rep in reps],
            "aggregate": _aggregate(reps),
        }
        for scenario_id, (scenario, reps) in results.items()
    }
    aggregates: list[dict[str, Any]] = [entry["aggregate"] for entry in scenarios.values()]
    reps = [rep for _, scenario_reps in results.values() for rep in scenario_reps]
    est_cost = sum_costs([rep.cost for rep in reps])
    total_usage = sum((rep.run.total_usage for rep in reps), UsageBreakdown())
    provider_latency_ms = sum(rep.run.total_latency_ms for rep in reps)
    totals = {
        "score_mean": round(mean(a["score_mean"] for a in aggregates), 1) if aggregates else 0.0,
        "pass_rate": round(mean(a["pass_rate"] for a in aggregates), 2) if aggregates else 0.0,
        "tool_calls": sum(rep.mechanical.tool_call_count for rep in reps),
        "tokens": sum(rep.mechanical.tokens for rep in reps),
        "user_turns": sum(len(rep.run.turns) for rep in reps),
        "model_turns": sum(rep.run.provider_calls for rep in reps),
        "wall_time_ms": sum(rep.run.wall_time_ms for rep in reps),
        "provider_latency_ms": provider_latency_ms,
        "usage": usage_dict(total_usage),
        "effective_output_tokens_per_second": _effective_output_tokens_per_second(
            output_tokens=total_usage.output_tokens,
            provider_latency_ms=provider_latency_ms,
        ),
        "est_cost_usd": est_cost,
    }
    tools = tool_costs([rep.run for rep in reps])
    return {
        "kind": SUMMARY_KIND,
        "version": SUMMARY_VERSION,
        "run_id": run_id,
        "generated_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "git_sha": git_sha,
        "model": model,
        "eval_run_nonce": eval_run_nonce,
        "repeat": repeat,
        "max_tokens": max_tokens,
        "requested_max_tokens": requested_max_tokens or max_tokens,
        "cassette_mode": cassette_mode,
        "cassette_dir": cassette_dir,
        "cassette_model_key": cassette_model_key,
        # Per-scenario tape provenance ("model"/"promoted"/"shared"/"none"). Without it a
        # reader cannot tell whether a run replayed its own recordings, another
        # arm's, or none at all. Both motivating runs were ~95% live and the
        # report said nothing.
        "cassette_tapes": cassette_tapes or {},
        "registered_tools": registered_tools,
        # Scenario id -> tools this host does not register. A run that silently
        # dropped its sandbox-only scenarios would read as full coverage.
        "skipped_scenarios": skipped_scenarios or {},
        "scenarios": scenarios,
        "tool_costs": [entry.as_dict() for entry in tools],
        # Live/replay/fault/miss split over cassette-eligible calls.
        "recorded_tool_calls": recorded_call_sources([rep.run for rep in reps]),
        "totals": totals,
    }


def render_harness_report(summary: dict) -> str:
    totals = summary["totals"]
    recorded = summary.get("recorded_tool_calls") or {}
    costs = [ToolCost(**entry) for entry in summary.get("tool_costs", [])]
    max_tokens = int(summary.get("max_tokens", 65_536))
    requested_max_tokens = int(summary.get("requested_max_tokens", max_tokens))
    max_tokens_cell = str(max_tokens)
    if requested_max_tokens != max_tokens:
        max_tokens_cell += f" effective (requested {requested_max_tokens})"
    lines = [
        f"# Harness eval: {summary['model']} @ {summary['git_sha']}",
        "",
        (
            f"**Run:** {summary['run_id']} | **Repeats:** {summary['repeat']} | "
            f"**Max tokens/call:** {max_tokens_cell} | "
            f"**Cassette:** {_cassette_cell(summary)}"
        ),
        (
            f"**Overall score:** {totals['score_mean']} | "
            f"**Pass rate:** {totals['pass_rate']} | "
            f"**Tokens:** {totals['tokens']}"
        ),
        (
            f"**Completion time:** {_seconds(totals.get('wall_time_ms', 0))} end-to-end | "
            f"{_seconds(totals.get('provider_latency_ms', 0))} in provider calls "
            "(informational; not scored)"
        ),
        (
            f"**Turns:** {totals.get('user_turns', 0)} user / "
            f"{totals.get('model_turns', 0)} model | "
            "**Effective output rate:** "
            f"{_tokens_per_second(totals.get('effective_output_tokens_per_second'))} tok/s"
        ),
        (
            f"**Cost:** {_usd(totals.get('est_cost_usd'))} tokens | "
            f"**Recorded tool calls:** {recorded_call_split(recorded)}"
        ),
        "",
        "| Scenario | Score (min–max) | Pass | Calls | Tokens | Turns (user / model) | Time (wall / provider) | Output tok/s | Cost | Flags |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for scenario_id, entry in summary["scenarios"].items():
        agg = entry["aggregate"]
        flags: list[str] = []
        for rep in entry["reps"]:
            flags.extend(f"missing:{t}" for t in rep["missing_tools"])
            flags.extend(f"unexpected:{t}" for t in rep["unexpected_tools"])
            if rep["unrecovered_errors"]:
                flags.append(f"unrecovered:{rep['unrecovered_errors']}")
            if rep["failed_reply_checks"]:
                flags.append("reply-check")
            if rep["over_budget"]:
                flags.append("over-budget")
            if rep.get("missing_attachment"):
                flags.append("no-attachment")
            if rep["raw_json_reply"]:
                flags.append("raw-json")
            if rep["repeated_calls"]:
                flags.append(f"repeats:{rep['repeated_calls']}")
            if rep.get("live_tool_errors", rep["tool_errors"]):
                flags.append(f"tool-errors:{rep.get('live_tool_errors', rep['tool_errors'])}")
            flags.extend(f"incomplete:{turn}" for turn in rep.get("incomplete_turns", []))
        unique_flags = sorted(set(flags))
        lines.append(
            f"| {scenario_id} | {agg['score_mean']} ({agg['score_min']}–{agg['score_max']}) "
            f"| {agg['pass_rate']} | {agg['tool_calls_mean']} | {agg['tokens_mean']} "
            f"| {float(agg.get('user_turns_mean', 0)):.1f} / "
            f"{float(agg.get('model_turns_mean', 0)):.1f} "
            f"| {_seconds(agg['wall_time_mean_ms'])} / "
            f"{_seconds(agg['provider_latency_mean_ms'])} "
            f"| {_tokens_per_second(agg.get('effective_output_tokens_per_second'))} "
            f"| {_usd(agg.get('cost_mean_usd'))} "
            f"| {', '.join(unique_flags) or 'clean'} |"
        )
    # Named explicitly, never omitted: a reader comparing two runs has to be able to
    # see that one of them sat out the sandbox-only scenarios.
    skipped = summary.get("skipped_scenarios") or {}
    if skipped:
        lines += ["", "## Skipped (host lacks the required tools)", ""]
        lines += [f"- `{sid}`: needs {', '.join(tools)}" for sid, tools in sorted(skipped.items())]
    lines.extend(tool_cost_table(costs))
    return "\n".join(lines) + "\n"


def _cassette_cell(summary: dict) -> str:
    """Mode plus the tape provenance split, since the mode alone hides a fully live run."""
    mode = summary["cassette_mode"]
    tapes = summary.get("cassette_tapes") or {}
    if not tapes:
        return str(mode)
    counts = {"model": 0, "promoted": 0, "shared": 0, "none": 0}
    for provenance in tapes.values():
        counts[provenance] = counts.get(provenance, 0) + 1
    # "promoted" is broken out rather than folded into "model": the tape is this
    # arm's own file, but the results it replayed came from the shared baseline,
    # so counting it as an own recording overstates how independent the run is.
    return (
        f"{mode} ({counts['model']} model tapes, {counts['promoted']} promoted-baseline, "
        f"{counts['shared']} shared-fallback, {counts['none']} none)"
    )


def _usd(value: float | None) -> str:
    """Render a cost, distinguishing "no price configured" from "cost nothing"."""
    if value is None:
        return "unpriced"
    if value and abs(value) < 0.0001:
        return f"${value:.6f}"
    return f"${value:.4f}"


def _seconds(milliseconds: int | float) -> str:
    return f"{float(milliseconds) / 1000:.2f}s"


def _tokens_per_second(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def write_transcripts(path: Path, results: dict[str, tuple[Scenario, list[RepResult]]]) -> None:
    """Full per-rep transcripts (tool results capped) for failure triage."""
    with path.open("w", encoding="utf-8") as handle:
        for scenario_id, (_, reps) in results.items():
            for index, rep in enumerate(reps):
                row = {
                    "scenario": scenario_id,
                    "rep": index,
                    "eval_identity": rep.run.identity.as_dict() if rep.run.identity else None,
                    "score": rep.mechanical.score,
                    "passed": rep.mechanical.passed,
                    "turns": [
                        {
                            "user": turn.user_message,
                            "reply": turn.final_text,
                            "termination_reason": turn.termination_reason,
                            "provider_calls": turn.provider_calls,
                            "tool_calls": [
                                {
                                    "tool": record.tool,
                                    "args": record.args,
                                    "ok": record.ok,
                                    "source": record.source,
                                    "result": record.result[:_TRANSCRIPT_RESULT_CAP],
                                }
                                for record in turn.tool_calls
                            ],
                        }
                        for turn in rep.run.turns
                    ],
                }
                handle.write(json.dumps(row, default=str) + "\n")


async def _run(args: argparse.Namespace) -> int:
    if args.repeat == 1:
        # Observed: one scenario swung 100 -> 35 between identical reps purely
        # from tool choice. A single sample per scenario is a coin flip dressed
        # as a measurement, and the default is 3 for that reason.
        log.warning(
            "--repeat 1 is a single sample per scenario; per-scenario scores from this "
            "run are unsound for comparison (default is 3)."
        )
    models = load_models(args.models)
    spec = resolve_model_spec(models, args.model)
    if spec is None:
        known = sorted([*models.candidates, models.baseline.label])
        log.error("Unknown model %r; known: %s", args.model, known)
        return 2
    max_tokens = spec.effective_max_tokens(args.max_tokens)
    if max_tokens != args.max_tokens:
        log.warning(
            "%s caps output at %d tokens; clamping requested --max-tokens %d.",
            spec.label,
            max_tokens,
            args.max_tokens,
        )
    try:
        assert_unique_model_keys(
            [models.baseline.label, *(c.label for c in models.candidates.values())]
        )
    except ValueError as exc:
        log.error("%s", exc)
        return 2
    model_key = cassette_model_key(spec.label)
    scenarios = load_scenarios(args.scenarios)
    # Resolve the runnable set *before* the dry-run print, or the plan reports
    # scenarios (and a call count) the real run would skip, which is the one
    # thing a dry run exists to get right.
    #
    # A text-only model given an image scenario either 400s or answers from the
    # caption alone, and either way scores as a model weakness rather than an
    # unrunnable scenario. Skip loudly rather than refusing the whole run: the
    # image scenarios sit in the default tree, so refusing would block every
    # ordinary harness run on a text-only arm.
    scenarios, visual = split_image_scenarios(scenarios)
    if visual and spec.supports_images():
        scenarios = [*scenarios, *visual]
    elif visual:
        log.warning(
            "Skipping %d image scenario(s). %s does not declare image_input in "
            "evals/models.yaml: %s",
            len(visual),
            spec.label,
            ", ".join(s.id for s in visual),
        )
    if not scenarios:
        log.error("No runnable scenarios for %s; every selected scenario needs images.", spec.label)
        return 2

    cassette_dir = Path(args.cassettes)
    shared_fallback = args.cassette != "record" and not args.no_shared_cassettes

    if args.dry_run:
        print(f"Model:     {spec.label} ({spec.model})")
        max_tokens_note = f" (requested {args.max_tokens})" if max_tokens != args.max_tokens else ""
        print(f"Max tokens/call: {max_tokens}{max_tokens_note}")
        print(f"Scenarios: {', '.join(s.id for s in scenarios)}")
        print(f"Cassette:  {args.cassette} (dir: {cassette_dir}, tapes: {model_key})")
        # Tape provenance before anything is spent: a scenario with no tape runs
        # fully live, which is the one thing a dry run should be able to warn about.
        for scenario in scenarios:
            _, provenance = load_cassette(
                cassette_dir, scenario.id, model_key, shared_fallback=shared_fallback
            )
            print(f"  {scenario.id}: {provenance}")
        live = args.cassette in ("off", "record")
        per_scenario = "all live" if live else "replayed where recorded"
        print(
            f"Total run_conversation calls: {len(scenarios) * args.repeat} "
            f"({len(scenarios)} scenarios x {args.repeat} reps, tools {per_scenario})"
        )
        return 0

    provider = InstrumentedProvider(build_eval_provider(spec))
    eval_run_nonce = new_eval_run_nonce()
    gateway = StubGateway()
    tapes: dict[str, str] = {}
    results: dict[str, tuple[Scenario, list[RepResult]]] = {}
    skipped: dict[str, list[str]] = {}
    eval_registry = None
    try:
        eval_registry = await build_eval_registry(settings, gateway=gateway)
        registry = eval_registry.registry
        registered = {entry.name for entry in registry.get_all_tools()}
        # Capability gate first: a scenario that DECLARED it needs a sandbox-only tool
        # sits out on a host without one. Everything left is expected to work here, so
        # missing_expected_tools keeps its hard failure for genuine gaps.
        scenarios, gated = split_gated_scenarios(scenarios, registered)
        for gated_scenario, missing in gated:
            log.warning(
                "Skipping %s: this host does not register %s",
                gated_scenario.id,
                ", ".join(missing),
            )
            skipped[gated_scenario.id] = missing
        if not scenarios:
            log.error("Every selected scenario needs a tool this host does not register.")
            return 2
        problems = missing_expected_tools(scenarios, registered)
        if problems:
            for scenario_id, tools in problems.items():
                log.error("Scenario %s expects unregistered tools: %s", scenario_id, tools)
            log.error("Refusing to run against a partial tool surface (check .env gates).")
            return 2
        compactor = eval_registry.provider_manager.build_compactor()
        for scenario in scenarios:
            # record must not inherit an underlay it will never rewrite: the point
            # of the mode is a tape recorded from scratch by this arm.
            cassette, tapes[scenario.id] = load_cassette(
                cassette_dir, scenario.id, model_key, shared_fallback=shared_fallback
            )
            if args.cassette == "record":
                cassette.clear()
            reps: list[RepResult] = []
            registry.configure_cassette(cassette, args.cassette)
            for rep_index in range(args.repeat):
                cassette.reset_cursors()
                registry.set_faults(scenario.faults)
                run = await run_scenario_for_model(
                    scenario,
                    provider=provider,
                    registry=registry,
                    gateway=gateway,
                    memory_client=eval_registry.memory_manager.active_client(),
                    preference_store=eval_registry.preference_store,
                    bot_name=settings.bot_name,
                    thread_handoff_suggest_after_tool_calls=(
                        settings.thread_handoff_suggest_after_tool_calls
                    ),
                    compactor=compactor,
                    max_tokens=max_tokens,
                    identity=EvalIdentity(
                        run_nonce=eval_run_nonce,
                        arm=spec.label,
                        scenario_id=scenario.id,
                        repetition=rep_index,
                    ),
                )
                sources: dict[str, int] = {}
                for record in run.all_tool_calls:
                    sources[record.source] = sources.get(record.source, 0) + 1
                mech = compute_mechanical(scenario, run)
                reps.append(
                    RepResult(
                        mechanical=mech,
                        sources=sources,
                        run=run,
                        cost=run_cost(run, spec.pricing),
                    )
                )
                log.info(
                    "%s rep %d/%d: score %.1f (%s)",
                    scenario.id,
                    rep_index + 1,
                    args.repeat,
                    mech.score,
                    "pass" if mech.passed else "fail",
                )
            registry.configure_cassette(None)
            registry.set_faults([])
            if args.cassette in ("record", "replay"):
                cassette.save()
            tapes[scenario.id] = tape_provenance(tapes[scenario.id], cassette)
            results[scenario.id] = (scenario, reps)
    finally:
        if eval_registry is not None:
            await eval_registry.close()
        await close_provider(provider)

    base_out = Path(args.out)
    sha = git_short_sha(EVALS_DIR, data_paths=run_data_paths(EVALS_DIR, cassette_dir))
    run_dir = make_run_dir(base_out, sha=sha)
    summary = build_summary(
        run_id=run_dir.name,
        git_sha=sha,
        model=spec.model,
        repeat=args.repeat,
        max_tokens=max_tokens,
        requested_max_tokens=args.max_tokens,
        cassette_mode=args.cassette,
        registered_tools=sorted(registered),
        results=results,
        cassette_dir=str(cassette_dir),
        cassette_model_key=model_key,
        cassette_tapes=tapes,
        skipped_scenarios=skipped,
        eval_run_nonce=eval_run_nonce,
    )
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (run_dir / "report.md").write_text(render_harness_report(summary), encoding="utf-8")
    write_transcripts(run_dir / "transcripts.jsonl", results)
    print(f"Run written to {run_dir}")
    print(
        f"Overall score: {summary['totals']['score_mean']} | "
        f"pass rate: {summary['totals']['pass_rate']}"
    )
    return 0


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(
        description="Score the harness with one model over repeated scenario runs."
    )
    parser.add_argument("--model", required=True, help="candidate name (or baseline label)")
    parser.add_argument("--repeat", type=int, default=3, help="repetitions per scenario")
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=65_536,
        help="maximum output tokens per provider call (default: 65536)",
    )
    parser.add_argument("--models", default=str(EVALS_DIR / "models.yaml"))
    parser.add_argument("--scenarios", default=str(EVALS_DIR / "scenarios"))
    parser.add_argument("--cassettes", default=str(EVALS_DIR / "cassettes"))
    parser.add_argument(
        "--cassette",
        choices=CASSETTE_MODES,
        default="replay",
        help="replay = hybrid (replay hits, record misses); strict fails misses",
    )
    parser.add_argument(
        "--no-shared-cassettes",
        action="store_true",
        help="Ignore the flat-tree shared baseline; use only this model's tapes.",
    )
    parser.add_argument("--out", default=str(EVALS_DIR / "runs" / "harness"))
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the run plan and exit without calling any provider.",
    )
    args = parser.parse_args()
    if args.repeat < 1:
        parser.error("--repeat must be >= 1")
    if args.max_tokens < 1:
        parser.error("--max-tokens must be >= 1")
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
