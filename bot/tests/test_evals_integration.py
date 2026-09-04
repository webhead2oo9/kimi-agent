"""End-to-end wiring test: harness -> judge -> mechanical -> report.

Every other eval test is a unit test over hand-built objects, so a seam mismatch
(a field renamed in one module but not its consumer) could pass them all yet fail at
runtime. This drives the real data flow across modules with scripted providers (no
network) to catch exactly that class of bug.
"""

import asyncio
import json

from evals.capture import InstrumentedProvider, InstrumentedRegistry
from evals.harness import run_scenario_for_model
from evals.judge import Rubric, RubricDimension, judge_pair
from evals.mechanical import compute_mechanical
from evals.report import ScenarioReport, render_report, write_raw_jsonl
from evals.scenario import Expect, Scenario
from providers.base import LLMProvider
from providers.types import ProviderRequest, ProviderResponse, ToolCall
from trust.tiers import TrustTier


class _Scripted(LLMProvider):
    def __init__(self, model, responses):
        self._model = model
        self._responses = list(responses)

    @property
    def model(self):
        return self._model

    async def run_turn(self, request: ProviderRequest) -> ProviderResponse:
        return self._responses.pop(0)


def _rubric():
    dims = ("helpfulness", "persona", "accuracy", "tool_use", "safety", "formatting")
    return Rubric(
        dimensions={d: RubricDimension(name=d, weight=1.0, anchors=f"5=good {d}") for d in dims}
    )


def test_full_pipeline_harness_judge_mechanical_report(tmp_path):
    rubric = _rubric()
    scenario = Scenario(
        id="probe-it",
        category="tooling",
        trust_tier=TrustTier.MEMBER,
        turns=["compute it"],
        expect=Expect(should_use_tools=["probe"]),
    )

    async def probe(args, ctx):
        return json.dumps({"value": 42})

    registry = InstrumentedRegistry()
    registry.register(
        name="probe",
        description="d",
        parameters={"type": "object", "properties": {}},
        handler=probe,
    )
    candidate = InstrumentedProvider(
        _Scripted(
            "cand",
            [
                ProviderResponse(
                    tool_calls=[ToolCall(id="1", name="probe", arguments={})],
                    finish_reason="tool_calls",
                ),
                ProviderResponse(content="the answer is 42"),
            ],
        )
    )
    baseline = InstrumentedProvider(_Scripted("base", [ProviderResponse(content="not sure")]))
    cand_run = asyncio.run(
        run_scenario_for_model(
            scenario,
            provider=candidate,
            registry=registry,
            memory_client=None,
            preference_store=None,
        )
    )
    base_run = asyncio.run(
        run_scenario_for_model(
            scenario,
            provider=baseline,
            registry=registry,
            memory_client=None,
            preference_store=None,
        )
    )

    # Harness captured the candidate's tool call and final reply.
    assert cand_run.turns[-1].final_text == "the answer is 42"
    assert "probe" in {r.tool for r in cand_run.all_tool_calls}

    judge = _Scripted(
        "judge-x",
        [
            ProviderResponse(
                content=json.dumps(
                    {
                        "A": dict.fromkeys(rubric.dimension_names, 4) | {"verdict": "ok"},
                        "B": dict.fromkeys(rubric.dimension_names, 2) | {"verdict": "meh"},
                        "winner": "A",
                    }
                )
            )
        ],
    )
    judge_result = asyncio.run(
        judge_pair(judge, scenario, candidate_run=cand_run, baseline_run=base_run, rubric=rubric)
    )

    report = ScenarioReport(
        scenario_id=scenario.id,
        category=scenario.category,
        candidate_run=cand_run,
        baseline_run=base_run,
        candidate_mechanical=compute_mechanical(scenario, cand_run),
        baseline_mechanical=compute_mechanical(scenario, base_run),
        judge=judge_result,
    )
    # Mechanical layer sees the expected tool was used (no missing-tool flag).
    assert report.candidate_mechanical.missing_tools == []

    md = render_report("cand", "base", [report], rubric)
    assert "probe-it (tooling)" in md
    assert "helpfulness" in md

    write_raw_jsonl(tmp_path / "raw.jsonl", [report])
    lines = (tmp_path / "raw.jsonl").read_text().strip().splitlines()
    assert len(lines) == 2
    assert {json.loads(line)["model"] for line in lines} == {"cand", "base"}
