"""Exercises app/threads.py and tools/threads.py: resolving a named or
existing Discord thread for a handoff, and rejecting ambiguous or
out-of-allowlist matches before a message is ever redirected.
"""

from __future__ import annotations

import json

import pytest

from app.turn_entry import _platform_scope_blocked_tools
from app.threads import ThreadHandoffManager
from storage.conversations import ConversationStore
from storage.db import Database
from tools.registry import MessageContext, ToolRegistry
from tools.threads import (
    THREAD_NAME_MAX,
    ThreadRequest,
    ThreadTarget,
    build_thread_request_payload,
    init_thread_tools,
    match_thread_target,
)
from trust.tiers import TrustTier


def _ctx(
    thread_id: str | None = None,
    *,
    user_id: str = "123",
    tier: TrustTier = TrustTier.MEMBER,
) -> MessageContext:
    return MessageContext(
        user_id=user_id,
        user_name="Alice",
        guild_id="999",
        channel_id="100",
        thread_id=thread_id,
        trust_tier=tier,
    )


class _FakeStore:
    """The thread-mapping methods of ConversationStore, in memory."""

    def __init__(self) -> None:
        self.rows: dict[str, int] = {}
        self.modes: dict[str, bool] = {}
        self.creators: dict[str, str] = {}

    async def map_thread_conversation(
        self,
        thread_id: str,
        conversation_id: int,
        *,
        creator_user_id: str,
        auto_respond: bool = True,
    ) -> None:
        self.rows[thread_id] = conversation_id
        self.modes[thread_id] = auto_respond
        self.creators[thread_id] = creator_user_id

    async def get_thread_conversation(self, thread_id: str):
        return self.rows.get(thread_id)

    async def get_thread_creator_user_id(self, thread_id: str) -> str | None:
        return self.creators.get(thread_id)

    async def delete_thread_conversation(self, thread_id: str) -> None:
        self.rows.pop(thread_id, None)
        self.modes.pop(thread_id, None)
        self.creators.pop(thread_id, None)

    async def set_thread_auto_respond(self, thread_id: str, auto_respond: bool) -> bool:
        if thread_id not in self.rows:
            return False
        self.modes[thread_id] = auto_respond
        return True

    async def list_thread_conversations(self) -> list[tuple[str, bool]]:
        return [(thread_id, self.modes.get(thread_id, True)) for thread_id in self.rows]


def _manager(store: _FakeStore) -> ThreadHandoffManager:
    return ThreadHandoffManager(store)  # type: ignore[arg-type]


def _handler(registry: ToolRegistry, name: str):
    """Look a tool up in either pool so lifecycle tests stay representation-agnostic."""
    entry = registry._core_tools.get(name) or registry._search_tools.get(name)
    assert entry is not None, name
    return entry.handler


def _tools(manager: ThreadHandoffManager | None, *, can_manage_thread=None):
    registry = ToolRegistry()
    init_thread_tools(
        registry,
        lambda: manager,
        bot_name="kimi",
        can_manage_thread=can_manage_thread,
    )
    return registry


# --- build_thread_request_payload ---


def test_payload_collapses_whitespace_and_validates():
    request = build_thread_request_payload({"name": "  Quest 3 \n  help  "}, _ctx())
    assert request == ThreadRequest(name="Quest 3 help")


def test_payload_truncates_to_discord_cap():
    request = build_thread_request_payload({"name": "x" * 300}, _ctx())
    assert len(request.name) == THREAD_NAME_MAX


@pytest.mark.parametrize("name", [None, "", "   ", "\n\t"])
def test_payload_rejects_blank_name(name):
    with pytest.raises(ValueError, match="thread name"):
        build_thread_request_payload({"name": name}, _ctx())


def test_payload_rejects_when_already_in_thread():
    with pytest.raises(ValueError, match="already in a thread"):
        build_thread_request_payload({"name": "ok"}, _ctx(thread_id="321"))


# --- cross-channel targeting ---


_TARGETS = (
    ThreadTarget(channel_id=200, name="bot-spam"),
    ThreadTarget(channel_id=201, name="vr-help"),
    ThreadTarget(channel_id=202, name="vr-hardware"),
)


def _resolver(candidates=_TARGETS):
    return lambda ctx, raw: match_thread_target(raw, candidates)


@pytest.mark.parametrize(
    ("written", "expected"),
    [
        ("<#200>", 200),  # what Discord puts in the message
        ("200", 200),  # a bare id
        ("#bot-spam", 200),  # as typed, with the hash
        ("bot spam", 200),  # spaces are the hyphens a user did not type
        ("BOT-SPAM", 200),  # case folds
        ("vr-hardw", 202),  # unique prefix
        ("vr-hardwear", 202),  # fuzzy, comfortably above the cutoff
    ],
)
def test_match_resolves_the_written_forms(written, expected):
    assert match_thread_target(written, _TARGETS).channel_id == expected


def test_match_rejects_an_id_outside_the_allowlist():
    with pytest.raises(ValueError, match="one I can start a thread in"):
        match_thread_target("<#999>", _TARGETS)


def test_match_refuses_an_ambiguous_prefix_rather_than_picking():
    # Both vr-help and vr-hardware start with "vr-"; guessing here posts the bot
    # in a channel nobody asked for, which no retry takes back.
    with pytest.raises(ValueError, match="more than one channel"):
        match_thread_target("vr-", _TARGETS)


_DUPLICATES = (
    ThreadTarget(channel_id=200, name="help"),
    ThreadTarget(channel_id=301, name="help"),
)


@pytest.mark.parametrize("written", ["help", "hel", "halp"])
def test_match_refuses_two_channels_sharing_a_name(written):
    """Discord lets two channels share a name (different categories).

    Every match path (exact, prefix, fuzzy) has to see both, or the live
    #help and an archived #help resolve to whichever the lookup happened to
    keep, and the answer lands in the wrong one.
    """
    with pytest.raises(ValueError, match="more than one channel"):
        match_thread_target(written, _DUPLICATES)


def test_match_disambiguates_same_named_channels_by_link():
    # Which is why the refusal asks for the link rather than "the exact name".
    assert match_thread_target("<#301>", _DUPLICATES).channel_id == 301


def test_match_falls_back_to_a_numeric_channel_name():
    # A bare number can be a name (#2024), so a failed id lookup must not stop
    # there with a misleading "that channel isn't one I can use".
    targets = (ThreadTarget(channel_id=200, name="2024"),)
    assert match_thread_target("2024", targets).channel_id == 200


def test_match_lists_the_alternatives_when_nothing_is_close():
    with pytest.raises(ValueError) as excinfo:
        match_thread_target("announcements", _TARGETS)
    message = str(excinfo.value)
    assert "#bot-spam" in message and "#vr-help" in message


def test_match_with_no_candidates_says_so():
    with pytest.raises(ValueError, match="no other channels"):
        match_thread_target("bot-spam", ())


def test_payload_carries_a_resolved_target():
    request = build_thread_request_payload(
        {"name": "Quest help", "channel": "#bot-spam"}, _ctx(), _resolver()
    )
    assert request.target_channel_id == 200


def test_payload_collapses_a_target_naming_the_current_channel():
    # "start a thread in #general" from #general is the ordinary handoff: no
    # anchor, no pointer message, no second notification.
    request = build_thread_request_payload(
        {"name": "Quest help", "channel": "here"},
        _ctx(),
        lambda ctx, raw: ThreadTarget(channel_id=100, name="here"),
    )
    assert request.target_channel_id is None


def test_payload_allows_a_cross_channel_thread_from_inside_a_thread():
    # Nesting is the Discord limit, not "no threads from threads", so "take this
    # to #bot-spam" has to work from a support thread too.
    request = build_thread_request_payload(
        {"name": "Quest help", "channel": "bot-spam"}, _ctx(thread_id="321"), _resolver()
    )
    assert request.target_channel_id == 200


def test_payload_still_rejects_a_same_channel_thread_from_inside_a_thread():
    with pytest.raises(ValueError, match="already in a thread"):
        build_thread_request_payload({"name": "ok"}, _ctx(thread_id="321"), _resolver())


@pytest.mark.parametrize("channel", [None, "", "   ", 5, True])
def test_payload_ignores_a_non_string_channel(channel):
    request = build_thread_request_payload(
        {"name": "Quest help", "channel": channel}, _ctx(), _resolver()
    )
    assert request.target_channel_id is None


def test_payload_without_a_resolver_refuses_a_named_channel():
    # Registration decides whether this deployment can resolve targets at all;
    # the tool must not silently drop the channel the user asked for.
    with pytest.raises(ValueError, match="another channel"):
        build_thread_request_payload({"name": "ok", "channel": "#bot-spam"}, _ctx())


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ({}, None),
        ({"auto_reply": True}, True),
        ({"auto_reply": False}, False),
        # Anything that is not a literal boolean leaves the operator default in
        # charge rather than guessing at what the model meant.
        ({"auto_reply": "false"}, None),
        ({"auto_reply": 0}, None),
        ({"auto_reply": None}, None),
    ],
)
def test_payload_reads_auto_reply_as_a_tristate(args, expected):
    request = build_thread_request_payload({"name": "Quest help", **args}, _ctx())
    assert request.auto_respond is expected


# --- move_to_thread handler ---


def test_move_to_thread_is_in_the_core_toolset():
    registry = _tools(None)

    assert "move_to_thread" in registry._core_tools
    assert "move_to_thread" not in registry._search_tools
    assert "step-by-step troubleshooting" in registry._core_tools["move_to_thread"].description


def test_move_to_thread_is_visible_in_guilds_and_hidden_in_dms():
    registry = _tools(None)

    guild_names = {
        schema["name"]
        for schema in registry.get_tool_schemas(
            TrustTier.MEMBER,
            guild_id="999",
            blocked=_platform_scope_blocked_tools("999"),
        )
    }
    dm_names = {
        schema["name"]
        for schema in registry.get_tool_schemas(
            TrustTier.MEMBER,
            guild_id=None,
            blocked=_platform_scope_blocked_tools(None),
        )
    }

    assert "move_to_thread" in guild_names
    assert "move_to_thread" not in dm_names


@pytest.mark.asyncio
async def test_move_handler_queues_single_slot_request():
    move = _handler(_tools(None), "move_to_thread")
    ctx = _ctx()

    result = json.loads(await move({"name": "First"}, ctx))
    assert result["queued"] is True
    assert ctx.outbox.thread_request == ThreadRequest(name="First")

    await move({"name": "Second"}, ctx)
    assert ctx.outbox.thread_request == ThreadRequest(name="Second")


@pytest.mark.asyncio
async def test_move_handler_rejection_keeps_prior_request():
    move = _handler(_tools(None), "move_to_thread")
    ctx = _ctx()
    await move({"name": "Keep me"}, ctx)

    rejected = await move({"name": "   "}, ctx)

    assert "error" in json.loads(rejected)
    assert ctx.outbox.thread_request == ThreadRequest(name="Keep me")


@pytest.mark.asyncio
async def test_move_handler_carries_auto_reply_through():
    move = _handler(_tools(None), "move_to_thread")
    ctx = _ctx()

    await move({"name": "Quiet thread", "auto_reply": False}, ctx)

    assert ctx.outbox.thread_request == ThreadRequest(name="Quiet thread", auto_respond=False)


# --- leave_thread handler ---


@pytest.mark.asyncio
async def test_leave_handler_requires_a_managed_thread():
    leave = _handler(_tools(_manager(_FakeStore())), "leave_thread")

    assert "error" in json.loads(await leave({}, _ctx()))
    assert "error" in json.loads(await leave({}, _ctx(thread_id="321")))


@pytest.mark.asyncio
async def test_leave_handler_ends_participation():
    store = _FakeStore()
    manager = _manager(store)
    await manager.enroll(321, 1, creator_user_id="123")
    leave = _handler(_tools(manager), "leave_thread")

    result = json.loads(await leave({}, _ctx(thread_id="321")))

    # The tool only queues; the runtime boundary performs the close after the
    # final reply, so the mapping is still intact here.
    assert result["queued"] is True
    assert result["thread_id"] == 321
    assert manager.is_managed(321)
    assert store.rows == {"321": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["leave_thread", "pause_thread_replies", "resume_thread_replies"])
async def test_lifecycle_handlers_reject_an_ordinary_non_creator(tool):
    store = _FakeStore()
    manager = _manager(store)
    await manager.enroll(321, 1, creator_user_id="123")
    handler = _handler(_tools(manager), tool)
    ctx = _ctx(thread_id="321", user_id="456")

    result = json.loads(await handler({}, ctx))

    assert "Only the person who started" in result["error"]
    assert ctx.outbox.thread_close_request is None
    assert manager.is_auto_responding(321)


@pytest.mark.asyncio
async def test_lifecycle_handler_allows_configured_staff():
    manager = _manager(_FakeStore())
    await manager.enroll(321, 1, creator_user_id="123")
    leave = _handler(_tools(manager), "leave_thread")

    result = json.loads(await leave({}, _ctx(thread_id="321", user_id="456", tier=TrustTier.STAFF)))

    assert result["queued"] is True


@pytest.mark.asyncio
async def test_lifecycle_handler_allows_effective_manage_threads_permission():
    manager = _manager(_FakeStore())
    await manager.enroll(321, 1, creator_user_id="123")
    checked: list[tuple[str, int]] = []

    def can_manage(ctx: MessageContext, thread_id: int) -> bool:
        checked.append((ctx.user_id, thread_id))
        return True

    leave = _handler(
        _tools(manager, can_manage_thread=can_manage),
        "leave_thread",
    )

    result = json.loads(await leave({}, _ctx(thread_id="321", user_id="456")))

    assert result["queued"] is True
    assert checked == [("456", 321)]


@pytest.mark.asyncio
async def test_thread_without_creator_fails_closed_for_member():
    store = _FakeStore()
    manager = _manager(store)
    await manager.enroll(321, 1, creator_user_id="123")
    store.creators.pop("321")
    leave = _handler(_tools(manager), "leave_thread")

    result = json.loads(await leave({}, _ctx(thread_id="321")))

    assert "Only the person who started" in result["error"]


# --- pause/resume handlers ---


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["pause_thread_replies", "resume_thread_replies"])
async def test_mode_handlers_require_a_managed_thread(tool):
    handler = _handler(_tools(_manager(_FakeStore())), tool)

    assert "error" in json.loads(await handler({}, _ctx()))
    assert "error" in json.loads(await handler({}, _ctx(thread_id="321")))


@pytest.mark.asyncio
async def test_pause_then_resume_flips_the_mode_and_persists():
    store = _FakeStore()
    manager = _manager(store)
    await manager.enroll(321, 1, creator_user_id="123")
    registry = _tools(manager)
    pause = _handler(registry, "pause_thread_replies")
    resume = _handler(registry, "resume_thread_replies")

    paused = json.loads(await pause({}, _ctx(thread_id="321")))
    assert paused["paused"] is True
    assert manager.is_managed(321)
    assert not manager.is_auto_responding(321)
    assert store.modes == {"321": False}
    # The note has to carry both halves of the way back: the tool the model
    # calls, and the phrase a user can type to reach a paused thread.
    assert "resume_thread_replies" in paused["note"]
    assert "hey kimi" in paused["note"]

    resumed = json.loads(await resume({}, _ctx(thread_id="321")))
    assert resumed["resumed"] is True
    assert manager.is_auto_responding(321)
    assert store.modes == {"321": True}


@pytest.mark.asyncio
async def test_mode_handlers_never_act_on_another_thread():
    store = _FakeStore()
    manager = _manager(store)
    await manager.enroll(321, 1, creator_user_id="123")
    await manager.enroll(654, 2, creator_user_id="123")
    pause = _handler(_tools(manager), "pause_thread_replies")

    # The thread is derived from the turn, so a model argument cannot redirect it.
    await pause({"thread_id": 654}, _ctx(thread_id="321"))

    assert not manager.is_auto_responding(321)
    assert manager.is_auto_responding(654)


# --- ThreadHandoffManager ---


@pytest.mark.asyncio
async def test_manager_enroll_leave_and_load_round_trip():
    store = _FakeStore()
    manager = _manager(store)

    await manager.enroll(555, 7, creator_user_id="123")
    assert manager.is_managed(555)
    assert manager.is_auto_responding(555)
    assert await manager.is_creator(555, "123")
    assert not await manager.is_creator(555, "456")
    assert store.rows == {"555": 7}

    assert await manager.leave(555) is True
    assert not manager.is_managed(555)
    assert await manager.leave(555) is False

    await store.map_thread_conversation("777", 9, creator_user_id="123")
    fresh = _manager(store)
    await fresh.load()
    assert fresh.is_managed(777)
    assert fresh.managed_count == 1


@pytest.mark.asyncio
async def test_manager_enroll_can_start_a_thread_paused():
    store = _FakeStore()
    manager = _manager(store)

    await manager.enroll(555, 7, creator_user_id="123", auto_respond=False)

    assert manager.is_managed(555)
    assert not manager.is_auto_responding(555)
    assert store.modes == {"555": False}


@pytest.mark.asyncio
async def test_manager_load_restores_the_paused_mode():
    store = _FakeStore()
    await store.map_thread_conversation("555", 7, creator_user_id="123", auto_respond=False)
    await store.map_thread_conversation("777", 9, creator_user_id="123", auto_respond=True)

    manager = _manager(store)
    await manager.load()

    # A pause has to survive a restart; otherwise the bot starts talking again
    # in a thread someone deliberately quieted.
    assert manager.is_managed(555) and not manager.is_auto_responding(555)
    assert manager.is_managed(777) and manager.is_auto_responding(777)
    assert manager.managed_count == 2
    assert manager.auto_respond_count == 1


@pytest.mark.asyncio
async def test_manager_pause_and_resume_reject_unmanaged_threads():
    manager = _manager(_FakeStore())

    assert await manager.pause(555) is False
    assert await manager.resume(555) is False
    assert not manager.is_managed(555)


@pytest.mark.asyncio
async def test_mode_change_drops_a_thread_whose_row_was_swept():
    """A retention sweep or privacy wipe can remove the row under a live id.

    Reporting success there would have the bot announce a mode change that no
    longer survives a restart.
    """
    store = _FakeStore()
    manager = _manager(store)
    await manager.enroll(555, 7, creator_user_id="123")
    store.rows.pop("555")
    store.modes.pop("555")

    assert await manager.resume(555) is False
    assert not manager.is_managed(555)
    assert not manager.is_auto_responding(555)


@pytest.mark.asyncio
async def test_manager_leave_and_forget_clear_both_sets():
    store = _FakeStore()
    manager = _manager(store)
    await manager.enroll(555, 7, creator_user_id="123")
    await manager.pause(555)

    manager.forget(555)
    assert not manager.is_managed(555)
    assert not manager.is_auto_responding(555)
    # forget() is the stale-row path: it never writes, so the row survives.
    assert store.rows == {"555": 7}

    await manager.enroll(555, 7, creator_user_id="123")
    await manager.leave(555)
    assert manager.auto_respond_count == 0
    assert store.rows == {}


# --- ConversationStore thread mapping on a real database ---


@pytest.mark.asyncio
async def test_thread_conversation_mapping_round_trip(tmp_path):
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        store = ConversationStore(db)
        first = await store.get_or_create("guild:999:channel:100:thread:main:root:1", "general")
        second = await store.get_or_create("guild:999:channel:100:thread:main:root:2", "general")

        await store.map_thread_conversation("555", first, creator_user_id="alice")
        record = await store.get_thread_conversation("555")
        assert record is not None
        assert record.id == first
        assert await store.get_thread_creator_user_id("555") == "alice"
        assert await store.list_thread_conversations() == [("555", True)]

        # Re-mapping replaces, never duplicates, and carries the mode explicitly
        # rather than letting INSERT OR REPLACE reset it to the column default.
        await store.map_thread_conversation(
            "555", second, creator_user_id="bob", auto_respond=False
        )
        record = await store.get_thread_conversation("555")
        assert record is not None
        assert record.id == second
        assert await store.get_thread_creator_user_id("555") == "bob"
        assert await store.list_thread_conversations() == [("555", False)]

        assert await store.set_thread_auto_respond("555", True) is True
        assert await store.list_thread_conversations() == [("555", True)]
        # No row, no update: the caller must be able to tell.
        assert await store.set_thread_auto_respond("999", False) is False

        await store.delete_thread_conversation("555")
        assert await store.get_thread_conversation("555") is None
        assert await store.list_thread_conversations() == []
    finally:
        await db.close()
