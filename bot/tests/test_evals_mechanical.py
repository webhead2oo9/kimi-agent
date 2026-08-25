from evals.capture import ToolCallRecord
from evals.harness import ScenarioRun, TurnRecord
from evals.mechanical import compute_mechanical
from evals.scenario import Expect, Scenario
from trust.tiers import TrustTier


def _run(tool_calls, final_text="ok"):
    return ScenarioRun(
        scenario_id="s",
        model_label="m",
        turns=[TurnRecord("q", final_text, tool_calls, tokens=10, latency_ms=5)],
    )


def test_mechanical_flags_missing_and_unexpected_tools():
    scenario = Scenario(
        id="s",
        category="tooling",
        trust_tier=TrustTier.MEMBER,
        turns=["q"],
        expect=Expect(should_use_tools=["wolfram_alpha"], should_not_use_tools=["publish_page"]),
    )
    run = _run(
        [
            ToolCallRecord("publish_page", {}, "{}", ok=True, duration_ms=1),
            ToolCallRecord("browse_tools", {}, "{}", ok=True, duration_ms=1),
        ]
    )
    m = compute_mechanical(scenario, run)
    assert m.missing_tools == ["wolfram_alpha"]
    assert m.unexpected_tools == ["publish_page"]
    assert m.tool_call_count == 2
    assert m.tokens == 10


def test_mechanical_counts_tool_errors():
    scenario = Scenario(id="s", category="tooling", trust_tier=TrustTier.MEMBER, turns=["q"])
    run = _run([ToolCallRecord("probe", {}, '{"error": "boom"}', ok=False, duration_ms=1)])
    m = compute_mechanical(scenario, run)
    assert m.tool_errors == 1


def test_mechanical_flags_raw_json_reply():
    scenario = Scenario(id="s", category="tooling", trust_tier=TrustTier.MEMBER, turns=["q"])
    run = _run([], final_text='{"raw": "dump"}')
    m = compute_mechanical(scenario, run)
    assert m.raw_json_reply is True


def test_mechanical_clean_run_scores_100_and_passes():
    scenario = Scenario(
        id="s",
        category="tooling",
        trust_tier=TrustTier.MEMBER,
        turns=["q"],
        expect=Expect(should_use_tools=["wolfram_alpha"]),
    )
    run = _run([ToolCallRecord("wolfram_alpha", {"q": "x"}, "{}", ok=True, duration_ms=1)])
    m = compute_mechanical(scenario, run)
    assert m.score == 100.0
    assert m.passed is True
    assert m.first_expected_call_index == 0


def test_mechanical_counts_repeats_only_after_a_success():
    scenario = Scenario(id="s", category="tooling", trust_tier=TrustTier.MEMBER, turns=["q"])
    looped = _run(
        [
            ToolCallRecord("probe", {"x": 1}, "{}", ok=True, duration_ms=1),
            ToolCallRecord("probe", {"x": 1}, "{}", ok=True, duration_ms=1),
        ]
    )
    m = compute_mechanical(scenario, looped)
    assert m.repeated_calls == 1
    assert m.score == 95.0

    # Retrying identical args after an error is recovery, not a loop.
    retried = _run(
        [
            ToolCallRecord("probe", {"x": 1}, '{"error": "boom"}', ok=False, duration_ms=1),
            ToolCallRecord("probe", {"x": 1}, "{}", ok=True, duration_ms=1),
        ]
    )
    m = compute_mechanical(scenario, retried)
    assert m.repeated_calls == 0
    assert m.unrecovered_errors == 0


def test_mechanical_unrecovered_live_error_penalized_twice():
    scenario = Scenario(id="s", category="tooling", trust_tier=TrustTier.MEMBER, turns=["q"])
    run = _run([ToolCallRecord("probe", {}, '{"error": "boom"}', ok=False, duration_ms=1)])
    m = compute_mechanical(scenario, run)
    assert m.tool_errors == 1
    assert m.unrecovered_errors == 1
    assert m.score == 80.0  # -5 live error, -15 unrecovered
    assert m.passed is False


def test_mechanical_recovered_fault_is_free():
    scenario = Scenario(id="s", category="tooling", trust_tier=TrustTier.MEMBER, turns=["q"])
    run = _run(
        [
            ToolCallRecord(
                "probe", {}, '{"error": "504"}', ok=False, duration_ms=1, source="fault"
            ),
            ToolCallRecord("probe", {}, "{}", ok=True, duration_ms=1),
        ]
    )
    m = compute_mechanical(scenario, run)
    assert m.tool_errors == 1  # still visible in the raw count
    assert m.unrecovered_errors == 0
    assert m.score == 100.0  # scripted fault + clean recovery costs nothing
    assert m.passed is True


def test_mechanical_reply_checks_and_call_budget():
    scenario = Scenario(
        id="s",
        category="tooling",
        trust_tier=TrustTier.MEMBER,
        turns=["q"],
        expect=Expect(max_tool_calls=1, reply_must_match=["beat saber", "players"]),
    )
    run = _run(
        [
            ToolCallRecord("a", {}, "{}", ok=True, duration_ms=1),
            ToolCallRecord("b", {}, "{}", ok=True, duration_ms=1),
        ],
        final_text="Beat Saber is popular.",
    )
    m = compute_mechanical(scenario, run)
    assert m.failed_reply_checks == ["players"]  # case-insensitive match on the other
    assert m.over_budget is True
    assert m.score == 100.0 - 15.0 - 10.0
    assert m.passed is False


def test_mechanical_missing_attachment_flag():
    scenario = Scenario(
        id="s",
        category="workspace",
        trust_tier=TrustTier.MEMBER,
        turns=["q"],
        expect=Expect(attaches_file=True),
    )
    bare = compute_mechanical(scenario, _run([]))
    assert bare.missing_attachment is True
    assert bare.score == 80.0
    assert bare.passed is False

    attached = ScenarioRun(
        scenario_id="s",
        model_label="m",
        turns=[
            TurnRecord(
                "q", "here you go", [], tokens=1, latency_ms=1, attached_files=["files/chart.png"]
            )
        ],
    )
    m = compute_mechanical(scenario, attached)
    assert m.missing_attachment is False
    assert m.passed is True


def test_mechanical_clean_multi_turn_run_has_no_flags():
    scenario = Scenario(
        id="s",
        category="tooling",
        trust_tier=TrustTier.MEMBER,
        turns=["a", "b"],
        expect=Expect(should_use_tools=["wolfram_alpha"], should_not_use_tools=["publish_page"]),
    )
    run = ScenarioRun(
        scenario_id="s",
        model_label="m",
        turns=[
            TurnRecord(
                "a",
                "sure thing",
                [ToolCallRecord("wolfram_alpha", {}, "{}", ok=True, duration_ms=1)],
                tokens=4,
                latency_ms=2,
            ),
            TurnRecord(
                "b",
                "all good, no JSON here",
                [ToolCallRecord("browse_tools", {}, "{}", ok=True, duration_ms=1)],
                tokens=6,
                latency_ms=3,
            ),
        ],
    )
    m = compute_mechanical(scenario, run)
    assert m.missing_tools == []  # should_use tool present in turn 1
    assert m.unexpected_tools == []  # forbidden tool absent
    assert m.tool_call_count == 2  # aggregated across both turns
    assert m.tokens == 10  # 4 + 6 across turns
    assert m.tool_errors == 0
    assert m.raw_json_reply is False  # plain prose, the negative case
