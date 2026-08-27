"""Cross-channel thread creation: the gate, the anchor, and the cleanup.

``move_to_thread(channel=...)`` is the one thread affordance that puts the bot's
voice in a channel nobody in the conversation is looking at, so most of what is
pinned here is refusal. The gate encodes one rule: *the bot does nothing in
another channel the asker could not do themselves*. That is what makes the
feature escalation-free, and it is only true while every one of these filters
holds.

The pure matching half lives in ``tests/test_thread_handoff.py``; this module
covers the Discord-shaped half: which channels become candidates at all, and
what the boundary actually posts, deletes, and enrolls.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

import app.runtime as app_runtime
import app.thread_handoff_boundary as thread_boundary
from config.settings import Settings
from tests.helpers import StubProviderManager
from tools.registry import MessageContext
from tools.threads import ThreadRequest
from trust.tiers import TrustTier


@dataclass
class _Perms:
    view_channel: bool = True
    send_messages: bool = True
    create_public_threads: bool = True
    send_messages_in_threads: bool = True
    manage_threads: bool = False


class _Thread:
    def __init__(
        self,
        thread_id: int,
        parent: Any = None,
        perms: dict[int, _Perms] | None = None,
    ) -> None:
        self.id = thread_id
        self.parent = parent
        self.guild: Any = None
        self._perms = perms or {}
        self.added: list[Any] = []
        self.add_user = AsyncMock(side_effect=self.added.append)
        self.delete = AsyncMock()

    def permissions_for(self, who: Any) -> _Perms:
        return self._perms.get(who.id, _Perms())


class _PartialMessage:
    def __init__(self, message_id: int, log: list[int]) -> None:
        self.id = message_id
        self._log = log
        self.delete = AsyncMock(side_effect=lambda: self._log.append(self.id))


class _Anchor:
    def __init__(self, message_id: int, channel: Any, content: str, deleted: list[int]) -> None:
        self.id = message_id
        self.channel = channel
        self.content = content
        self._deleted = deleted
        self.create_thread = AsyncMock(side_effect=self._create)

    async def _create(self, *, name: str, auto_archive_duration: int | None = None) -> _Thread:
        self.channel.created.append((name, auto_archive_duration))
        # A public thread created from a message shares its starter's ID.
        return _Thread(self.id, parent=self.channel)

    async def delete(self) -> None:
        self._deleted.append(self.id)


class _TextChannel:
    def __init__(self, channel_id: int, name: str, perms: dict[int, _Perms] | None = None) -> None:
        self.id = channel_id
        self.name = name
        self._perms = perms or {}
        self.sent: list[tuple[str, Any]] = []
        self.created: list[tuple[str, int | None]] = []
        self.deleted: list[int] = []

    def is_news(self) -> bool:
        return False

    def permissions_for(self, who: Any) -> _Perms:
        return self._perms.get(who.id, _Perms())

    async def send(self, content: str, **kwargs: Any) -> _Anchor:
        self.sent.append((content, kwargs.get("allowed_mentions")))
        return _Anchor(7000 + len(self.sent), self, content, self.deleted)

    def get_partial_message(self, message_id: int) -> _PartialMessage:
        return _PartialMessage(message_id, self.deleted)


class _Forum:
    """Stands in for a forum channel: present in the guild, never a candidate."""

    def __init__(self, channel_id: int, name: str) -> None:
        self.id = channel_id
        self.name = name


class _Member:
    def __init__(self, user_id: int, display_name: str = "Alice") -> None:
        self.id = user_id
        self.display_name = display_name
        self.guild: Any = None


class _Guild:
    def __init__(self, guild_id: int, channels: list[Any], me: _Member, members: list[_Member]):
        self.id = guild_id
        self.me = me
        self._channels = {channel.id: channel for channel in channels}
        self._threads: dict[int, _Thread] = {}
        self._members = {member.id: member for member in members}
        for member in members:
            member.guild = self

    def get_channel(self, channel_id: int) -> Any:
        return self._channels.get(channel_id)

    def get_member(self, user_id: int) -> _Member | None:
        return self._members.get(user_id)

    def get_thread(self, thread_id: int) -> _Thread | None:
        return self._threads.get(thread_id)


BOT_ID = 42
ASKER_ID = 123


def _settings():
    # Deliberately unannotated, like tests/test_message_routing.py: `_env_file`
    # is a pydantic-settings runtime kwarg mypy does not know about, and
    # _env_file=None keeps the test hermetic against the developer's real .env.
    return Settings(_env_file=None, hindsight_url="", model_api_key="test-key")


def _app(monkeypatch, *, targets: set[str], channels: list[Any] | None = None):
    """A wired application plus the guild its thread targets live in."""
    monkeypatch.setattr(
        app_runtime, "build_provider_manager", lambda settings: StubProviderManager(settings)
    )
    monkeypatch.setattr(discord, "TextChannel", _TextChannel)
    monkeypatch.setattr(discord, "Member", _Member)
    monkeypatch.setattr(discord, "Thread", _Thread)
    monkeypatch.setattr(
        thread_boundary, "load_guild_thread_targets", lambda gid: frozenset(targets)
    )

    app = app_runtime.build_app(_settings())
    me = _Member(BOT_ID, "Kimi")
    asker = _Member(ASKER_ID, "Alice")
    guild = _Guild(
        999,
        channels if channels is not None else [_TextChannel(200, "bot-spam")],
        me,
        [me, asker],
    )
    app.bot = cast(Any, MagicMock(get_guild=lambda gid: guild if gid == 999 else None))
    return app, guild, asker


def _ctx(*, platform_member: Any | None = None) -> MessageContext:
    return MessageContext(
        user_id=str(ASKER_ID),
        user_name="Alice",
        guild_id="999",
        channel_id="100",
        thread_id=None,
        trust_tier=TrustTier.MEMBER,
        platform_member=platform_member,
    )


def _message(guild: _Guild, author: _Member, channel: Any | None = None) -> Any:
    message = MagicMock()
    message.id = 1000
    message.guild = guild
    message.author = author
    message.channel = channel if channel is not None else _TextChannel(100, "general")
    message.reply = AsyncMock()
    return message


# --- the candidate gate ---


def test_allowlisted_text_channel_is_a_candidate(monkeypatch):
    app, guild, asker = _app(monkeypatch, targets={"200"})

    names = [
        t.name for t in app.threads._thread_target_candidates(cast(Any, guild), cast(Any, asker))
    ]

    assert names == ["bot-spam"]


def test_a_channel_off_the_allowlist_is_never_a_candidate(monkeypatch):
    app, guild, asker = _app(
        monkeypatch,
        targets={"200"},
        channels=[_TextChannel(200, "bot-spam"), _TextChannel(201, "staff-only")],
    )

    ids = [
        t.channel_id
        for t in app.threads._thread_target_candidates(cast(Any, guild), cast(Any, asker))
    ]

    assert ids == [200]


def test_no_allowlist_means_no_candidates(monkeypatch):
    """Absent thread_targets is the feature being off, not "anywhere"."""
    app, guild, asker = _app(monkeypatch, targets=set())

    assert app.threads._thread_target_candidates(cast(Any, guild), cast(Any, asker)) == []


def test_a_forum_on_the_allowlist_is_refused(monkeypatch):
    # A forum post *is* a thread, so there is no message to anchor one to. The
    # config authoring filters forums out; this is the second, independent
    # refusal that a hand-edited fragment still hits.
    app, guild, asker = _app(
        monkeypatch,
        targets={"200", "300"},
        channels=[_TextChannel(200, "bot-spam"), _Forum(300, "support-forum")],
    )

    names = [
        t.name for t in app.threads._thread_target_candidates(cast(Any, guild), cast(Any, asker))
    ]

    assert names == ["bot-spam"]


@pytest.mark.parametrize(
    "denied",
    ["view_channel", "send_messages", "create_public_threads", "send_messages_in_threads"],
)
@pytest.mark.parametrize("who", ["asker", "bot"])
def test_a_channel_neither_party_may_use_is_refused(monkeypatch, denied, who):
    holder = ASKER_ID if who == "asker" else BOT_ID
    app, guild, asker = _app(
        monkeypatch,
        targets={"200"},
        channels=[_TextChannel(200, "bot-spam", {holder: _Perms(**{denied: False})})],
    )

    assert app.threads._thread_target_candidates(cast(Any, guild), cast(Any, asker)) == []


def test_a_target_channel_with_handoff_off_is_refused(monkeypatch):
    # Listing a channel as a target does not override its own opt-out.
    app, guild, asker = _app(monkeypatch, targets={"200"})
    monkeypatch.setattr(thread_boundary, "load_channel_thread_handoff", lambda cid: False)

    assert app.threads._thread_target_candidates(cast(Any, guild), cast(Any, asker)) == []


def test_a_target_channel_blocking_move_to_thread_is_refused(monkeypatch):
    """The other half of "handoff is off here".

    Channel and tool config both express "no bot threads in this channel" as a
    `blocked_tools` entry, and the *source*-channel gate treats that as
    equivalent to `thread_handoff: false`. A target channel has to agree, or
    staff turn it off the only way the config offers and the bot posts there
    anyway.
    """
    app, guild, asker = _app(monkeypatch, targets={"200"})
    monkeypatch.setattr(
        thread_boundary, "load_blocked_tools", lambda *a, **kw: frozenset({"move_to_thread"})
    )

    assert app.threads._thread_target_candidates(cast(Any, guild), cast(Any, asker)) == []


def test_a_target_outside_the_deployment_allowlist_is_refused(monkeypatch):
    """A guild fragment must not reach past ALLOWED_CHANNEL_IDS.

    Otherwise a target outside it gets an anchor, a thread, and an added user.
    Then `is_eligible_to_respond` drops every follow-up, so the asker is pinged
    into a thread the bot will never answer in again.
    """
    app, guild, asker = _app(monkeypatch, targets={"200"})
    app.settings.allowed_channel_ids = "100,101"

    assert app.threads._thread_target_candidates(cast(Any, guild), cast(Any, asker)) == []

    app.settings.allowed_channel_ids = "100,200"
    ids = [
        t.channel_id
        for t in app.threads._thread_target_candidates(cast(Any, guild), cast(Any, asker))
    ]
    assert ids == [200]


def test_an_announcement_channel_is_refused(monkeypatch):
    # A news channel is a TextChannel in discord.py, so isinstance alone lets it
    # through, but a thread on an announcement post is not what handoff means.
    news = _TextChannel(200, "announcements")
    news.is_news = lambda: True
    app, guild, asker = _app(monkeypatch, targets={"200"}, channels=[news])

    assert app.threads._thread_target_candidates(cast(Any, guild), cast(Any, asker)) == []


def test_a_missing_channel_is_skipped_rather_than_raising(monkeypatch):
    app, guild, asker = _app(monkeypatch, targets={"200", "999999"})

    ids = [
        t.channel_id
        for t in app.threads._thread_target_candidates(cast(Any, guild), cast(Any, asker))
    ]

    assert ids == [200]


# --- the resolver seam ---


def test_resolver_matches_a_written_channel(monkeypatch):
    app, _guild, _asker = _app(monkeypatch, targets={"200"})

    assert app.threads.resolve_thread_target(_ctx(), "#bot spam").channel_id == 200


def test_resolver_refuses_outside_a_guild(monkeypatch):
    app, _guild, _asker = _app(monkeypatch, targets={"200"})
    ctx = MessageContext(
        user_id=str(ASKER_ID),
        user_name="Alice",
        guild_id=None,
        channel_id="100",
        thread_id=None,
        trust_tier=TrustTier.MEMBER,
    )

    with pytest.raises(ValueError, match="inside a server"):
        app.threads.resolve_thread_target(ctx, "#bot-spam")


def test_resolver_refuses_when_the_asker_cannot_be_resolved(monkeypatch):
    # Without the member there is nothing to check permissions against, and the
    # whole gate is "nothing they could not do themselves". Refuse.
    app, guild, _asker = _app(monkeypatch, targets={"200"})
    guild._members.pop(ASKER_ID)

    with pytest.raises(ValueError, match="able to post"):
        app.threads.resolve_thread_target(_ctx(), "#bot-spam")


def test_resolver_uses_turn_member_when_members_intent_cache_is_empty(monkeypatch):
    """MESSAGE_CREATE carries its author Member without the Members intent."""
    app, guild, asker = _app(monkeypatch, targets={"200"})
    guild._members.pop(ASKER_ID)

    target = app.threads.resolve_thread_target(_ctx(platform_member=asker), "#bot-spam")

    assert target.channel_id == 200


def test_manage_thread_permission_uses_current_thread_overwrites(monkeypatch):
    app, guild, asker = _app(monkeypatch, targets={"200"})
    thread = _Thread(321, perms={ASKER_ID: _Perms(manage_threads=True)})
    thread.guild = guild
    guild._threads[thread.id] = thread

    assert app.threads.can_manage_thread(
        _ctx(platform_member=asker),
        thread.id,
    )


def test_manage_thread_permission_fails_closed_without_effective_permission(monkeypatch):
    app, guild, asker = _app(monkeypatch, targets={"200"})
    thread = _Thread(321)
    thread.guild = guild
    guild._threads[thread.id] = thread

    assert not app.threads.can_manage_thread(_ctx(platform_member=asker), thread.id)
    assert not app.threads.can_manage_thread(_ctx(platform_member=asker), 999)
    malformed = _ctx(platform_member=asker)
    malformed.guild_id = "not-a-discord-id"
    assert not app.threads.can_manage_thread(malformed, thread.id)


# --- creation ---


def _create(app, message, request) -> Any:
    return asyncio.run(app.threads._create_handoff_thread(message, request, 7))


def _enable(app, monkeypatch) -> Any:
    handoff = MagicMock()
    handoff.enroll = AsyncMock()
    app.thread_handoff = handoff
    monkeypatch.setattr(app.threads, "_thread_handoff_creation_allowed", lambda message: True)
    monkeypatch.setattr(app.threads, "_thread_auto_respond_default", lambda message: True)
    return handoff


def test_cross_channel_creation_posts_an_anchor_and_adds_the_asker(monkeypatch):
    target = _TextChannel(200, "bot-spam")
    app, guild, asker = _app(monkeypatch, targets={"200"}, channels=[target])
    handoff = _enable(app, monkeypatch)

    thread = _create(
        app,
        _message(guild, asker),
        ThreadRequest(name="Quest help", target_channel_id=200),
    )

    assert thread is not None
    anchor_text, allowed_mentions = target.sent[0]
    assert "Alice" in anchor_text and "<@" not in anchor_text
    assert allowed_mentions is not None
    assert target.created == [("Quest help", thread_boundary.CROSS_CHANNEL_ARCHIVE_MINUTES)]
    assert thread.added == [asker]
    handoff.enroll.assert_awaited_once_with(
        thread.id,
        7,
        creator_user_id=str(ASKER_ID),
        auto_respond=True,
    )


def test_cross_channel_creation_survives_an_add_user_failure(monkeypatch):
    target = _TextChannel(200, "bot-spam")
    app, guild, asker = _app(monkeypatch, targets={"200"}, channels=[target])
    _enable(app, monkeypatch)
    request = ThreadRequest(name="Quest help", target_channel_id=200)

    created: list[_Thread] = []
    original = _Anchor._create

    async def create(self, *, name, auto_archive_duration=None):
        thread = await original(self, name=name, auto_archive_duration=auto_archive_duration)
        thread.add_user = AsyncMock(side_effect=discord.HTTPException(MagicMock(), "nope"))
        created.append(thread)
        return thread

    monkeypatch.setattr(_Anchor, "_create", create)

    thread = _create(app, _message(guild, asker), request)

    # The pointer reply is what actually notifies; a public thread stays readable
    # either way, so this must not cost the user their answer.
    assert thread is created[0]
    assert target.deleted == []


def test_cross_channel_creation_deletes_the_anchor_when_the_thread_fails(monkeypatch):
    target = _TextChannel(200, "bot-spam")
    app, guild, asker = _app(monkeypatch, targets={"200"}, channels=[target])
    _enable(app, monkeypatch)
    monkeypatch.setattr(thread_boundary, "THREAD_HANDOFF_CREATE_ATTEMPTS", 1)

    async def fail(self, *, name, auto_archive_duration=None):
        raise discord.HTTPException(MagicMock(), "nope")

    monkeypatch.setattr(_Anchor, "_create", fail)

    thread = _create(
        app,
        _message(guild, asker),
        ThreadRequest(name="Quest help", target_channel_id=200),
    )

    # Otherwise the target channel keeps a message introducing a thread that
    # does not exist.
    assert thread is None
    assert target.deleted == [7001]


def test_cross_channel_creation_rechecks_the_gate_at_the_boundary(monkeypatch):
    # The tool resolved this a model turn ago. The boundary is what actually
    # posts, so it re-runs the same candidate filter rather than trusting the id.
    target = _TextChannel(200, "bot-spam", {ASKER_ID: _Perms(send_messages=False)})
    app, guild, asker = _app(monkeypatch, targets={"200"}, channels=[target])
    _enable(app, monkeypatch)

    thread = _create(
        app,
        _message(guild, asker),
        ThreadRequest(name="Quest help", target_channel_id=200),
    )

    assert thread is None
    assert target.sent == []


def test_same_channel_creation_is_unchanged(monkeypatch):
    app, guild, asker = _app(monkeypatch, targets={"200"})
    handoff = _enable(app, monkeypatch)
    message = _message(guild, asker)
    thread = _Thread(5555)
    message.create_thread = AsyncMock(return_value=thread)

    created = _create(app, message, ThreadRequest(name="Quest help"))

    assert created is thread
    message.create_thread.assert_awaited_once_with(name="Quest help")
    handoff.enroll.assert_awaited_once_with(
        5555,
        7,
        creator_user_id=str(ASKER_ID),
        auto_respond=True,
    )


def test_same_channel_creation_adopts_existing_thread(monkeypatch):
    app, guild, asker = _app(monkeypatch, targets={"200"})
    handoff = _enable(app, monkeypatch)
    message = _message(guild, asker)
    thread = _Thread(5555)
    message.thread = thread
    message.create_thread = AsyncMock()

    adopted = _create(app, message, ThreadRequest(name="Quest help"))

    assert adopted is thread
    message.create_thread.assert_not_awaited()
    handoff.enroll.assert_awaited_once_with(
        5555,
        7,
        creator_user_id=str(ASKER_ID),
        auto_respond=True,
    )


@pytest.mark.asyncio
async def test_same_channel_cancellation_during_enrollment_removes_created_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, guild, asker = _app(monkeypatch, targets={"200"})
    handoff = _enable(app, monkeypatch)
    message = _message(guild, asker)
    thread = _Thread(5555)
    message.create_thread = AsyncMock(return_value=thread)
    enrollment_started = asyncio.Event()
    release_enrollment = asyncio.Event()
    enrollment_cancelled = asyncio.Event()

    async def slow_enroll(*_args: Any, **_kwargs: Any) -> None:
        enrollment_started.set()
        try:
            await release_enrollment.wait()
        except asyncio.CancelledError:
            enrollment_cancelled.set()
            raise

    handoff.enroll = AsyncMock(side_effect=slow_enroll)
    handoff.leave = AsyncMock()

    creating = asyncio.create_task(
        app.threads._create_handoff_thread(message, ThreadRequest(name="Quest help"), 7)
    )
    await enrollment_started.wait()
    creating.cancel()
    await asyncio.sleep(0)
    enrollment_was_cancelled = enrollment_cancelled.is_set()
    release_enrollment.set()

    with pytest.raises(asyncio.CancelledError):
        await creating

    assert enrollment_was_cancelled is False
    handoff.leave.assert_awaited_once_with(thread.id)
    thread.delete.assert_awaited_once_with(reason="Thread handoff enrollment failed")


def test_cross_channel_enrollment_failure_deletes_owned_anchor(monkeypatch):
    target = _TextChannel(200, "bot-spam")
    app, guild, asker = _app(monkeypatch, targets={"200"}, channels=[target])
    handoff = _enable(app, monkeypatch)
    handoff.enroll = AsyncMock(side_effect=RuntimeError("database unavailable"))
    handoff.leave = AsyncMock()

    thread = _create(
        app,
        _message(guild, asker),
        ThreadRequest(name="Quest help", target_channel_id=200),
    )

    assert thread is None
    assert target.deleted == [7001]
    handoff.leave.assert_awaited_once_with(7001)


def test_adopted_thread_enrollment_failure_does_not_delete_thread(monkeypatch):
    app, guild, asker = _app(monkeypatch, targets={"200"})
    handoff = _enable(app, monkeypatch)
    handoff.enroll = AsyncMock(side_effect=RuntimeError("database unavailable"))
    handoff.leave = AsyncMock()
    message = _message(guild, asker)
    thread = _Thread(5555)
    message.thread = thread

    adopted = _create(app, message, ThreadRequest(name="Quest help"))

    assert adopted is None
    thread.delete.assert_not_awaited()
    handoff.leave.assert_not_awaited()


@pytest.mark.asyncio
async def test_same_channel_concurrent_creation_is_idempotent(monkeypatch):
    app, guild, asker = _app(monkeypatch, targets={"200"})
    _enable(app, monkeypatch)
    message = _message(guild, asker)
    message.thread = None
    thread = _Thread(5555)

    async def create_thread(*, name: str):
        assert name == "Quest help"
        message.thread = thread
        await asyncio.sleep(0)
        return thread

    message.create_thread = AsyncMock(side_effect=create_thread)
    request = ThreadRequest(name="Quest help")

    first, second = await asyncio.gather(
        app.threads._create_handoff_thread(message, request, 7),
        app.threads._create_handoff_thread(message, request, 7),
    )

    assert first is thread
    assert second is thread
    message.create_thread.assert_awaited_once_with(name="Quest help")


# --- delivery and cleanup ---


def test_pointer_replies_to_the_asker_with_the_ping_on(monkeypatch):
    app, guild, asker = _app(monkeypatch, targets={"200"})
    message = _message(guild, asker)
    thread = _Thread(5555)

    asyncio.run(app.threads._send_cross_channel_pointer(message, cast(Any, thread)))

    _args, kwargs = message.reply.await_args
    assert "<#5555>" in message.reply.await_args.args[0]
    assert kwargs["mention_author"] is True
    # add_user only puts the thread in their sidebar; this reply is the notification.
    assert kwargs["allowed_mentions"].replied_user is True
    assert kwargs["allowed_mentions"].users is False


def test_pointer_failure_is_logged_not_raised(monkeypatch):
    app, guild, asker = _app(monkeypatch, targets={"200"})
    message = _message(guild, asker)
    message.reply = AsyncMock(side_effect=discord.HTTPException(MagicMock(), "nope"))

    asyncio.run(app.threads._send_cross_channel_pointer(message, cast(Any, _Thread(5555))))


def test_discarding_a_thread_deletes_its_anchor(monkeypatch):
    target = _TextChannel(200, "bot-spam")
    app, _guild, _asker = _app(monkeypatch, targets={"200"}, channels=[target])

    asyncio.run(app.threads._discard_cross_channel_thread(cast(Any, _Thread(5555, parent=target))))

    # A thread shares the id of the message it was created from, and deleting
    # that message takes the empty thread with it.
    assert target.deleted == [5555]
