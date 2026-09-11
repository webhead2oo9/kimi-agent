import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.dashboard_files import DashboardFiles
from app.dashboard_tasks import DashboardTasks
from app.root_locks import RootLockPool
from storage.coding_tasks import CodingTaskStatus, CodingTaskStore
from storage.dashboard import DashboardStore
from storage.db import Database
from tests.test_coding_delivery import make_delivery
from tests.helpers import make_settings
from tools.workspace.common import UserLocks
from workspace import WorkspaceKey, WorkspaceManager


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_publication", [False, True])
async def test_coding_delivery_is_durable_private_and_deduplicated_without_socket(
    tmp_path, fail_publication
):
    db = Database(tmp_path / "coding.db")
    await db.connect()
    try:
        store = DashboardStore(db)
        chat = await store.create(
            user_id="1", guild_id="2", channel_id="3", parent_channel_id="3", channel_name="general"
        )
        coding = CodingTaskStore(db)
        task = await coding.create_task(
            conversation_id=chat.conversation_id,
            root_key=chat.key,
            workspace_key="1__2",
            user_id="1",
            user_name="Charlie",
            guild_id="2",
            channel_id="3",
            thread_id=None,
            trigger_discord_message_id="",
            objective="Make a report",
            acceptance_criteria=[],
            context_text="",
            max_seconds=300,
            delivery_surface="dashboard",
        )
        delivery = make_delivery(store=coding, conversation_store=store.conversations)
        delivery._publish_locked = AsyncMock(
            side_effect=AssertionError("Private task reached Discord")
        )
        with pytest.raises(RuntimeError, match="Private dashboard"):
            await delivery.publish(task, None)
        bridge = DashboardTasks(
            store=store,
            files=DashboardFiles(
                store=store,
                workspace=WorkspaceManager(tmp_path / "work"),
                locks=UserLocks(),
                settings=make_settings(),
            ),
            access=None,
            coding=SimpleNamespace(store=coding),
            delivery=delivery,
            scheduled=None,
            roots=RootLockPool(),
            privacy=None,
            operations=None,
            admission=None,
        )
        delivery.dashboard_publish = bridge.publish_coding
        await delivery.publish(task, None)
        await coding.finish(task.id, CodingTaskStatus.COMPLETED, result_text="Report is complete.")
        if fail_publication:
            async with db.write_transaction() as conn:
                await conn.execute(
                    "CREATE TRIGGER fail_coding BEFORE INSERT ON dashboard_events "
                    "WHEN NEW.kind='coding_task' AND json_extract(NEW.payload_json,'$.status')='completed' "
                    "BEGIN SELECT RAISE(ABORT, 'publication failed'); END"
                )
            with pytest.raises(Exception, match="publication failed"):
                await delivery.publish(task, None)
            assert (
                await store.conversations.load_recent_conversation_messages(chat.conversation_id)
                == []
            )
            assert (await coding.get_task(task.id)).delivery_state != "delivered"
            async with db.write_transaction() as conn:
                await conn.execute("DROP TRIGGER fail_coding")
        # No WebSocket or HTTP connection exists during delivery.
        await delivery.publish(task, None)
        await delivery.publish(task, None)
        current = await coding.get_task(task.id)
        assert current.delivery_state == "delivered"
        assert current.final_discord_message_id is None
        events = await store.events(chat.id)
        assert [event.payload["status"] for event in events] == ["queued", "completed"]
        assert events[-1].payload["text"] == "Report is complete."
        assert "checkpoint" not in str(events[-1].payload)
        async with db.conn.execute("SELECT discord_message_id, source_id FROM messages") as cursor:
            assert [tuple(row) for row in await cursor.fetchall()] == [
                (None, f"coding:{task.id}:final")
            ]
        delivery._publish_locked.assert_not_called()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_coding_progress_cannot_deadlock_a_followup_waiting_for_the_workspace(tmp_path):
    db = Database(tmp_path / "progress.db")
    await db.connect()
    try:
        store = DashboardStore(db)
        chat = await store.create(
            user_id="1", guild_id="2", channel_id="3", parent_channel_id="3", channel_name="general"
        )
        coding = CodingTaskStore(db)
        task = await coding.create_task(
            conversation_id=chat.conversation_id,
            root_key=chat.key,
            workspace_key="1__2",
            user_id="1",
            user_name="Charlie",
            guild_id="2",
            channel_id="3",
            thread_id=None,
            trigger_discord_message_id="",
            objective="Make a report",
            acceptance_criteria=[],
            context_text="",
            max_seconds=300,
            delivery_surface="dashboard",
        )
        delivery = make_delivery(store=coding, conversation_store=store.conversations)
        locks, roots = UserLocks(), RootLockPool()
        bridge = DashboardTasks(
            store=store,
            files=None,
            access=None,
            coding=SimpleNamespace(store=coding),
            delivery=delivery,
            scheduled=None,
            roots=roots,
            privacy=None,
            operations=None,
            admission=None,
        )
        delivery.dashboard_publish = bridge.publish_coding
        writer_started, followup_started = asyncio.Event(), asyncio.Event()
        key = WorkspaceKey(task.workspace_key)

        async def coding_work():
            async with locks.writer(key):
                writer_started.set()
                await followup_started.wait()
                await delivery.publish(task, None)

        async def followup():
            await writer_started.wait()
            async with roots.hold(chat.key):
                followup_started.set()
                async with locks.activity(key):
                    pass

        async with asyncio.timeout(2), asyncio.TaskGroup() as group:
            group.create_task(coding_work())
            group.create_task(followup())
        events = await store.events(chat.id)
        assert len(events) == 1 and events[0].kind == "coding_task"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_restart_resumes_only_acknowledged_dashboard_coding_handoffs(tmp_path):
    import asyncio

    from tests.test_coding_tasks import _start_service, _wait_forever

    db = Database(tmp_path / "recovery.db")
    await db.connect()
    service = None
    try:
        dashboard = DashboardStore(db)
        coding = CodingTaskStore(db)
        tasks = []
        for acknowledged in (True, False):
            chat = await dashboard.create(
                user_id="1",
                guild_id="2",
                channel_id="3",
                parent_channel_id="3",
                channel_name="general",
            )
            turn, _ = await dashboard.accept(chat, request_id="r", text="Do this work", files=[])
            task = await coding.create_task(
                conversation_id=chat.conversation_id,
                root_key=chat.key,
                workspace_key="1__2",
                user_id="1",
                user_name="Charlie",
                guild_id="2",
                channel_id="3",
                thread_id=None,
                trigger_discord_message_id="",
                objective="Make a report",
                acceptance_criteria=[],
                context_text="",
                max_seconds=300,
                delivery_surface="dashboard",
                handoff_pending=True,
            )
            if acknowledged:
                await dashboard.finish_turn(
                    chat.id, turn, "completed", {"coding_task_id": task.id, "text": "Task accepted"}
                )
            tasks.append(task)
        await db.close()
        await db.connect()
        service = _start_service(coding, tmp_path)
        service._scheduler = None
        service._delivery_retries = {}
        service._closed = False
        service._scheduler_loop = _wait_forever
        service._notify = AsyncMock()
        service._runtime.jobs = SimpleNamespace(close=AsyncMock())
        await service.start()
        if service._publishers:
            await asyncio.gather(*list(service._publishers.values()))
        approved = await coding.get_task(tasks[0].id)
        unacknowledged = await coding.get_task(tasks[1].id)
        assert approved.status == CodingTaskStatus.QUEUED
        assert not approved.handoff_pending
        assert unacknowledged.status == CodingTaskStatus.CANCELLED
        assert await coding.dashboard_handoff_acknowledged(approved.id)
        assert not await coding.dashboard_handoff_acknowledged(unacknowledged.id)
    finally:
        try:
            if service:
                await service.close()
        finally:
            await db.close()
