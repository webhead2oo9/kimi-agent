"""Module event bus: namespaces, bounded lanes, isolation, normalization."""

from __future__ import annotations

import asyncio
import datetime as dt
from types import SimpleNamespace
from typing import Any

import pytest

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
    snap = message_snapshot(_message())
    assert snap.ref.guild_id == 1 and snap.ref.message_id == 3
    assert snap.attachments[0].filename == "a.png"
    assert snap.created_at == dt.datetime(2026, 1, 1, tzinfo=dt.UTC).timestamp()


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
    assert len(bot.listeners) == 7

    await publisher.on_message(_message())
    await publisher.on_message_delete(_message(content="gone"))
    before = _message(content="old")
    await publisher.on_message_edit(before, _message(content="new"))
    member_before = SimpleNamespace(roles=[SimpleNamespace(id=1)], timed_out_until=None, nick=None)
    member_after = SimpleNamespace(
        id=4,
        guild=SimpleNamespace(id=1),
        roles=[SimpleNamespace(id=1), SimpleNamespace(id=2)],
        timed_out_until=dt.datetime(2026, 1, 2, tzinfo=dt.UTC),
        nick="n",
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

    publisher.uninstall()
    assert bot.listeners == []


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
    event = audit_entry_event(entry)  # type: ignore[arg-type]
    assert event is not None
    assert event.action == "timeout" and event.actor_id == 7 and event.target_id == 8
    assert event.changes[0][0] == "timed_out_until"
