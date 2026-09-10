from __future__ import annotations

import time
import asyncio
import json
from dataclasses import asdict
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from storage.dashboard import DashboardBusyError, DashboardStore
from storage.dashboard_branches import DashboardBranches
from storage.conversations import ChannelMessageRecord
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


async def saved_turn(store, current, text="Question", answer="Answer", files=None):
    turn, _ = await store.accept(
        current, request_id=str(time.time_ns()), text=text, files=files or []
    )
    await store.conversations.save_channel_messages(
        current.conversation_id,
        [
            ChannelMessageRecord(
                None, "user", current.user_id, "User", text, source_id=f"dashboard:{turn}:user"
            ),
            ChannelMessageRecord(
                None, "assistant", None, None, answer, source_id=f"dashboard:{turn}:assistant"
            ),
        ],
    )
    await store.finish_turn(
        current.id,
        turn,
        "completed",
        {"text": answer, "files": files or [], "task_preview": {"id": "schedule"}},
    )
    return (await store.events(current.id))[-1]


@pytest.mark.asyncio
async def test_branch_copies_only_selected_context_without_running_work(store):
    parent = await chat(store)
    selected = await saved_turn(
        store, parent, files=[{"id": "original-file", "filename": "notes.txt"}]
    )
    await saved_turn(store, parent, "Later question", "Later answer")
    branches = DashboardBranches(store, copy_file=AsyncMock(return_value=None))
    first, retry = await asyncio.gather(
        *[branches.fork(parent, event_id=selected.id, request_id="same-request") for _ in range(2)]
    )
    assert first == retry
    assert first.parent_id == parent.id and first.parent_event_id == selected.id
    assert first.channel_id == parent.channel_id and first.user_id == parent.user_id
    history = await store.conversations.load_recent_conversation_messages(first.conversation_id)
    assert [message.content[0].text for message in history] == ["User: Question", "Answer"]
    events = await store.events(first.id)
    assert [event.kind for event in events] == ["history_message", "history_message"]
    assert events[-1].payload["files"][0] == {"filename": "notes.txt", "expired": True}
    assert all(
        "task_preview" not in event.payload and "turn_id" not in event.payload for event in events
    )
    assert await store.work_events(first.id) == []
    async with store.db.conn.execute(
        "SELECT count(*) FROM dashboard_turns WHERE dashboard_id=?", (first.id,)
    ) as cur:
        assert (await cur.fetchone())[0] == 0
    # Re-branching inherited context is supported, without inventing source messages.
    nested = await branches.fork(first, event_id=events[-1].id, request_id="nested")
    assert nested.parent_id == first.id
    assert len(await store.events(nested.id)) == 2
    await store.delete(parent)
    orphan = await store.get(first.id, user_id="1", guild_id="2")
    assert orphan.parent_id is None and orphan.parent_title is not None
    assert (
        len(await store.conversations.load_recent_conversation_messages(first.conversation_id)) == 2
    )
    await store.conversations.delete_user_data("1")
    async with store.db.conn.execute("SELECT count(*) FROM dashboard_branches") as cur:
        assert (await cur.fetchone())[0] == 0


@pytest.mark.asyncio
async def test_branch_return_adds_selected_result_as_context_once(store):
    parent = await chat(store)
    selected = await saved_turn(store, parent)
    branches = DashboardBranches(store)
    branch = await branches.fork(parent, event_id=selected.id, request_id="fork")
    answer = await saved_turn(store, branch, "Try another way", "Alternative result")
    await saved_turn(store, branch, "More", "Do not bring this back")
    await branches.return_result(branch, parent, event_id=answer.id)
    await branches.return_result(branch, parent, event_id=answer.id)
    returned = [event for event in await store.events(parent.id) if event.kind == "branch_result"]
    assert len(returned) == 1 and returned[0].payload["text"] == "Alternative result"
    assert returned[0].payload["source_event_id"] == answer.id
    branch_events = await store.events(branch.id)
    assert (
        next(event for event in branch_events if event.id == answer.id).payload[
            "returned_to_parent"
        ]
        is True
    )
    assert len([event for event in branch_events if event.kind == "branch_returned"]) == 1
    # Older returned results also show their durable state without a new journal notice.
    async with store.db.write_transaction() as conn:
        await conn.execute(
            "DELETE FROM dashboard_events WHERE dashboard_id=? AND kind='branch_returned'",
            (branch.id,),
        )
    assert (
        next(event for event in await store.events(branch.id) if event.id == answer.id).payload[
            "returned_to_parent"
        ]
        is True
    )
    history = await store.conversations.load_recent_conversation_messages(parent.conversation_id)
    assert history[-1].role == "user"
    assert "Alternative result" in history[-1].content[0].text
    assert "Do not bring this back" not in history[-1].content[0].text
    nested = await branches.fork(parent, event_id=returned[0].id, request_id="returned-context")
    copied = (await store.events(nested.id))[-1].payload
    assert copied["render_markdown"] is True
    assert copied["source_chat_id"] == branch.id
    assert copied["text"] == "Alternative result"
    await store.accept(parent, request_id="busy", text="Keep working", files=[])
    with pytest.raises(DashboardBusyError, match="finish"):
        await branches.return_result(
            branch, parent, event_id=(await store.events(branch.id))[-1].id
        )


@pytest.mark.asyncio
async def test_branch_rejects_foreign_unpersisted_and_reused_events(store):
    parent, other = await chat(store), await chat(store, user_id="9")
    own_event = await saved_turn(store, parent)
    foreign = await saved_turn(store, other)
    branches = DashboardBranches(store)
    with pytest.raises(LookupError):
        await branches.fork(parent, event_id=foreign.id, request_id="foreign")
    await branches.fork(parent, event_id=own_event.id, request_id="retry")
    with pytest.raises(DashboardBusyError, match="another message"):
        await branches.fork(parent, event_id=own_event.id - 1, request_id="retry")
    await store.accept(parent, request_id="unfinished", text="Still being saved", files=[])
    with pytest.raises(DashboardBusyError, match="saved conversation"):
        await branches.fork(
            parent, event_id=(await store.events(parent.id))[-1].id, request_id="unfinished"
        )


@pytest.mark.asyncio
async def test_dashboard_branch_migration_preserves_existing_chats(tmp_path):
    path = tmp_path / "upgrade.db"
    db = Database(path)
    await db.connect()
    store = DashboardStore(db)
    parent = await chat(store)
    selected = await saved_turn(store, parent)
    async with db.write_transaction() as conn:
        await conn.execute("DROP TABLE dashboard_branches")
        await conn.execute("DELETE FROM schema_version WHERE version=15")
    await db.close()
    await db.connect()
    try:
        restored = await store.get(parent.id, user_id="1", guild_id="2")
        branch = await DashboardBranches(store).fork(
            restored, event_id=selected.id, request_id="after-upgrade"
        )
        assert branch.parent_id == parent.id
        assert len(await store.events(branch.id)) == 2
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_branch_metadata_ignores_coding_progress_and_keeps_timed_out_results(store):
    parent = await chat(store)
    await saved_turn(store, parent, files=[{"filename": "early.txt", "unavailable": True}])
    async with store.db.write_transaction() as conn:
        await conn.executemany(
            "INSERT INTO dashboard_events(dashboard_id,kind,payload_json,created_at) VALUES(?,'coding_task',?,?)",
            [
                (
                    parent.id,
                    json.dumps({"id": "coding", "status": "running", "text": str(i)}),
                    time.time(),
                )
                for i in range(1001)
            ],
        )
    await store.conversations.save_channel_messages(
        parent.conversation_id,
        [
            ChannelMessageRecord(
                None, "assistant", None, None, "Partial result", source_id="coding:coding:final"
            )
        ],
    )
    await store.event(
        parent.id,
        "coding_task",
        {
            "id": "coding",
            "status": "timed_out",
            "text": "Partial result",
            "files": [{"filename": "partial.txt", "unavailable": True}],
        },
    )
    selected = (await store.events(parent.id))[-1]
    branch = await DashboardBranches(store).fork(
        parent, event_id=selected.id, request_id="with-progress"
    )
    history = await store.events(branch.id)
    assert len(history) == 3
    assert history[0].payload["files"][0]["filename"] == "early.txt"
    assert history[-1].payload["files"][0]["filename"] == "partial.txt"
