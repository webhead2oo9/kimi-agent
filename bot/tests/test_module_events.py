"""Module event bus: namespaces, bounded lanes, isolation, normalization."""

from __future__ import annotations

import asyncio
import datetime as dt
from types import SimpleNamespace
from typing import Any, cast

import pytest
from discord import RawBulkMessageDeleteEvent, RawMessageDeleteEvent

from discord_adapter.module_events import (
    ModuleEventPublisher,
    audit_entry_event,
    message_snapshot,
)
from kimi_agent_module_api import events as ev
from kimi_agent_module_api.contracts import Event, EventTopicError, ModulePermissions
from modules.events import EventBusImpl, ModuleEventView


def _view(bus: EventBusImpl, name: str, *topics: str) -> ModuleEventView:
    return ModuleEventView(bus, name, ModulePermissions(event_topics=topics))


@pytest.mark.asyncio
async def test_publish_is_namespaced_and_subscriptions_need_declarations() -> None:
    bus = EventBusImpl()
    mod = _view(bus, "community_moderation")
    img = _view(bus, "image_fingerprints", "community_moderation.*")
    seen: list[Event] = []

    async def handler(event: Event) -> None:
        seen.append(event)

    img.subscribe("community_moderation.case_created", handler)
    with pytest.raises(EventTopicError):
        img.subscribe("discord.message", handler)
    with pytest.raises(EventTopicError):
        mod.publish("image_fingerprints.x", {})
    with pytest.raises(EventTopicError):
        bus.publish_core("community_moderation.case_created", {})

    mod.publish("community_moderation.case_created", {"case_id": 7})
    await bus.drain()
    assert [(e.topic, e.payload, e.source_module) for e in seen] == [
        ("community_moderation.case_created", {"case_id": 7}, "community_moderation")
    ]
    await bus.close()


@pytest.mark.asyncio
async def test_failures_and_timeouts_are_isolated_and_counted() -> None:
    metrics: dict[str, dict[str, float]] = {}
    bus = EventBusImpl(
        handler_timeout=0.05, metrics_sink=lambda n, m: metrics.__setitem__(n, dict(m))
    )
    good = _view(bus, "good", "discord.message")
    bad = _view(bus, "bad", "discord.message")
    slow = _view(bus, "slow", "discord.*")
    got: list[str] = []

    async def ok(event: Event) -> None:
        got.append("ok")

    async def boom(event: Event) -> None:
        raise RuntimeError("boom")

    async def sleepy(event: Event) -> None:
        await asyncio.sleep(1)

    good.subscribe("discord.message", ok)
    bad.subscribe("discord.message", boom)
    slow.subscribe("discord.*", sleepy)
    bus.publish_core(ev.TOPIC_MESSAGE, {"id": 1})
    await bus.drain()
    await asyncio.sleep(0.1)
    assert got == ["ok"]
    assert metrics["good"]["events_handled"] == 1
    assert metrics["bad"]["events_failed"] == 1
    assert metrics["slow"]["events_timed_out"] == 1
    await bus.close()


@pytest.mark.asyncio
async def test_discord_events_only_reach_modules_enabled_for_their_guild() -> None:
    bus = EventBusImpl()
    enabled = ModuleEventView(
        bus,
        "enabled",
        ModulePermissions(event_topics=("discord.*",)),
        is_guild_active=lambda guild_id: guild_id == 1,
    )
    disabled = ModuleEventView(
        bus,
        "disabled",
        ModulePermissions(event_topics=("discord.*",)),
        is_guild_active=lambda _guild_id: False,
    )
    enabled_topics: list[str] = []
    disabled_topics: list[str] = []

    async def enabled_handler(event: Event) -> None:
        enabled_topics.append(event.topic)

    async def disabled_handler(event: Event) -> None:
        disabled_topics.append(event.topic)

    enabled.subscribe("discord.*", enabled_handler)
    disabled.subscribe("discord.*", disabled_handler)
    bus.publish_core(
        ev.TOPIC_MESSAGE,
        SimpleNamespace(message=SimpleNamespace(ref=SimpleNamespace(guild_id=1))),
    )
    bus.publish_core(
        ev.TOPIC_MEMBER_JOIN,
        SimpleNamespace(member=SimpleNamespace(guild_id=1)),
    )
    bus.publish_core(ev.TOPIC_AUDIT_LOG_ENTRY, SimpleNamespace(guild_id=1))
    await bus.drain()

    assert enabled_topics == [
        ev.TOPIC_MESSAGE,
        ev.TOPIC_MEMBER_JOIN,
        ev.TOPIC_AUDIT_LOG_ENTRY,
    ]
    assert disabled_topics == []
    await bus.close()


@pytest.mark.asyncio
async def test_module_owned_events_ignore_discord_guild_predicate() -> None:
    bus = EventBusImpl()
    view = ModuleEventView(
        bus,
        "mod",
        ModulePermissions(),
        is_guild_active=lambda _guild_id: False,
    )
    seen: list[Event] = []

    async def handler(event: Event) -> None:
        seen.append(event)

    view.subscribe("mod.changed", handler)
    view.publish("mod.changed", SimpleNamespace(guild_id=1))
    await bus.drain()

    assert [event.topic for event in seen] == ["mod.changed"]
    await bus.close()


@pytest.mark.asyncio
async def test_queued_discord_event_rechecks_module_guild_activation() -> None:
    bus = EventBusImpl(workers=1)
    active = True
    gated = ModuleEventView(
        bus,
        "gated",
        ModulePermissions(event_topics=("discord.*",)),
        is_guild_active=lambda _guild_id: active,
    )
    unaffected = ModuleEventView(
        bus,
        "unaffected",
        ModulePermissions(event_topics=("discord.*",)),
        is_guild_active=lambda _guild_id: True,
    )
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    gated_seen: list[int] = []
    unaffected_seen: list[int] = []

    async def gated_handler(event: Event) -> None:
        sequence = cast(int, event.payload.sequence)
        if sequence == 1:
            first_started.set()
            await release_first.wait()
        gated_seen.append(sequence)

    async def unaffected_handler(event: Event) -> None:
        unaffected_seen.append(cast(int, event.payload.sequence))

    gated.subscribe("discord.*", gated_handler)
    unaffected.subscribe("discord.*", unaffected_handler)
    bus.publish_core(ev.TOPIC_MESSAGE, SimpleNamespace(guild_id=1, sequence=1))
    await first_started.wait()

    # This event passes the enqueue-time predicate, then waits behind the first
    # handler while the module/guild becomes inactive.
    bus.publish_core(ev.TOPIC_MESSAGE, SimpleNamespace(guild_id=1, sequence=2))
    active = False
    release_first.set()
    await bus.drain()

    assert gated_seen == [1]
    assert unaffected_seen == [1, 2]
    await bus.close()


@pytest.mark.asyncio
async def test_full_lane_drops_oldest_and_close_module_cancels() -> None:
    bus = EventBusImpl(queue_size=2, workers=1)
    view = _view(bus, "m", "discord.message")
    gate = asyncio.Event()
    handled: list[Any] = []

    async def handler(event: Event) -> None:
        await gate.wait()
        handled.append(event.payload)

    view.subscribe("discord.message", handler)
    for i in range(5):
        bus.publish_core(ev.TOPIC_MESSAGE, i)
    await asyncio.sleep(0)
    assert bus.metrics_for("m")["events_dropped"] >= 2
    await bus.close_module("m")
    assert bus.subscriptions_for("m") == ()
    gate.set()
    await asyncio.sleep(0)
    assert handled == []
    await bus.close()


def _message(**overrides: Any) -> Any:
    base: dict[str, Any] = {
        "id": 3,
        "guild": SimpleNamespace(id=1),
        "channel": SimpleNamespace(id=2),
        "author": SimpleNamespace(id=4, bot=False),
        "content": "hi",
        "attachments": [
            SimpleNamespace(
                id=9, filename="a.png", url="https://cdn/a.png", size=10, content_type="image/png"
            )
        ],
        "jump_url": "https://discord.com/channels/1/2/3",
        "created_at": dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        "edited_at": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_message_snapshot_carries_ids_and_attachments_only() -> None:
    snap = message_snapshot(
        _message(
            embeds=[
                SimpleNamespace(
                    image=SimpleNamespace(proxy_url="https://p/x.png", url=None), thumbnail=None
                )
            ]
        )
    )
    assert snap.ref.guild_id == 1 and snap.ref.message_id == 3
    assert snap.embed_image_urls == ("https://p/x.png",)
    assert snap.attachments[0].filename == "a.png"
    assert snap.created_at == dt.datetime(2026, 1, 1, tzinfo=dt.UTC).timestamp()


def test_message_snapshot_carries_history_projection_fields() -> None:
    snap = message_snapshot(
        _message(
            pinned=True,
            edited_at=dt.datetime(2026, 1, 2, tzinfo=dt.UTC),
            reference=SimpleNamespace(message_id=99),
            author=SimpleNamespace(id=4, bot=False, display_name="Ada"),
            embeds=[
                SimpleNamespace(
                    title="Decision",
                    description="Ship the index",
                    fields=[SimpleNamespace(name="Owner", value="Ada")],
                    footer=SimpleNamespace(text="approved"),
                    author=SimpleNamespace(name="Review"),
                    image=None,
                    thumbnail=None,
                )
            ],
        )
    )

    assert snap.reply_to_message_id == 99
    assert snap.pinned is True
    assert snap.edited_at == dt.datetime(2026, 1, 2, tzinfo=dt.UTC).timestamp()
    assert snap.author_display_name == "Ada"
    assert all(
        expected in snap.embed_texts[0]
        for expected in ("Decision", "Ship the index", "Review", "Owner: Ada", "approved")
    )


class _Bot:
    def __init__(self) -> None:
        self.listeners: list[tuple[Any, str]] = []

    def add_listener(self, callback: Any, name: str) -> None:
        self.listeners.append((callback, name))

    def remove_listener(self, callback: Any, name: str) -> None:
        self.listeners.remove((callback, name))


@pytest.mark.asyncio
async def test_publisher_normalizes_gateway_events_and_uninstalls() -> None:
    published: list[tuple[str, Any]] = []
    bot = _Bot()
    publisher = ModuleEventPublisher(bot, lambda t, p: published.append((t, p)))  # type: ignore[arg-type]
    publisher.install()
    assert len(bot.listeners) == 9

    await publisher.on_message(_message())
    await publisher.on_message_delete(_message(content="gone"))
    before = _message(content="old")
    await publisher.on_message_edit(before, _message(content="new"))
    member_before = SimpleNamespace(
        roles=[SimpleNamespace(id=1, name="one")], timed_out_until=None, nick=None
    )
    member_after = SimpleNamespace(
        id=4,
        guild=SimpleNamespace(id=1),
        roles=[SimpleNamespace(id=1, name="one"), SimpleNamespace(id=2, name="two")],
        timed_out_until=dt.datetime(2026, 1, 2, tzinfo=dt.UTC),
        nick="n",
        display_name="n",
        bot=False,
    )
    await publisher.on_member_update(member_before, member_after)  # type: ignore[arg-type]

    topics = [t for t, _ in published]
    assert topics == [
        ev.TOPIC_MESSAGE,
        ev.TOPIC_MESSAGE_DELETE,
        ev.TOPIC_MESSAGE_EDIT,
        ev.TOPIC_MEMBER_UPDATE,
    ]
    delete = published[1][1]
    assert isinstance(delete, ev.MessageDeleteEvent) and delete.cached_content == "gone"
    edit = published[2][1]
    assert isinstance(edit, ev.MessageEditEvent) and (edit.before_content, edit.after_content) == (
        "old",
        "new",
    )
    update = published[3][1]
    assert isinstance(update, ev.MemberUpdateEvent)
    assert update.roles_added == (2,) and update.timed_out_until_after is not None
    assert update.role_names == {2: "two"}

    publisher.uninstall()
    assert bot.listeners == []


@pytest.mark.asyncio
async def test_publisher_deduplicates_cached_and_raw_single_delete() -> None:
    published: list[tuple[str, Any]] = []
    bot = _Bot()
    publisher = ModuleEventPublisher(bot, lambda t, p: published.append((t, p)))  # type: ignore[arg-type]
    cached = _message(content="gone")

    # discord.py emits the raw event first, then the cached event when the
    # message was present in its cache.
    await publisher.on_raw_message_delete(
        cast(
            RawMessageDeleteEvent,
            SimpleNamespace(
                guild_id=1,
                channel_id=2,
                message_id=3,
                cached_message=cached,
            ),
        )
    )
    await publisher.on_message_delete(cached)

    assert len(published) == 1
    topic, payload = published[0]
    assert topic == ev.TOPIC_MESSAGE_DELETE
    assert isinstance(payload, ev.MessageDeleteEvent)
    assert payload.ref.message_id == 3
    assert payload.cached_content == "gone"


@pytest.mark.asyncio
async def test_publisher_normalizes_uncached_single_and_bulk_deletes() -> None:
    published: list[tuple[str, Any]] = []
    bot = _Bot()
    publisher = ModuleEventPublisher(bot, lambda t, p: published.append((t, p)))  # type: ignore[arg-type]

    await publisher.on_raw_message_delete(
        cast(
            RawMessageDeleteEvent,
            SimpleNamespace(guild_id=1, channel_id=2, message_id=30, cached_message=None),
        )
    )
    await publisher.on_raw_bulk_message_delete(
        cast(
            RawBulkMessageDeleteEvent,
            SimpleNamespace(
                guild_id=1,
                channel_id=2,
                message_ids={31, 32},
                cached_messages=[],
            ),
        )
    )

    assert [topic for topic, _payload in published] == [ev.TOPIC_MESSAGE_DELETE] * 2
    single = published[0][1]
    bulk = published[1][1]
    assert isinstance(single, ev.MessageDeleteEvent) and single.ref.message_id == 30
    assert single.cached_content is None
    assert isinstance(bulk, ev.MessageBulkDeleteEvent)
    assert {ref.message_id for ref in bulk.refs} == {31, 32}


def test_audit_entry_maps_timeout_from_member_update() -> None:
    import discord

    entry = SimpleNamespace(
        guild=SimpleNamespace(id=1),
        id=55,
        action=discord.AuditLogAction.member_update,
        user_id=7,
        target=SimpleNamespace(id=8),
        reason="spam",
        before=SimpleNamespace(timed_out_until=None),
        after=SimpleNamespace(timed_out_until=dt.datetime(2026, 1, 2, tzinfo=dt.UTC)),
        created_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
    )
    event = audit_entry_event(entry, self_user_id=7)  # type: ignore[arg-type]
    assert event is not None
    assert event.actor_is_self is True
    assert event.action == "timeout" and event.actor_id == 7 and event.target_id == 8
    assert event.until == dt.datetime(2026, 1, 2, tzinfo=dt.UTC).timestamp()
    assert event.changes[0][0] == "timed_out_until"
