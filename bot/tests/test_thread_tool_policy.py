"""The pure thread-tool policy helpers in ``config/fragments/tool_policy.py``."""

from __future__ import annotations

from config.fragments.tool_policy import (
    THREAD_STATE_TOOLS,
    load_blocked_tools,
    thread_state_blocked_tools,
)


def test_no_thread_state_tool_is_offered_outside_a_managed_thread() -> None:
    # These are core tools, so the mask is the only thing keeping them out of an
    # ordinary channel turn's tool list.
    assert thread_state_blocked_tools(managed=False, auto_responding=False) == THREAD_STATE_TOOLS
    assert thread_state_blocked_tools(managed=False, auto_responding=True) == THREAD_STATE_TOOLS


def test_exactly_one_of_pause_resume_is_offered_inside_a_thread() -> None:
    live = thread_state_blocked_tools(managed=True, auto_responding=True)
    paused = thread_state_blocked_tools(managed=True, auto_responding=False)

    assert live == frozenset({"resume_thread_replies"})
    assert paused == frozenset({"pause_thread_replies"})
    # leave_thread stays offered in both states: a managed conversation can
    # always be wound down deliberately.
    assert "leave_thread" not in (live | paused)


def test_load_blocked_tools_unions_the_three_scopes() -> None:
    seen: dict[str, str] = {}

    def fake_guild(guild_id: str) -> frozenset[str]:
        seen["guild"] = guild_id
        return frozenset({"guild_blocked"})

    def fake_channel(channel_id: str) -> frozenset[str]:
        seen["channel"] = channel_id
        return frozenset({"channel_blocked"})

    merged = load_blocked_tools(
        "999",
        "100",
        load_global=lambda: frozenset({"global_blocked"}),
        load_guild=fake_guild,
        load_channel=fake_channel,
    )

    assert merged == frozenset({"global_blocked", "guild_blocked", "channel_blocked"})
    assert seen == {"guild": "999", "channel": "100"}
