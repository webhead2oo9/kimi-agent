import argparse
import asyncio
import json

from evals import run as evals_run
from evals.harness import ScenarioRun, TurnRecord
from evals.judge import JudgeResult, Rubric, RubricDimension
from evals.models import ModelSpec, ModelsConfig
from evals.run import plan_matrix
from evals.scenario import Expect, Scenario
from providers.base import LLMProvider
from providers.types import ProviderRequest, ProviderResponse
from tests.helpers import make_settings
from tools.registry import ToolRegistry
from trust.tiers import TrustTier


def test_plan_matrix_lists_scenarios_times_models():
    models = ModelsConfig(
        baseline=ModelSpec("base", "openai_compat", "kimi"),
        candidates={"new": ModelSpec("new", "anthropic", "claude-x")},
        judge=ModelSpec("judge", "anthropic", "opus"),
    )
    lines = plan_matrix("new", models, scenario_ids=["a", "b"])
    text = "\n".join(lines)
    assert "new" in text and "base" in text
    assert "a" in text and "b" in text
    # 2 scenarios x 2 models = 4 runs.
    assert any("4" in line for line in lines)


class _Stub(LLMProvider):
    def __init__(self, model):
        self._model = model

    @property
    def model(self):
        return self._model

    async def run_turn(self, request: ProviderRequest) -> ProviderResponse:
        raise AssertionError("the arms are stubbed; no provider call should happen")


class _FakeEvalRegistry:
    def __init__(self):
        self.registry = ToolRegistry()
        self.preference_store = None
        self.memory_manager = self
        self.provider_manager = self

    def active_client(self):
        return None

    def build_compactor(self):
        return None

    async def close(self):
        return None


def _rubric():
    dims = ("helpfulness", "accuracy")
    return Rubric(
        dimensions={d: RubricDimension(name=d, weight=1.0, anchors=f"5=good {d}") for d in dims}
    )


async def _noop_tool(args, ctx):
    return "{}"


def test_qualification_run_skips_tools_hidden_at_scenario_tier(monkeypatch, tmp_path, capsys):
    regular = Scenario(
        id="regular-code",
        category="tooling",
        trust_tier=TrustTier.REGULAR,
        turns=["q"],
        expect=Expect(),
        requires_tools=["run_code"],
    )
    staff = Scenario(
        id="staff-code",
        category="tooling",
        trust_tier=TrustTier.STAFF,
        turns=["q"],
        expect=Expect(),
        requires_tools=["run_code"],
    )

    async def _fake_registry(settings, *, gateway):
        result = _FakeEvalRegistry()
        result.registry.register(
            "run_code",
            "",
            {},
            _noop_tool,
            min_tier=TrustTier.STAFF,
        )
        return result

    identities = []

    async def _fake_scenario_run(scenario_arg, **kwargs):
        identities.append(kwargs["identity"])
        return ScenarioRun(
            scenario_id=scenario_arg.id,
            model_label="m",
            identity=kwargs["identity"],
            turns=[TurnRecord("q", "an answer", [], 10, 5)],
        )

    async def _fake_judge(judge, scenario_arg, *, candidate_run, baseline_run, rubric):
        return JudgeResult(
            candidate_scores=dict.fromkeys(rubric.dimension_names, 4),
            baseline_scores=dict.fromkeys(rubric.dimension_names, 3),
            candidate_verdict="ok",
            baseline_verdict="ok",
            winner_label="candidate",
        )

    async def _noop_close(provider):
        return None

    monkeypatch.setattr(
        evals_run,
        "load_models",
        lambda path: ModelsConfig(
            baseline=ModelSpec("base", "openai_compat", "kimi", base_url="https://x"),
            candidates={"cand": ModelSpec("cand", "openai_compat", "c", base_url="https://x")},
            judge=ModelSpec("judge", "openai_compat", "j", base_url="https://x"),
        ),
    )
    monkeypatch.setattr(evals_run, "load_scenarios", lambda path: [regular, staff])
    monkeypatch.setattr(evals_run, "load_rubric", lambda path: _rubric())
    monkeypatch.setattr(evals_run, "build_eval_provider", lambda spec: _Stub(spec.model))
    monkeypatch.setattr(evals_run, "build_eval_registry", _fake_registry)
    monkeypatch.setattr(evals_run, "run_scenario_for_model", _fake_scenario_run)
    monkeypatch.setattr(evals_run, "judge_pair", _fake_judge)
    monkeypatch.setattr(evals_run, "close_provider", _noop_close)

    args = argparse.Namespace(
        candidate="cand",
        models="m.yaml",
        scenarios="s",
        rubric="r.yaml",
        out=str(tmp_path),
        dry_run=False,
    )
    settings = make_settings()
    assert asyncio.run(evals_run._run(args, settings)) == 0

    report = (tmp_path / "report.md").read_text()
    assert "Candidate tokens: 10 | Baseline tokens: 10" in report
    assert "**Coverage:** 2 selected | 1 executed | 1 skipped" in report
    assert "`regular-code`: needs run_code" in report
    rows = [json.loads(line) for line in (tmp_path / "raw.jsonl").read_text().splitlines()]
    assert len(rows) == 2
    assert rows[0]["coverage"] == {
        "selected_scenarios": ["regular-code", "staff-code"],
        "executed_scenarios": ["staff-code"],
        "skipped_scenarios": {"regular-code": ["run_code"]},
    }
    assert [identity.scenario_id for identity in identities] == ["staff-code", "staff-code"]
    assert len({identity.user_id for identity in identities}) == 2
    assert identities[0].arm == "candidate:cand"
    assert identities[1].arm == "baseline:base"

    capsys.readouterr()
    identities.clear()
    args.dry_run = True
    monkeypatch.setattr(
        evals_run,
        "build_eval_provider",
        lambda spec: (_ for _ in ()).throw(AssertionError("dry-run built a model provider")),
    )
    assert asyncio.run(evals_run._run(args, settings)) == 0
    plan = capsys.readouterr().out
    assert "Scenarios: staff-code" in plan
    assert "regular-code" not in plan
    assert "Total live run_conversation calls: 2" in plan
    assert identities == []

    monkeypatch.setattr(evals_run, "load_scenarios", lambda path: [regular])
    assert asyncio.run(evals_run._run(args, settings)) == 2
