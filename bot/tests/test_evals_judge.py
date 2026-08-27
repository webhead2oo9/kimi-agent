import asyncio
import json

import pytest

from evals.harness import ScenarioRun, TurnRecord
from evals.judge import JudgeError, build_judge_packet, judge_pair, load_rubric, parse_judge_scores
from evals.scenario import Expect, Scenario
from providers.base import LLMProvider
from providers.types import ProviderRequest, ProviderResponse
from trust.tiers import TrustTier


DIMENSIONS = ("helpfulness", "persona", "accuracy", "tool_use", "safety", "formatting")


class _JudgeProvider(LLMProvider):
    model = "judge-model"

    def __init__(self, payload):
        self._payload = payload

    async def run_turn(self, request: ProviderRequest) -> ProviderResponse:
        return ProviderResponse(content=self._payload)


def _run(label):
    return ScenarioRun(
        scenario_id="s",
        model_label=label,
        turns=[TurnRecord("q", f"reply from {label}", [], tokens=1, latency_ms=1)],
    )


def _scenario():
    return Scenario(
        id="s",
        category="persona",
        trust_tier=TrustTier.MEMBER,
        turns=["q"],
        expect=Expect(notes="be helpful"),
    )


def _rubric(tmp_path):
    path = tmp_path / "rubric.yaml"
    path.write_text(
        "dimensions:\n"
        "  helpfulness: { weight: 1.0, anchors: '5=solves it; 1=ignores it' }\n"
        "  persona: { weight: 1.0, anchors: '5=in voice; 1=generic' }\n"
        "  accuracy: { weight: 1.0, anchors: '5=correct; 1=wrong' }\n"
        "  tool_use: { weight: 1.0, anchors: '5=right tools; 1=wrong tools' }\n"
        "  safety: { weight: 1.0, anchors: '5=safe; 1=unsafe' }\n"
        "  formatting: { weight: 1.0, anchors: '5=clean; 1=raw dump' }\n"
    )
    return load_rubric(path)


def test_load_rubric_reads_dimensions_anchors_and_weights(tmp_path):
    rubric = _rubric(tmp_path)
    assert tuple(rubric.dimension_names) == DIMENSIONS
    assert rubric.dimensions["helpfulness"].weight == 1.0
    assert "solves it" in rubric.dimensions["helpfulness"].anchors


def test_parse_judge_scores_reads_all_dimensions():
    payload = json.dumps(
        {
            "A": dict.fromkeys(DIMENSIONS, 4) | {"verdict": "good"},
            "B": dict.fromkeys(DIMENSIONS, 3) | {"verdict": "weaker"},
            "winner": "A",
        }
    )
    parsed = parse_judge_scores(payload)
    assert parsed["A"]["helpfulness"] == 4
    assert parsed["winner"] == "A"


def test_build_judge_packet_includes_rubric_anchors_notes_and_replies(tmp_path):
    packet = build_judge_packet(
        _scenario(), _run("cand"), _run("base"), _rubric(tmp_path), swap=False
    )
    assert "be helpful" in packet
    assert "reply from cand" in packet
    assert "5=solves it" in packet


def test_build_judge_packet_exposes_incomplete_turns(tmp_path):
    candidate = _run("cand")
    candidate.turns[0].termination_reason = "max_iterations"

    packet = build_judge_packet(_scenario(), candidate, _run("base"), _rubric(tmp_path), swap=False)

    assert "TERMINATION: max_iterations" in packet


def test_judge_pair_maps_blind_labels_back_to_models(tmp_path):
    payload = json.dumps(
        {
            "A": dict.fromkeys(DIMENSIONS, 5) | {"verdict": "a"},
            "B": dict.fromkeys(DIMENSIONS, 2) | {"verdict": "b"},
            "winner": "A",
        }
    )
    result = asyncio.run(
        judge_pair(
            _JudgeProvider(payload),
            _scenario(),
            candidate_run=_run("cand"),
            baseline_run=_run("base"),
            rubric=_rubric(tmp_path),
        )
    )
    # id="s" has an odd ord-sum -> swap=True -> Model A = baseline, Model B = candidate.
    # So the candidate's scores must come from the "B" block (=2), baseline from "A" (=5),
    # and winner "A" must map back to "baseline". A direction error silently
    # misattributes results, so assert each reverse mapping concretely.
    assert set(result.candidate_scores) == set(DIMENSIONS)
    assert result.candidate_scores["helpfulness"] == 2
    assert result.baseline_scores["helpfulness"] == 5
    assert result.winner_label == "baseline"


def test_judge_pair_maps_labels_without_swap(tmp_path):
    # id="b" has an even ord-sum -> swap=False -> Model A = candidate, Model B = baseline.
    scenario = Scenario(
        id="b",
        category="persona",
        trust_tier=TrustTier.MEMBER,
        turns=["q"],
        expect=Expect(notes="be helpful"),
    )
    payload = json.dumps(
        {
            "A": dict.fromkeys(DIMENSIONS, 5) | {"verdict": "a"},
            "B": dict.fromkeys(DIMENSIONS, 2) | {"verdict": "b"},
            "winner": "A",
        }
    )
    result = asyncio.run(
        judge_pair(
            _JudgeProvider(payload),
            scenario,
            candidate_run=_run("cand"),
            baseline_run=_run("base"),
            rubric=_rubric(tmp_path),
        )
    )
    assert result.candidate_scores["helpfulness"] == 5  # candidate is Model A
    assert result.baseline_scores["helpfulness"] == 2  # baseline is Model B
    assert result.candidate_verdict == "a"
    assert result.winner_label == "candidate"


def test_judge_pair_rejects_self_preference(tmp_path):
    judge = _JudgeProvider("{}")
    judge.model = "cand"  # instance attr shadows the class attr: judge == a candidate
    with pytest.raises(JudgeError, match="self-preference"):
        asyncio.run(
            judge_pair(
                judge,
                _scenario(),
                candidate_run=_run("cand"),
                baseline_run=_run("base"),
                rubric=_rubric(tmp_path),
            )
        )


def test_judge_pair_coerces_non_integer_scores_to_zero(tmp_path):
    # A judge can occasionally return a non-numeric score; it must coerce to 0, not crash.
    payload = json.dumps(
        {
            "A": dict.fromkeys(DIMENSIONS, "high") | {"verdict": "x"},
            "B": dict.fromkeys(DIMENSIONS, 3) | {"verdict": "y"},
            "winner": "tie",
        }
    )
    result = asyncio.run(
        judge_pair(
            _JudgeProvider(payload),
            _scenario(),
            candidate_run=_run("cand"),
            baseline_run=_run("base"),
            rubric=_rubric(tmp_path),
        )
    )
    # _scenario() id="s" -> swap=True -> baseline reads block "A" ("high" -> 0).
    assert result.baseline_scores["helpfulness"] == 0
    assert result.candidate_scores["helpfulness"] == 3


def test_parse_judge_scores_raises_judge_error_on_malformed_json():
    with pytest.raises(JudgeError):
        parse_judge_scores('answer: {"A": {bad json}}')
