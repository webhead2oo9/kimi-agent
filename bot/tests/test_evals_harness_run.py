from pathlib import Path

import pytest

from evals import harness_run
from evals.capture import ToolCallRecord
from evals.harness import ScenarioRun, TurnRecord
from evals.harness_run import (
    RepResult,
    build_summary,
    make_run_dir,
    missing_expected_tools,
    render_harness_report,
    resolve_model_spec,
)
from evals.mechanical import MechanicalResult
from evals.models import ModelsConfig, ModelSpec, load_models
from evals.scenario import Expect, Scenario
from trust.tiers import TrustTier


def _models():
    return ModelsConfig(
        baseline=ModelSpec("prod", "openai_compat", "kimi", base_url="https://x"),
        candidates={"sol": ModelSpec("gpt-5.6-sol", "codex", "gpt-5.6-sol")},
        judge=ModelSpec("judge", "openai_compat", "glm", base_url="https://x"),
    )


def _mech(score=100.0, passed=True, **overrides):
    base = {
        "missing_tools": [],
        "unexpected_tools": [],
        "tool_call_count": 2,
        "tool_errors": 0,
        "tokens": 100,
        "latency_ms": 50,
        "raw_json_reply": False,
        "score": score,
    }
    if not passed:
        base["missing_tools"] = ["wolfram_alpha"]
    base.update(overrides)
    return MechanicalResult(**base)


def _rep(mech, *, cost=None, tool_calls=None):
    turns = []
    if tool_calls:
        turns.append(
            TurnRecord(
                user_message="q",
                final_text="a",
                tool_calls=list(tool_calls),
                tokens=10,
                latency_ms=5,
                provider_calls=2,
            )
        )
    return RepResult(
        mechanical=mech,
        sources={"replay": 2},
        run=ScenarioRun(scenario_id="s", model_label="m", turns=turns),
        cost=cost,
    )


def _call(tool, source):
    return ToolCallRecord(
        tool=tool,
        args={},
        result="r",
        ok=True,
        duration_ms=1,
        source=source,
        provider_calls_before=1,
    )


def _scenario(scenario_id="s", expect=None):
    return Scenario(
        id=scenario_id,
        category="tooling",
        trust_tier=TrustTier.MEMBER,
        turns=["q"],
        expect=expect or Expect(),
    )


def test_resolve_model_spec_finds_candidates_and_baseline_label():
    models = _models()
    assert resolve_model_spec(models, "sol") is models.candidates["sol"]
    assert resolve_model_spec(models, "prod") is models.baseline
    assert resolve_model_spec(models, "nope") is None


def test_missing_expected_tools_flags_unregistered_only():
    scenarios = [
        _scenario("a", Expect(should_use_tools=["wolfram_alpha"])),
        _scenario("b", Expect(should_use_tools=["get_steam_game_info"])),
    ]
    problems = missing_expected_tools(scenarios, registered={"wolfram_alpha"})
    assert problems == {"b": ["get_steam_game_info"]}


def test_build_summary_aggregates_scores_and_pass_rate():
    results = {
        "a": (_scenario("a"), [_rep(_mech(100.0)), _rep(_mech(80.0, passed=False))]),
        "b": (_scenario("b"), [_rep(_mech(90.0)), _rep(_mech(90.0))]),
    }
    summary = build_summary(
        run_id="r1",
        git_sha="abc1234",
        model="gpt-5.6-sol",
        repeat=2,
        cassette_mode="replay",
        registered_tools=["wolfram_alpha"],
        results=results,
    )
    assert summary["kind"] == "harness-eval"
    agg_a = summary["scenarios"]["a"]["aggregate"]
    assert agg_a["score_mean"] == 90.0
    assert agg_a["score_min"] == 80.0
    assert agg_a["pass_rate"] == 0.5
    assert summary["totals"]["score_mean"] == 90.0  # mean of per-scenario means
    assert summary["totals"]["pass_rate"] == 0.75
    assert summary["max_tokens"] == 65_536
    assert summary["requested_max_tokens"] == 65_536
    assert summary["scenarios"]["a"]["reps"][0]["sources"] == {"replay": 2}
    assert summary["scenarios"]["a"]["reps"][0]["passed"] is True


def _fake_git(responses):
    """Stub subprocess.run keyed by the git subcommand, recording every call."""
    calls: list[list[str]] = []

    class _Proc:
        def __init__(self, stdout):
            self.stdout = stdout
            self.returncode = 0

    def run(cmd, **kwargs):
        calls.append(cmd)
        return _Proc(responses[cmd[1]])

    return run, calls


def test_git_short_sha_marks_dirty_tree(monkeypatch):
    run, calls = _fake_git({"rev-parse": "0c2894a\n", "status": " M evals/compare.py\n"})
    monkeypatch.setattr(harness_run.subprocess, "run", run)

    assert harness_run.git_short_sha(Path(".")) == "0c2894a-dirty"
    # Untracked files must not count: a run writes its own new cassette tapes.
    assert "--untracked-files=no" in calls[1]


def test_git_short_sha_excludes_the_cassettes_the_run_writes(monkeypatch):
    # A replay run records misses and promotes baseline entries. A caller may
    # place them in a tracked custom path; without the exclusion the run stamps
    # itself -dirty from its own data write and two runs of identical code report
    # different trees.
    run, calls = _fake_git({"rev-parse": "0c2894a\n", "status": "\n"})
    monkeypatch.setattr(harness_run.subprocess, "run", run)

    assert harness_run.git_short_sha(Path("."), data_paths=("evals/cassettes",)) == "0c2894a"
    status_args = calls[1]
    assert ":(top,exclude)evals/cassettes" in status_args
    # A bare exclusion matches nothing in git; the positive pathspec is what
    # keeps the rest of the tree in the check.
    assert ":(top)" in status_args


def _seeded_repo(tmp_path):
    """A throwaway git repo shaped like the checkout, or a skip when git is absent."""
    import subprocess as sp

    def git(*args):
        return sp.run(
            [
                "git",
                "-c",
                "user.name=t",
                "-c",
                "user.email=t@t",
                "-c",
                "commit.gpgsign=false",
                *args,
            ],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )

    if git("init", "-q", ".").returncode != 0:
        pytest.skip("git unavailable")
    evals_dir = tmp_path / "evals"
    (evals_dir / "cassettes" / "model-a").mkdir(parents=True)
    (evals_dir / "tapes-alt" / "model-a").mkdir(parents=True)
    (evals_dir / "harness_run.py").write_text("x = 1\n")
    for tapes in ("cassettes", "tapes-alt"):
        (evals_dir / tapes / "model-a" / "s.json").write_text('{"entries": []}\n')
    git("add", "-A")
    assert git("commit", "-qm", "seed").returncode == 0
    return evals_dir


def test_git_dirty_marker_tracks_source_edits_not_cassette_writes(tmp_path):
    """End to end against real git: the stub can only prove we pass the flag."""
    evals_dir = _seeded_repo(tmp_path)
    cassettes = evals_dir / "cassettes"
    data_paths = harness_run.run_data_paths(evals_dir, cassettes)

    assert "-dirty" not in harness_run.git_short_sha(evals_dir, data_paths=data_paths)
    # The run's own write: a recorded miss / promoted baseline entry.
    (cassettes / "model-a" / "s.json").write_text('{"entries": [{"tool": "internet_search"}]}\n')
    assert "-dirty" not in harness_run.git_short_sha(evals_dir, data_paths=data_paths)
    # An edited source file is what the marker is for.
    (evals_dir / "harness_run.py").write_text("x = 2\n")
    assert harness_run.git_short_sha(evals_dir, data_paths=data_paths).endswith("-dirty")


def test_run_data_paths_follows_a_non_default_cassette_dir(tmp_path):
    # --cassettes <another tracked path> writes there instead, so a hardcoded
    # "evals/cassettes" exclusion covers none of the run's own writes and the run
    # stamps itself -dirty from its own data, the exact bug the exclusion fixed.
    evals_dir = _seeded_repo(tmp_path)
    alt = evals_dir / "tapes-alt"

    assert harness_run.run_data_paths(evals_dir, evals_dir / "cassettes") == ("evals/cassettes",)
    assert harness_run.run_data_paths(evals_dir, alt) == ("evals/tapes-alt",)

    (alt / "model-a" / "s.json").write_text('{"entries": [{"tool": "internet_search"}]}\n')
    alt_paths = harness_run.run_data_paths(evals_dir, alt)
    assert "-dirty" not in harness_run.git_short_sha(evals_dir, data_paths=alt_paths)
    # The stale hardcoded exclusion would have called this run's own write a
    # source edit.
    stale = harness_run.git_short_sha(evals_dir, data_paths=("evals/cassettes",))
    assert stale.endswith("-dirty")


def test_run_data_paths_excludes_nothing_for_tapes_outside_the_repo(tmp_path):
    # Tapes outside the checkout cannot dirty it, and a path git cannot place
    # must not silently widen the exclusion.
    evals_dir = _seeded_repo(tmp_path)
    outside = tmp_path.parent / "tapes-elsewhere"
    assert harness_run.run_data_paths(evals_dir, outside) == ()


def test_git_short_sha_clean_tree_has_no_suffix(monkeypatch):
    run, _ = _fake_git({"rev-parse": "0c2894a\n", "status": "\n"})
    monkeypatch.setattr(harness_run.subprocess, "run", run)

    sha = harness_run.git_short_sha(Path("."))

    assert sha == "0c2894a"  # trailing newline stripped
    assert not sha.endswith("-dirty")  # a clean tree takes the no-suffix branch


def test_git_short_sha_falls_back_on_subprocess_failure(monkeypatch):
    import subprocess

    def boom(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 10)

    monkeypatch.setattr(harness_run.subprocess, "run", boom)
    assert harness_run.git_short_sha(Path(".")) == "nogit"


def test_git_short_sha_marks_unknown_when_the_status_check_fails(monkeypatch):
    # rev-parse succeeding proves git works, so a status that then fails
    # (index.lock contention, the 10s timeout, a permissions error) is the
    # ambiguous case. Returning the bare sha stamps a genuinely edited tree with
    # a clean commit id, which is the "claimed a commit it never executed"
    # failure the marker exists to prevent.
    import subprocess

    class _Proc:
        def __init__(self, stdout, returncode=0):
            self.stdout = stdout
            self.returncode = returncode

    def run(cmd, **kwargs):
        if cmd[1] == "rev-parse":
            return _Proc("0c2894a\n")
        raise subprocess.TimeoutExpired(cmd, 10)

    monkeypatch.setattr(harness_run.subprocess, "run", run)
    assert harness_run.git_short_sha(Path(".")) == "0c2894a-unknown"

    def nonzero(cmd, **kwargs):
        if cmd[1] == "rev-parse":
            return _Proc("0c2894a\n")
        return _Proc("", returncode=128)

    monkeypatch.setattr(harness_run.subprocess, "run", nonzero)
    assert harness_run.git_short_sha(Path(".")) == "0c2894a-unknown"


def test_make_run_dir_uniquifies_collisions(tmp_path):
    from datetime import UTC, datetime

    now = datetime(2026, 7, 29, 12, 0, 0, tzinfo=UTC)
    first = make_run_dir(tmp_path, sha="abc", now=now)
    second = make_run_dir(tmp_path, sha="abc", now=now)
    assert first != second
    assert first.exists() and second.exists()


def test_render_harness_report_lists_scenarios_and_flags():
    results = {
        "a": (_scenario("a"), [_rep(_mech(75.0, passed=False, repeated_calls=2))]),
    }
    summary = build_summary(
        run_id="r1",
        git_sha="abc",
        model="m",
        repeat=1,
        cassette_mode="off",
        registered_tools=[],
        results=results,
    )
    report = render_harness_report(summary)
    assert "| a |" in report
    assert "missing:wolfram_alpha" in report
    assert "repeats:2" in report


def test_summary_and_report_include_unscored_completion_timing():
    rep = _rep(_mech(100.0), tool_calls=[_call("get_steam_game_info", "replay")])
    rep.run.wall_time_ms = 1_250
    rep.run.turns[0].latency_ms = 900
    summary = build_summary(
        run_id="r1",
        git_sha="abc",
        model="m",
        repeat=1,
        cassette_mode="replay",
        registered_tools=[],
        results={"a": (_scenario("a"), [rep])},
    )

    aggregate = summary["scenarios"]["a"]["aggregate"]
    assert aggregate["wall_time_mean_ms"] == 1_250
    assert aggregate["wall_time_min_ms"] == 1_250
    assert aggregate["wall_time_max_ms"] == 1_250
    assert aggregate["provider_latency_mean_ms"] == 900
    assert summary["totals"]["wall_time_ms"] == 1_250
    assert summary["totals"]["provider_latency_ms"] == 900

    report = render_harness_report(summary)
    assert "**Completion time:** 1.25s end-to-end | 0.90s in provider calls" in report
    assert "Time (wall / provider)" in report
    assert "| 1.25s / 0.90s |" in report


def test_build_summary_carries_llm_token_cost_keys():
    results = {
        "a": (_scenario("a"), [_rep(_mech(100.0), cost=0.10)]),
        "b": (_scenario("b"), [_rep(_mech(90.0), cost=0.20)]),
    }
    summary = build_summary(
        run_id="r1",
        git_sha="abc",
        model="m",
        repeat=1,
        cassette_mode="replay",
        registered_tools=[],
        results=results,
    )

    assert summary["totals"]["est_cost_usd"] == 0.3
    assert summary["scenarios"]["a"]["reps"][0]["est_cost_usd"] == 0.10
    assert summary["scenarios"]["a"]["aggregate"]["cost_mean_usd"] == 0.10


def test_build_summary_records_cassette_tapes_and_model_key():
    results = {
        "a": (_scenario("a"), [_rep(_mech(100.0))]),
        "b": (_scenario("b"), [_rep(_mech(90.0))]),
        "c": (_scenario("c"), [_rep(_mech(90.0))]),
    }
    summary = build_summary(
        run_id="r1",
        git_sha="abc",
        model="m",
        repeat=1,
        cassette_mode="replay",
        registered_tools=[],
        results=results,
        cassette_dir="evals/cassettes",
        cassette_model_key="deepseek-v4-flash",
        cassette_tapes={"a": "model", "b": "shared", "c": "none"},
    )

    assert summary["cassette_model_key"] == "deepseek-v4-flash"
    assert summary["cassette_dir"] == "evals/cassettes"
    assert summary["cassette_tapes"] == {"a": "model", "b": "shared", "c": "none"}

    report = render_harness_report(summary)
    assert (
        "**Cassette:** replay (1 model tapes, 0 promoted-baseline, 1 shared-fallback, 1 none)"
        in report
    )


def test_build_summary_defaults_cassette_tape_keys_when_unreported():
    summary = build_summary(
        run_id="r1",
        git_sha="abc",
        model="m",
        repeat=1,
        cassette_mode="off",
        registered_tools=[],
        results={"a": (_scenario("a"), [_rep(_mech(100.0))])},
    )

    assert summary["cassette_tapes"] == {}
    report = render_harness_report(summary)
    assert "**Cassette:** off" in report
    assert "model tapes" not in report


def test_totals_est_cost_is_zero_not_unpriced_for_priced_free_run():
    results = {"a": (_scenario("a"), [_rep(_mech(100.0), cost=0.0)])}
    summary = build_summary(
        run_id="r1",
        git_sha="abc",
        model="m",
        repeat=1,
        cassette_mode="replay",
        registered_tools=[],
        results=results,
    )

    assert summary["totals"]["est_cost_usd"] == 0.0


def test_totals_est_cost_unpriced_when_any_rep_unpriced():
    results = {
        "a": (_scenario("a"), [_rep(_mech(100.0), cost=0.10), _rep(_mech(90.0), cost=None)]),
    }
    summary = build_summary(
        run_id="r1",
        git_sha="abc",
        model="m",
        repeat=2,
        cassette_mode="replay",
        registered_tools=[],
        results=results,
    )

    assert summary["totals"]["est_cost_usd"] is None


def test_scenario_cost_mean_is_unpriced_when_any_rep_is():
    # The mixed case is ordinary: the rep that writes cache prices to None while
    # the reps that only read cache price fine. Averaging the priced two printed
    # a concrete Cost cell under an "unpriced" header, computed over 2 of 3 reps.
    reps = [
        _rep(_mech(100.0), cost=None),
        _rep(_mech(100.0), cost=0.00205),
        _rep(_mech(100.0), cost=0.00205),
    ]
    summary = build_summary(
        run_id="r1",
        git_sha="abc",
        model="m",
        repeat=3,
        cassette_mode="replay",
        registered_tools=[],
        results={"a": (_scenario("a"), reps)},
    )

    assert summary["scenarios"]["a"]["aggregate"]["cost_mean_usd"] is None
    assert summary["totals"]["est_cost_usd"] is None
    report = render_harness_report(summary)
    assert "| unpriced |" in report
    assert "$0.0021" not in report


def test_summary_uses_the_initial_schema_version():
    summary = build_summary(
        run_id="r1",
        git_sha="abc",
        model="m",
        repeat=1,
        cassette_mode="replay",
        registered_tools=[],
        results={"a": (_scenario("a"), [_rep(_mech(100.0))])},
    )

    assert summary["version"] == 1
    assert summary["kind"] == "harness-eval"


def test_render_report_shows_llm_token_cost():
    results = {"a": (_scenario("a"), [_rep(_mech(100.0), cost=0.10)])}
    summary = build_summary(
        run_id="r1",
        git_sha="abc",
        model="m",
        repeat=1,
        cassette_mode="replay",
        registered_tools=[],
        results=results,
    )

    report = render_harness_report(summary)

    assert "**Cost:** $0.1000 tokens" in report
    assert "| $0.1000 |" in report


def test_report_call_source_split_counts_faulted_and_missed_calls():
    calls = [
        _call("discord_text_search", "live"),
        _call("discord_text_search", "fault"),
        _call("discord_text_search", "replay"),
        _call("discord_text_search", "miss"),
    ]
    results = {"a": (_scenario("a"), [_rep(_mech(100.0), tool_calls=calls)])}
    summary = build_summary(
        run_id="r1",
        git_sha="abc",
        model="m",
        repeat=1,
        cassette_mode="strict",
        registered_tools=[],
        results=results,
    )

    assert summary["recorded_tool_calls"] == {"live": 1, "replay": 1, "fault": 1, "miss": 1}
    report = render_harness_report(summary)
    assert "**Recorded tool calls:** 1 live / 1 replayed / 1 faulted / 1 missed" in report


def test_resolve_api_key_prefers_shell_env_then_dotenv(tmp_path, monkeypatch):
    from evals.models import resolve_api_key

    env_file = tmp_path / ".env"
    env_file.write_text("EVAL_PROBE_KEY=from-dotenv\n")
    monkeypatch.delenv("EVAL_PROBE_KEY", raising=False)
    assert resolve_api_key("EVAL_PROBE_KEY", env_file=env_file) == "from-dotenv"

    monkeypatch.setenv("EVAL_PROBE_KEY", "from-shell")
    assert resolve_api_key("EVAL_PROBE_KEY", env_file=env_file) == "from-shell"

    assert resolve_api_key("", env_file=env_file) == ""
    assert resolve_api_key("MISSING_KEY", env_file=tmp_path / "nope.env") == ""


def test_public_models_example_is_safe_and_complete():
    # Operator constraint: nothing in evals may call the Anthropic API. The
    # anthropic_compat *type* is fine (Messages-over-HTTP against a compatible
    # gateway), so ban the native type, the api.anthropic.com host, and the
    # Anthropic key, not the substring.
    models = load_models(Path("evals/models.example.yaml"))
    specs = [models.baseline, models.judge, *models.candidates.values()]
    for spec in specs:
        assert spec.provider_name != "anthropic"
        assert "api.anthropic.com" not in spec.base_url
        assert spec.api_key_env != "ANTHROPIC_API_KEY"

        assert spec.capabilities
        assert spec.base_url.endswith(".example.invalid/v1")
