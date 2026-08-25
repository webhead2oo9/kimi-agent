from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evals.harness import ScenarioRun
from evals.judge import JudgeResult, Rubric
from evals.mechanical import MechanicalResult


@dataclass
class ScenarioReport:
    scenario_id: str
    category: str
    candidate_run: ScenarioRun
    baseline_run: ScenarioRun
    candidate_mechanical: MechanicalResult
    baseline_mechanical: MechanicalResult
    judge: JudgeResult


def _mean(values: list[int]) -> float:
    return round(sum(values) / len(values), 2) if values else 0.0


def _last_reply(run: ScenarioRun) -> str:
    return run.turns[-1].final_text[:500] if run.turns else "(no turns)"


def render_report(
    candidate_model: str,
    baseline_model: str,
    reports: list[ScenarioReport],
    rubric: Rubric,
) -> str:
    wins = sum(1 for r in reports if r.judge.winner_label == "candidate")
    losses = sum(1 for r in reports if r.judge.winner_label == "baseline")
    ties = sum(1 for r in reports if r.judge.winner_label == "tie")
    cand_tokens = sum(r.candidate_run.total_tokens for r in reports)
    base_tokens = sum(r.baseline_run.total_tokens for r in reports)

    lines = [
        f"# Eval: {candidate_model} (candidate) vs {baseline_model} (baseline)",
        "",
        f"**Scenarios:** {len(reports)} | **Candidate W/L/T:** {wins}/{losses}/{ties}",
        "",
        "## Per-dimension mean (candidate vs baseline)",
        "",
        "| Dimension | Candidate | Baseline |",
        "| --- | --- | --- |",
    ]
    for dim in rubric.dimension_names:
        cand = _mean([r.judge.candidate_scores.get(dim, 0) for r in reports])
        base = _mean([r.judge.baseline_scores.get(dim, 0) for r in reports])
        lines.append(f"| {dim} | {cand} | {base} |")
    lines += [
        "",
        "## Cost (this run)",
        "",
        f"- Candidate tokens: {cand_tokens} | Baseline tokens: {base_tokens}",
        f"- Candidate tool calls: {sum(r.candidate_mechanical.tool_call_count for r in reports)}",
        f"- Tool errors (candidate): {sum(r.candidate_mechanical.tool_errors for r in reports)}",
    ]
    lines += [
        "",
        "## Per-scenario detail",
        "",
    ]
    for r in reports:
        flag = (
            " ⚠️"
            if (r.candidate_mechanical.missing_tools or r.candidate_mechanical.tool_errors)
            else ""
        )
        lines += [
            f"### {r.scenario_id} ({r.category}): winner {r.judge.winner_label}{flag}",
            "",
            f"- Missing tools: {r.candidate_mechanical.missing_tools or 'none'}",
            f"- Unexpected tools: {r.candidate_mechanical.unexpected_tools or 'none'}",
            f"- Candidate verdict: {r.judge.candidate_verdict}",
            f"- Candidate reply: {_last_reply(r.candidate_run)}",
            f"- Baseline reply: {_last_reply(r.baseline_run)}",
            "",
        ]
    return "\n".join(lines)


def _row(report: ScenarioReport, which: str) -> dict[str, Any]:
    run = report.candidate_run if which == "candidate" else report.baseline_run
    mech = report.candidate_mechanical if which == "candidate" else report.baseline_mechanical
    scores = report.judge.candidate_scores if which == "candidate" else report.judge.baseline_scores
    return {
        "scenario": report.scenario_id,
        "role": which,
        "model": run.model_label,
        "final_text": run.turns[-1].final_text if run.turns else "",
        "tool_calls": [
            {"tool": rec.tool, "args": rec.args, "ok": rec.ok, "duration_ms": rec.duration_ms}
            for rec in run.all_tool_calls
        ],
        "tokens": run.total_tokens,
        "latency_ms": run.total_latency_ms,
        "missing_tools": mech.missing_tools,
        "scores": scores,
    }


def write_raw_jsonl(path: str | Path, reports: list[ScenarioReport]) -> None:
    out = Path(path)
    with out.open("w") as handle:
        for report in reports:
            handle.write(json.dumps(_row(report, "candidate"), default=str) + "\n")
            handle.write(json.dumps(_row(report, "baseline"), default=str) + "\n")
