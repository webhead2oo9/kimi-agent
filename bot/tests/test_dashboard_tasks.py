from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web

from app.admission import TurnAdmissionController
from app.cancellation import ActiveOperationRegistry
from app.dashboard_tasks import DashboardTasks
from app.root_locks import RootLockPool
from storage.coding_tasks import CodingTaskStore
from storage.dashboard import DashboardStore
from storage.db import Database
from storage.scheduled_tasks import ScheduledTaskStore
from tests.test_scheduled_tasks import context, definition, preview_fixture
from tools.registry import TaskPreviewRequest
from utils.privacy_barrier import UserPrivacyBarrier


@pytest.mark.asyncio
async def test_dashboard_preview_and_actions_use_existing_revision_authority(tmp_path):
    db = Database(tmp_path / "actions.db")
    await db.connect()
    bridge = None
    try:
        scheduled, task_id, channel, _ = await preview_fixture(ScheduledTaskStore(db))
        store = DashboardStore(db)
        chat = await store.create(
            user_id="10",
            guild_id="100",
            channel_id="200",
            parent_channel_id="200",
            channel_name="general",
        )
        bridge = DashboardTasks(
            store=store,
            files=None,
            access=None,
            coding=None,
            delivery=None,
            scheduled=scheduled,
            roots=RootLockPool(),
            privacy=UserPrivacyBarrier(),
            operations=ActiveOperationRegistry(),
            admission=TurnAdmissionController(max_active=2, max_active_per_user=1),
        )
        bridge.context = AsyncMock(return_value=context(context_key=chat.key))
        preview = await bridge.preview(chat, TaskPreviewRequest(task_id, 1, False))
        assert preview["skill"] == definition()["skill"]
        assert preview["revision"] == 1
        assert "<t:" not in preview["text"]
        assert "https://discord.com/channels/100/300" in preview["text"]

        async def drain():
            async with asyncio.timeout(3):
                while bridge._workers:
                    await asyncio.gather(*list(bridge._workers))

        action = await bridge.submit_action(
            chat, request_id="r", task_id=task_id, action="approve", revision=1
        )
        await drain()
        assert (await scheduled.r.store.get(task_id))["approval_status"] == "approved"
        assert (
            await bridge.submit_action(
                chat, request_id="r", task_id=task_id, action="approve", revision=1
            )
            == action
        )
        await drain()
        results = [
            event for event in await store.events(chat.id) if event.kind == "task_action_result"
        ]
        assert len(results) == 1 and results[0].payload["text"] == "Task activated."
        with pytest.raises(web.HTTPNotFound):
            await bridge.submit_action(
                chat, request_id="stale", task_id=task_id, action="reject", revision=2
            )
        channel.send.assert_not_called()
        # New revisions invalidate the old card and cannot inherit its approval.
        await scheduled.r.store.draft(
            task_id=task_id,
            guild_id="100",
            owner_id="10",
            channel_id="200",
            proposer_id="10",
            definition=definition(),
            expected_revision=1,
        )
        await bridge.submit_action(
            chat, request_id="old", task_id=task_id, action="approve", revision=1
        )
        await drain()
        assert (await scheduled.r.store.get(task_id))["approval_status"] == "pending"
        assert (await bridge.states(chat))[0]["status"] == "superseded"
    finally:
        if bridge:
            await bridge.close()
        await db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("busy_user", [None, "10", "99"])
async def test_coding_stop_allows_child_finalizer_to_publish_under_root(tmp_path, busy_user):
    db = Database(tmp_path / "cancel.db")
    await db.connect()
    bridge = None
    admission = TurnAdmissionController(max_active=1, max_active_per_user=1)
    occupied = await admission.try_acquire(busy_user) if busy_user else None
    try:
        scheduled, _, _, _ = await preview_fixture(ScheduledTaskStore(db))
        scheduled.r.tools.registry = SimpleNamespace(dispatch_gate=lambda _tool, _ctx: None)
        store = DashboardStore(db)
        chat = await store.create(
            user_id="10",
            guild_id="100",
            channel_id="200",
            parent_channel_id="200",
            channel_name="general",
        )
        coding = CodingTaskStore(db)
        task = await coding.create_task(
            conversation_id=chat.conversation_id,
            root_key=chat.key,
            workspace_key="10__100",
            user_id="10",
            user_name="Charlie",
            guild_id="100",
            channel_id="200",
            thread_id=None,
            trigger_discord_message_id="",
            objective="Make a report",
            acceptance_criteria=[],
            context_text="",
            max_seconds=300,
            delivery_surface="dashboard",
        )
        roots = RootLockPool()

        async def finalize():
            async with roots.hold(chat.key):
                await store.event(
                    chat.id, "coding_task", {"task_id": task.id, "status": "cancelled"}
                )

        async def cancel(task_id, *, reason):
            assert task_id == task.id
            # Real cancellation waits for a separate worker's delivery finalizer.
            await asyncio.create_task(finalize())

        bridge = DashboardTasks(
            store=store,
            files=None,
            access=None,
            coding=SimpleNamespace(store=coding, cancel_task=cancel),
            delivery=None,
            scheduled=scheduled,
            roots=roots,
            privacy=UserPrivacyBarrier(),
            operations=ActiveOperationRegistry(),
            admission=admission,
        )
        bridge.context = AsyncMock(return_value=context(context_key=chat.key))
        async with asyncio.timeout(3):
            if occupied:
                assert occupied.lease is not None
                # Starting more work remains bounded even though stopping it is not.
                with pytest.raises(web.HTTPTooManyRequests):
                    await bridge.submit_action(
                        chat,
                        request_id="steer",
                        task_id=task.id,
                        action="steer",
                        message="Continue the report",
                    )
            await bridge.submit_action(chat, request_id="stop", task_id=task.id, action="cancel")
            while bridge._workers:
                await asyncio.gather(*list(bridge._workers))
        events = await store.events(chat.id)
        assert any(event.kind == "coding_task" for event in events)
        result = next(event for event in events if event.kind == "task_action_result")
        assert result.payload["text"] == "Stop requested. Partial file changes are kept."
        if occupied:
            # Cancellation must not release the lease held by the other turn.
            assert (await admission.try_acquire("another-user")).lease is None
    finally:
        try:
            if bridge:
                await bridge.close()
        finally:
            if occupied and occupied.lease:
                await occupied.lease.release()
            await db.close()
