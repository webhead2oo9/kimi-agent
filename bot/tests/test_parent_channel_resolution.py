"""Verify the shared definition of "this channel" inside a thread.

Per-channel operator config (tool pins, the denylist, handoff policy, and the
instructions fragment) is keyed on the channel a thread hangs off.
``resolve_parent_channel_id`` supplies that key to every lookup.
"""

from __future__ import annotations

import discord
import pytest

from agent.turn import TurnPreparationInput
from app.turn_entry import _tool_config_channel_id, resolve_parent_channel_id
from trust.tiers import TrustTier


def _thread(thread_id: int, parent_id: int | None) -> discord.Thread:
    """A bare Thread instance: the helper branches on isinstance, not on init."""
    thread = object.__new__(discord.Thread)
    thread.id = thread_id  # type: ignore[misc]
    thread.parent_id = parent_id  # type: ignore[misc,assignment]
    return thread


def _channel(channel_id: int) -> discord.TextChannel:
    channel = object.__new__(discord.TextChannel)
    channel.id = channel_id  # type: ignore[misc]
    return channel


def test_a_thread_resolves_to_its_parent() -> None:
    assert resolve_parent_channel_id(_thread(77, 20)) == "20"


def test_a_plain_channel_resolves_to_itself() -> None:
    assert resolve_parent_channel_id(_channel(20)) == "20"


@pytest.mark.parametrize(
    "channel",
    [None, object()],
    ids=["missing", "no-id-attribute"],
)
def test_an_unusable_channel_resolves_to_nothing(channel: object) -> None:
    """Empty, never a guess: callers fall back to the turn's own channel id."""
    assert resolve_parent_channel_id(channel) == ""


def test_a_parentless_thread_falls_back_to_itself() -> None:
    """Fail safe to the thread id when no parent is available."""
    assert resolve_parent_channel_id(_thread(77, None)) == "77"


def _source(channel: object, channel_id: str) -> TurnPreparationInput:
    return TurnPreparationInput(
        raw_content="hi",
        source_message=type("Msg", (), {"channel": channel})(),
        bot_user=object(),
        guild_id="1",
        channel_id=channel_id,
        thread_id=None,
        channel_name="c",
        user_id="2",
        user_name="u",
        trust_tier=TrustTier.MEMBER,
        conversation_key="k",
    )


def test_tool_config_follows_the_parent_channel() -> None:
    source = _source(_thread(77, 20), "77")
    assert _tool_config_channel_id(source) == "20"


def test_tool_config_falls_back_when_the_source_has_no_channel() -> None:
    source = _source(None, "100")
    assert _tool_config_channel_id(source) == "100"
