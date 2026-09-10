from __future__ import annotations

import time
from dataclasses import asdict

import pytest
import pytest_asyncio

from storage.dashboard import DashboardBusyError, DashboardStore
from storage.db import Database


@pytest_asyncio.fixture
async def store(tmp_path):
    db = Database(tmp_path / "dashboard.db")
    await db.connect()
    try:
        yield DashboardStore(db)
    finally:
        await db.close()


async def chat(store, user_id="1", guild_id="2"):
    return await store.create(
        user_id=user_id,
        guild_id=guild_id,
        channel_id="3",
        parent_channel_id="3",
        channel_name="general",
    )


@pytest.mark.asyncio
async def test_chats_are_owner_and_guild_scoped(store):
    first = await chat(store)
    await chat(store, user_id="9")
    await chat(store, guild_id="8")
    assert await store.get(first.id, user_id="9", guild_id="2") is None
    assert await store.get(first.id, user_id="1", guild_id="8") is None
    assert await store.list_chats(user_id="1", guild_id="2") == [first]
    assert "key" not in first.public()
    async with store.db.conn.execute(
        "SELECT access_scope,root_discord_message_id FROM conversations WHERE id=?",
        (first.conversation_id,),
    ) as cur:
        row = await cur.fetchone()
    assert tuple(row) == ("owner_only", None)


@pytest.mark.asyncio
async def test_duplicate_requests_do_not_repeat_input_and_busy_chats_reject(store):
    current = await chat(store)
    turn, created = await store.accept(current, request_id="request", text="Make a chart", files=[])
    assert created
    assert await store.accept(current, request_id="request", text="different", files=[]) == (
        turn,
        False,
    )
    with pytest.raises(DashboardBusyError):
        await store.accept(current, request_id="other", text="hello", files=[])
    assert len(await store.events(current.id)) == 1
    await store.finish_turn(current.id, turn, "completed", {"text": "Done"})
    await store.finish_turn(current.id, turn, "failed", {"text": "Must not replace the result"})
    assert [event.payload.get("text") for event in await store.events(current.id)] == [
        "Make a chart",
        "Done",
    ]
    assert (await store.accept(current, request_id="other", text="hello", files=[]))[1]


@pytest.mark.asyncio
async def test_journal_replay_pagination_and_late_event_after_delete(store):
    current = await chat(store)
    for i in range(5):
        await store.event(current.id, "progress", {"label": str(i)}, key=f"progress:{i}")
    await store.event(current.id, "progress", {"label": "duplicate"}, key="progress:4")
    recent = await store.events(current.id, limit=2)
    assert [e.payload["label"] for e in recent] == ["3", "4"]
    older = await store.events(current.id, before=recent[0].id)
    assert [e.payload["label"] for e in older] == ["0", "1", "2"]
    assert await store.events(current.id, after=older[-1].id) == recent
    await store.delete(current)
    await store.event(current.id, "progress", {"label": "late"})
    assert await store.events(current.id) == []


@pytest.mark.asyncio
async def test_restarts_record_interruption_without_replaying_requests(store):
    current = await chat(store)
    turn, _ = await store.accept(current, request_id="request", text="hello", files=[])
    await store.start_turn(turn)
    await store.interrupt_unfinished()
    await store.interrupt_unfinished()
    events = await store.events(current.id)
    assert len(events) == 2
    assert events[-1].payload["status"] == "interrupted"
    assert await store.accepted(current.id, "request") == turn


@pytest.mark.asyncio
async def test_privacy_and_retention_cascade_all_dashboard_records(store):
    for user in ("1", "9"):
        current = await chat(store, user_id=user)
        await store.accept(current, request_id="request", text="hello", files=[])
        file = await store.add_file(
            current,
            path="generated/secret",
            filename="a.txt",
            media_type="text/plain",
            size=2,
            kind="output",
        )
        assert "path" not in file.public()
        assert asdict(file)["path"] == "generated/secret"
    await store.conversations.delete_user_data("1")
    assert await store.list_chats(user_id="1", guild_id="2") == []
    assert len(await store.list_chats(user_id="9", guild_id="2")) == 1
    await store.conversations.delete_conversations_older_than(time.time() + 1)
    for table in (
        "dashboard_conversations",
        "dashboard_turns",
        "dashboard_events",
        "dashboard_files",
    ):
        async with store.db.conn.execute(f"SELECT COUNT(*) FROM {table}") as cur:
            assert (await cur.fetchone())[0] == 0


@pytest.mark.asyncio
async def test_sidebar_cursor_does_not_lose_chats_with_equal_timestamps(store):
    for _ in range(102):
        await chat(store)
    async with store.db.write_transaction() as conn:
        await conn.execute("UPDATE dashboard_conversations SET updated_at=100")
    first = await store.list_chats(user_id="1", guild_id="2")
    second = await store.list_chats(
        user_id="1", guild_id="2", before=first[-1].updated_at, before_id=first[-1].id
    )
    assert len(first) == 100 and len(second) == 2
    assert len({item.id for item in [*first, *second]}) == 102


@pytest.mark.asyncio
async def test_work_cards_survive_history_pagination_and_keep_latest_revision(store):
    current = await chat(store)
    await store.event(current.id, "turn_finished", {"task_preview": {"id": "task", "revision": 1}})
    await store.event(current.id, "turn_finished", {"task_preview": {"id": "task", "revision": 2}})
    await store.event(current.id, "coding_task", {"id": "code", "status": "waiting_for_input"})
    for _ in range(201):
        await store.event(current.id, "activity", {"label": "Working"})
    assert all(event.kind == "activity" for event in await store.events(current.id))
    cards = await store.work_events(current.id)
    assert len(cards) == 2
    assert cards[0].payload["task_preview"]["revision"] == 2
    assert cards[1].payload["status"] == "waiting_for_input"
