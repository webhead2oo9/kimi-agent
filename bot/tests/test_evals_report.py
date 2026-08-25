import json

from evals.harness import ScenarioRun, TurnRecord
from evals.identity import EvalIdentity
from evals.judge import JudgeResult, Rubric, RubricDimension
from evals.mechanical import MechanicalResult
from evals.report import ScenarioReport, render_report, write_raw_jsonl


DIMENSIONS = ("helpfulness", "persona", "accuracy", "tool_use", "safety", "formatting")


def _rubric():
    return Rubric(
        dimensions={
            d: RubricDimension(name=d, weight=1.0, anchors=f"5=strong {d}; 1=weak {d}")
            for d in DIMENSIONS
        }
    )


def _scenario_report():
    run_c = ScenarioRun(
        "s",
        "cand",
        [TurnRecord("q", "cand reply", [], 10, 5)],
        identity=EvalIdentity("qualification-run", "candidate:cand", "s", 0),
    )
    run_b = ScenarioRun(
        "s",
        "base",
        [TurnRecord("q", "base reply", [], 8, 4)],
        identity=EvalIdentity("qualification-run", "baseline:base", "s", 0),
    )
    mech = MechanicalResult([], [], 0, 0, 10, 5, False)
    judge = JudgeResult(
        candidate_scores=dict.fromkeys(DIMENSIONS, 4),
        baseline_scores=dict.fromkeys(DIMENSIONS, 3),
        candidate_verdict="good",
        baseline_verdict="ok",
        winner_label="candidate",
    )
    return ScenarioReport(
        scenario_id="s",
        category="tooling",
        candidate_run=run_c,
        baseline_run=run_b,
        candidate_mechanical=mech,
        baseline_mechanical=mech,
        judge=judge,
    )


def test_render_report_has_summary_table_and_costs():
    md = render_report("cand-model", "base-model", [_scenario_report()], _rubric())
    assert "cand-model" in md and "base-model" in md
    assert "helpfulness" in md
    assert "Tokens" in md or "tokens" in md
    # Structural assertions so a dropped section is caught, not just "function ran".
    assert "1/0/0" in md  # W/L/T: the single candidate win
    assert "4.0" in md  # candidate per-dimension mean
    assert "3.0" in md  # baseline per-dimension mean
    assert "s (tooling)" in md  # per-scenario detail heading


def test_write_raw_jsonl_one_line_per_scenario_model(tmp_path):
    path = tmp_path / "raw.jsonl"
    write_raw_jsonl(path, [_scenario_report()])
    lines = path.read_text().strip().splitlines()
    # One line for candidate + one for baseline.
    assert len(lines) == 2
    parsed = [json.loads(line) for line in lines]
    rows = {row["role"]: row for row in parsed}
    assert {row["model"] for row in rows.values()} == {"cand", "base"}
    candidate = rows["candidate"]["eval_identity"]
    baseline = rows["baseline"]["eval_identity"]
    assert candidate["run_nonce"] == baseline["run_nonce"] == "qualification-run"
    assert candidate["arm"] == "candidate:cand"
    assert baseline["arm"] == "baseline:base"
    assert candidate["user_id"] != baseline["user_id"]
    assert candidate["context_key"] != baseline["context_key"]
