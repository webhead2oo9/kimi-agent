from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from evals.harness import ScenarioRun
from evals.scenario import Scenario
from providers.base import LLMProvider
from providers.types import ContentPart, ConversationMessage, ProviderRequest
from utils.json_payload import extract_json_object


class JudgeError(RuntimeError):
    """Raised when judging cannot proceed (e.g. self-preference)."""


@dataclass(frozen=True)
class RubricDimension:
    name: str
    weight: float
    anchors: str


@dataclass(frozen=True)
class Rubric:
    dimensions: dict[str, RubricDimension]

    @property
    def dimension_names(self) -> list[str]:
        return list(self.dimensions)

    def prompt_block(self) -> str:
        lines = ["Rubric dimensions (score each 1-5 using these anchors):"]
        for dim in self.dimensions.values():
            lines.append(f"- {dim.name} (weight {dim.weight}): {dim.anchors}")
        return "\n".join(lines)


@dataclass
class JudgeResult:
    candidate_scores: dict[str, int]
    baseline_scores: dict[str, int]
    candidate_verdict: str
    baseline_verdict: str
    winner_label: str  # "candidate" | "baseline" | "tie"


def load_rubric(path: str | Path) -> Rubric:
    raw = yaml.safe_load(Path(path).read_text()) or {}
    dimensions: dict[str, RubricDimension] = {}
    for name, data in (raw.get("dimensions") or {}).items():
        if not isinstance(data, dict):
            raise JudgeError(f"Rubric dimension {name!r} must be a mapping")
        dimensions[str(name)] = RubricDimension(
            name=str(name),
            weight=float(data.get("weight", 1.0)),
            anchors=str(data.get("anchors", "")),
        )
    if not dimensions:
        raise JudgeError(f"Rubric {path} must declare at least one dimension")
    return Rubric(dimensions=dimensions)


def _transcript(run: ScenarioRun) -> str:
    lines = []
    for turn in run.turns:
        lines.append(f"USER: {turn.user_message}")
        if turn.tool_calls:
            tools = ", ".join(f"{r.tool}({r.args})" for r in turn.tool_calls)
            lines.append(f"TOOLS: {tools}")
        lines.append(f"REPLY: {turn.final_text}")
    return "\n".join(lines)


def build_judge_packet(
    scenario: Scenario,
    candidate_run: ScenarioRun,
    baseline_run: ScenarioRun,
    rubric: Rubric,
    *,
    swap: bool,
) -> str:
    a_run, b_run = (baseline_run, candidate_run) if swap else (candidate_run, baseline_run)
    return (
        f"Scenario category: {scenario.category}\n"
        f"What good looks like: {scenario.expect.notes or '(none given)'}\n\n"
        f"{rubric.prompt_block()}\n\n"
        f"--- MODEL A ---\n{_transcript(a_run)}\n\n"
        f"--- MODEL B ---\n{_transcript(b_run)}\n\n"
        "Return STRICT JSON only:\n"
        '{"A": {<dim>: int, ..., "verdict": "..."}, '
        '"B": {<dim>: int, ..., "verdict": "..."}, "winner": "A|B|tie"}'
    )


def parse_judge_scores(text: str) -> dict[str, Any]:
    payload = extract_json_object(text)
    if payload is None:
        raise JudgeError("Judge did not return a parseable JSON object")
    return payload


def _coerce_score(value: Any) -> int:
    # A judge can occasionally return a non-numeric score ("high", "N/A"); coerce to 0
    # rather than crash the whole eval pair (consistent with the missing-dim -> 0 rule).
    try:
        return int(value)
    except TypeError, ValueError:
        return 0


def _scores(block: dict[str, Any], rubric: Rubric) -> dict[str, int]:
    return {d: _coerce_score(block.get(d, 0)) for d in rubric.dimension_names}


async def judge_pair(
    judge: LLMProvider,
    scenario: Scenario,
    *,
    candidate_run: ScenarioRun,
    baseline_run: ScenarioRun,
    rubric: Rubric,
) -> JudgeResult:
    if judge.model in {candidate_run.model_label, baseline_run.model_label}:
        raise JudgeError(
            f"Judge model {judge.model!r} matches a graded model (self-preference); pin a "
            "different judge."
        )
    swap = sum(ord(c) for c in scenario.id) % 2 == 1
    packet = build_judge_packet(scenario, candidate_run, baseline_run, rubric, swap=swap)
    request = ProviderRequest(
        conversation_id=0,
        system_prompt="You are a rigorous evaluator. Output strict JSON only.",
        messages=[ConversationMessage(role="user", content=[ContentPart.from_text(packet)])],
        current_user_parts=[],
        tools=[],
        # Generous ceiling: a reasoning judge (K3) spends tokens thinking before
        # the scores JSON; a tight cap could truncate mid-object.
        max_tokens=32768,
        temperature=0.0,
    )
    response = await judge.run_turn(request)
    parsed = parse_judge_scores(response.content or "")
    # Map blind A/B back to candidate/baseline.
    cand_key, base_key = ("B", "A") if swap else ("A", "B")
    winner_raw = str(parsed.get("winner", "tie")).upper()
    if winner_raw == cand_key:
        winner = "candidate"
    elif winner_raw == base_key:
        winner = "baseline"
    else:
        winner = "tie"
    return JudgeResult(
        candidate_scores=_scores(parsed.get(cand_key, {}), rubric),
        baseline_scores=_scores(parsed.get(base_key, {}), rubric),
        candidate_verdict=str(parsed.get(cand_key, {}).get("verdict", "")),
        baseline_verdict=str(parsed.get(base_key, {}).get("verdict", "")),
        winner_label=winner,
    )
