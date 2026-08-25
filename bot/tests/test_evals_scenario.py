from pathlib import Path

import pytest

from evals.scenario import Scenario, load_scenario, load_scenarios, split_gated_scenarios
from trust.tiers import TrustTier


def _write(path, body):
    path.write_text(body)
    return path


def test_load_scenario_parses_all_fields(tmp_path):
    path = _write(
        tmp_path / "s.yaml",
        "id: wolfram-derivative\n"
        "category: tooling\n"
        "trust_tier: MEMBER\n"
        'channel: { name: xr-talk, id: "369" }\n'
        "seeded_history:\n"
        "  - { role: user, name: Alice, text: hey }\n"
        "  - { role: assistant, text: hi }\n"
        "turns:\n"
        "  - what is the derivative of sin(x^2)?\n"
        "expect:\n"
        "  should_use_tools: [wolfram_alpha]\n"
        "  should_not_use_tools: [publish_page]\n"
        "  notes: should call wolfram\n",
    )
    s = load_scenario(path)
    assert isinstance(s, Scenario)
    assert s.id == "wolfram-derivative"
    assert s.trust_tier is TrustTier.MEMBER
    assert s.channel_name == "xr-talk"
    assert s.channel_id == "369"
    assert [t.text for t in s.turns] == ["what is the derivative of sin(x^2)?"]
    assert s.seeded_history[0] == ("user", "Alice", "hey")
    assert s.seeded_history[1] == ("assistant", "", "hi")
    assert s.expect.should_use_tools == ["wolfram_alpha"]
    assert s.expect.should_not_use_tools == ["publish_page"]


def test_load_scenario_parses_image_turns_on_both_rails(tmp_path):
    path = _write(
        tmp_path / "s.yaml",
        "id: vision\n"
        "trust_tier: MEMBER\n"
        "turns:\n"
        "  - plain text turn\n"
        "  - text: compare these\n"
        "    images: [checker-yellow.png]\n"
        "    reply_images: [bands-rgb.png]\n"
        "    reply_author: Ana\n"
        "    reply_text: here you go\n",
    )

    scenario = load_scenario(path)

    plain, rich = scenario.turns
    # The string form must keep working: most scenarios have no attachments and
    # should not pay a nesting level for the ones that do.
    assert (plain.text, plain.images, plain.reply_images) == ("plain text turn", (), ())
    assert plain.has_images is False
    assert rich.images == ("checker-yellow.png",)
    assert rich.reply_images == ("bands-rgb.png",)
    assert (rich.reply_author, rich.reply_text) == ("Ana", "here you go")
    assert rich.has_images is True


def test_load_scenario_rejects_reply_text_without_reply_images(tmp_path):
    # reply_text alone builds no ReplyContext, so the scenario would silently
    # grade as if the quoted message had never existed.
    path = _write(
        tmp_path / "s.yaml",
        "id: bad\ntrust_tier: MEMBER\nturns:\n  - text: hi\n    reply_text: orphaned\n",
    )

    with pytest.raises(ValueError, match="reply_text"):
        load_scenario(path)


def test_load_scenario_rejects_empty_turns(tmp_path):
    path = _write(
        tmp_path / "bad.yaml",
        "id: x\ncategory: tooling\ntrust_tier: MEMBER\nturns: []\n",
    )
    with pytest.raises(ValueError):
        load_scenario(path)


def test_load_scenarios_reads_directory_recursively(tmp_path):
    (tmp_path / "tooling").mkdir()
    _write(
        tmp_path / "tooling" / "a.yaml",
        "id: a\ncategory: tooling\ntrust_tier: MEMBER\nturns: [hi]\n",
    )
    scenarios = load_scenarios(tmp_path)
    assert [s.id for s in scenarios] == ["a"]


def test_bundled_scenarios_use_webhead_not_prior_name():
    scenarios = load_scenarios(Path("evals/scenarios"))
    fields: list[str] = []
    for scenario in scenarios:
        fields.extend(
            [
                scenario.id,
                scenario.category,
                scenario.channel_name,
                scenario.guild_name,
                "\n".join(turn.text for turn in scenario.turns),
                "\n".join(
                    f"{role} {name} {message}" for role, name, message in scenario.seeded_history
                ),
                " ".join(scenario.activated_tools),
                " ".join(scenario.expect.should_use_tools),
                " ".join(scenario.expect.should_not_use_tools),
                scenario.expect.notes,
            ]
        )
    text = "\n".join(fields)
    prior_name = "cha" + "rlie"
    assert prior_name not in text.lower()
    assert any(
        name == "webhead" for scenario in scenarios for _, name, _ in scenario.seeded_history
    )


def test_load_scenario_parses_reliability_fields(tmp_path):
    path = _write(
        tmp_path / "s.yaml",
        "id: steam-recovery\n"
        "category: tooling\n"
        "trust_tier: MEMBER\n"
        'turns: ["how many players?"]\n'
        "faults:\n"
        '  - { tool: get_steam_game_info, message: "upstream 504", times: 2 }\n'
        "expect:\n"
        "  should_use_tools: [get_steam_game_info]\n"
        "  max_tool_calls: 6\n"
        '  reply_must_match: ["player"]\n',
    )
    s = load_scenario(path)
    assert s.expect.max_tool_calls == 6
    assert s.expect.reply_must_match == ["player"]
    assert len(s.faults) == 1
    assert s.faults[0].tool == "get_steam_game_info"
    assert s.faults[0].message == "upstream 504"
    assert s.faults[0].times == 2


def test_load_scenario_rejects_bad_regex_and_incomplete_fault(tmp_path):
    bad_regex = _write(
        tmp_path / "regex.yaml",
        'id: x\ncategory: t\ntrust_tier: MEMBER\nturns: [hi]\nexpect:\n  reply_must_match: ["("]\n',
    )
    with pytest.raises(ValueError):
        load_scenario(bad_regex)

    bad_fault = _write(
        tmp_path / "fault.yaml",
        "id: x\ncategory: t\ntrust_tier: MEMBER\nturns: [hi]\nfaults:\n  - { tool: probe }\n",
    )
    with pytest.raises(ValueError):
        load_scenario(bad_fault)


def test_load_scenario_parses_staff_tier_and_rejects_invalid(tmp_path):
    staff = _write(
        tmp_path / "staff.yaml",
        "id: s\ncategory: safety\ntrust_tier: STAFF\nturns: [hi]\n",
    )
    assert load_scenario(staff).trust_tier is TrustTier.STAFF

    bad = _write(
        tmp_path / "bad-tier.yaml",
        "id: s\ncategory: safety\ntrust_tier: wizard\nturns: [hi]\n",
    )
    with pytest.raises(ValueError):
        load_scenario(bad)


def test_split_gated_scenarios_holds_back_only_undeclared_hosts(tmp_path):
    """requires_tools means "this host cannot", not "regression".

    The harness hard-fails on an expected-but-unregistered tool, which is right for
    a real gap and wrong for browser/run_code on a box without a Linux sandbox.
    Declaring the requirement moves a scenario from "fails the whole run" to
    "reported as skipped".
    """

    gated = tmp_path / "gated.yaml"
    gated.write_text(
        "id: needs-sandbox\ntrust_tier: MEMBER\nrequires_tools: [run_code]\n"
        "turns:\n  - 'compute something big'\n",
        encoding="utf-8",
    )
    plain = tmp_path / "plain.yaml"
    plain.write_text(
        "id: plain\ntrust_tier: MEMBER\nturns:\n  - 'hello'\n",
        encoding="utf-8",
    )

    scenarios = load_scenarios(tmp_path)
    runnable, held = split_gated_scenarios(scenarios, {"read_file"})
    assert [s.id for s in runnable] == ["plain"]
    assert [(s.id, missing) for s, missing in held] == [("needs-sandbox", ["run_code"])]

    # Same scenarios on a host that does register it: nothing sits out.
    runnable, held = split_gated_scenarios(scenarios, {"read_file", "run_code"})
    assert sorted(s.id for s in runnable) == ["needs-sandbox", "plain"]
    assert held == []
