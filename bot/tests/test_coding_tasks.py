"""Exercises app/coding_jobs.py and app/coding_tasks.py: the background
coding-job queue, per-workspace write admission, and handoff/claim
lifecycle backed by storage/coding_tasks.py.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from app.cancellation import ActiveOperationRegistry
from app import coding_jobs as coding_jobs_module
from app.coding_jobs import CodingJobManager
from app.coding_tasks import CodingTaskService
from sandbox.runner import SandboxConfig, SandboxResult, SandboxTeardownError
from storage.coding_tasks import (
    CodingJobStatus,
    CodingTask,
    CodingTaskQueueFull,
    CodingTaskStatus,
    CodingTaskStore,
)
from storage.db import Database, SCHEMA_VERSION
from storage.usage import UsageStore
from tools.code_exec import CodeExecRuntimeGuards
from tools.coding_tasks import (
    CODING_CONTROL_TOOLS,
    CODING_WORKSPACE_TOOLS,
    CodingTaskControls,
    build_coding_registry,
    init_coding_control_tools,
)
from tools.registry import MessageContext, ToolRegistry
from tools.workspace.common import UserLocks
from trust.tiers import TrustTier
from workspace import WorkspaceManager


_requires_dir_fd = pytest.mark.skipif(
    os.name != "posix" or not os.supports_dir_fd,
    reason="quota cleanup requires POSIX dir_fd operations",
)


async def _create(
    store: CodingTaskStore,
    *,
    user_id: str = "u1",
    workspace_key: str = "u1__g1",
    root_key: str = "root-1",
    handoff_pending: bool = False,
):
    return await store.create_task(
        conversation_id=None,
        root_key=root_key,
        workspace_key=workspace_key,
        user_id=user_id,
        user_name="User",
        guild_id="g1",
        channel_id="c1",
        thread_id=None,
        handoff_pending=handoff_pending,
        trigger_discord_message_id="m1",
        objective="Fix the project",
        acceptance_criteria=["Tests pass"],
        context_text="",
        max_seconds=3600,
    )


def _steering_service(
    store: CodingTaskStore,
    *,
    max_queued_per_user: int,
    max_queued_per_workspace: int,
) -> CodingTaskService:
    service = object.__new__(CodingTaskService)
    service._store = store
    service._wake = asyncio.Event()
    service._runtime = cast(
        Any,
        SimpleNamespace(
            settings=SimpleNamespace(
                coding_task_max_queued_per_user=max_queued_per_user,
                coding_task_max_queued_per_workspace=max_queued_per_workspace,
            )
        ),
    )
    return service


def _control_context(*, user_id: str = "u1") -> MessageContext:
    return MessageContext(
        user_id=user_id,
        user_name="User",
        guild_id="g1",
        channel_id="c1",
        thread_id=None,
        trust_tier=TrustTier.MEMBER,
        context_key="root-1",
    )


def _coding_job_manager(
    db: Database,
    store: CodingTaskStore,
    workspace_manager: WorkspaceManager,
    *,
    sandbox_config: SandboxConfig | None = None,
) -> CodingJobManager:
    return CodingJobManager(
        store=store,
        workspace_manager=workspace_manager,
        workspace_locks=UserLocks(),
        sandbox_config=sandbox_config or SandboxConfig(),
        max_seconds=60,
        max_cpu_seconds=10,
        runtime_guards=CodeExecRuntimeGuards.create(
            max_concurrency=1,
            network_weekly_limit=0,
        ),
        usage_store=UsageStore(db),
    )


async def _run_coding_job(
    manager: CodingJobManager,
    task: CodingTask,
    *,
    request: dict[str, Any] | None = None,
) -> str:
    async with manager.workspace_activity(task.workspace_key):
        job_id = await manager.start(
            task_id=task.id,
            workspace_key=task.workspace_key,
            request=request or {"path": "test.sh", "mode": "shell"},
        )
        await manager.wait(job_id, timeout=1)
    return job_id


@pytest.mark.asyncio
async def test_coding_tables_are_flattened_into_schema_v1(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        assert SCHEMA_VERSION == 1
        async with db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'coding_%'"
        ) as cursor:
            names = {str(row[0]) for row in await cursor.fetchall()}
        assert names == {"coding_tasks", "coding_task_events", "coding_command_jobs"}
        async with db.conn.execute("PRAGMA table_info(coding_tasks)") as cursor:
            columns = {str(row[1]) for row in await cursor.fetchall()}
        assert "handoff_pending" in columns
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_queue_claims_one_writer_per_workspace_fifo(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        store = CodingTaskStore(db)
        first = await _create(store)
        second = await _create(store, root_key="root-2")
        other = await _create(store, user_id="u2", workspace_key="u2__g1", root_key="root-3")

        claimed_first = await store.claim_next()
        claimed_other = await store.claim_next()
        blocked = await store.claim_next()

        assert claimed_first is not None and claimed_first.id == first.id
        assert claimed_other is not None and claimed_other.id == other.id
        assert blocked is None

        await store.finish(first.id, CodingTaskStatus.COMPLETED, result_text="done")
        claimed_second = await store.claim_next()
        assert claimed_second is not None and claimed_second.id == second.id
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_handoff_task_is_held_until_target_is_bound_and_released(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        store = CodingTaskStore(db)
        task = await _create(store, handoff_pending=True)
        await _create(store, root_key="root-2")

        assert task.handoff_pending is True
        assert await store.queued_counts(
            user_id=task.user_id, workspace_key=task.workspace_key
        ) == (2, 2)
        assert await store.claim_next() is None

        assert await store.bind_handoff_target(task.id, channel_id="c2", thread_id="t2")
        assert await store.claim_next() is None
        assert await store.release_handoff(task.id)

        claimed = await store.claim_next()
        assert claimed is not None
        assert claimed.id == task.id
        assert claimed.channel_id == "c2"
        assert claimed.thread_id == "t2"
        assert claimed.handoff_pending is False
        assert [event.kind for event in await store.events(task.id)][-2:] == [
            "handoff_released",
            "started",
        ]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_abandoned_handoff_cannot_be_claimed_or_recovered(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        store = CodingTaskStore(db)
        task = await _create(store, handoff_pending=True)

        assert await store.abandon_handoff(task.id, reason="foreground timed out")
        assert await store.claim_next() is None
        assert await store.recover_interrupted() == []

        abandoned = await store.get_task(task.id)
        assert abandoned is not None
        assert abandoned.status == CodingTaskStatus.CANCELLED
        assert abandoned.cancel_requested is True
        assert abandoned.handoff_pending is False
        assert abandoned.delivery_state == "delivered"
        assert (await store.events(task.id))[-1].kind == "handoff_abandoned"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_service_publishes_bound_status_after_prepare_and_before_release(
    tmp_path,
) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        store = CodingTaskStore(db)
        task = await _create(store, handoff_pending=True)
        observed: list[tuple[str, bool, str, str | None]] = []
        service = object.__new__(CodingTaskService)
        service._store = store
        service._wake = asyncio.Event()

        async def notify(bound_task, _context=None):
            observed.append(
                (
                    "notify",
                    bound_task.handoff_pending,
                    bound_task.channel_id,
                    bound_task.thread_id,
                )
            )

        cast(Any, service)._notify = notify

        assert await service.prepare_handoff(
            task.id,
            channel_id="parent-2",
            thread_id="thread-2",
        )
        assert observed == []

        bound = await store.get_task(task.id)
        assert bound is not None and bound.handoff_pending is True
        assert bound.channel_id == "parent-2"
        assert bound.thread_id == "thread-2"

        assert await service.release_handoff(task.id)

        assert observed == [("notify", True, "parent-2", "thread-2")]
        released = await store.get_task(task.id)
        assert released is not None and released.handoff_pending is False
        assert service._wake.is_set()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_service_abandons_commit_when_foreground_already_finalized(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        store = CodingTaskStore(db)
        service = object.__new__(CodingTaskService)
        service._store = store
        service._runtime = cast(
            Any,
            SimpleNamespace(
                settings=SimpleNamespace(
                    coding_task_max_seconds=60,
                    coding_task_max_queued_per_user=1,
                    coding_task_max_queued_per_workspace=1,
                )
            ),
        )
        ctx = MessageContext(
            user_id="u1",
            user_name="User",
            guild_id="g1",
            channel_id="c1",
            thread_id=None,
            trust_tier=TrustTier.MEMBER,
            context_key="root-1",
        )
        ctx.begin_turn_finalization()

        result = await service.start_from_tool(
            ctx,
            objective="Fix it",
            acceptance_criteria=[],
            context_text="",
        )

        assert result["accepted"] is False
        async with db.conn.execute("SELECT * FROM coding_tasks") as cursor:
            rows = list(await cursor.fetchall())
        assert len(rows) == 1
        task = await store.get_task(str(rows[0]["id"]))
        assert task is not None
        assert task.status == CodingTaskStatus.CANCELLED
        assert task.delivery_state == "delivered"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_queue_admission_limit_is_checked_in_the_insert_transaction(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        store = CodingTaskStore(db)
        await store.create_task(
            conversation_id=None,
            root_key="r1",
            workspace_key="u1__g1",
            user_id="u1",
            user_name="User",
            guild_id="g1",
            channel_id="c1",
            thread_id=None,
            trigger_discord_message_id="m1",
            objective="first",
            acceptance_criteria=[],
            context_text="",
            max_seconds=60,
            max_queued_per_user=1,
            max_queued_per_workspace=1,
        )
        with pytest.raises(CodingTaskQueueFull, match="user queue"):
            await store.create_task(
                conversation_id=None,
                root_key="r2",
                workspace_key="u1__g1",
                user_id="u1",
                user_name="User",
                guild_id="g1",
                channel_id="c1",
                thread_id=None,
                trigger_discord_message_id="m2",
                objective="second",
                acceptance_criteria=[],
                context_text="",
                max_seconds=60,
                max_queued_per_user=1,
                max_queued_per_workspace=1,
            )
    finally:
        await db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scope", "queued_user", "queued_workspace", "expected_error"),
    [
        ("user", "u1", "u1__g2", "Your coding-task queue is full."),
        ("workspace", "u2", "u1__g1", "This workspace's coding queue is full."),
    ],
)
async def test_paused_resume_rejects_full_queue_without_storing_steering(
    tmp_path,
    scope: str,
    queued_user: str,
    queued_workspace: str,
    expected_error: str,
) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        store = CodingTaskStore(db)
        paused = await _create(store)
        await store.set_status(paused.id, CodingTaskStatus.WAITING_FOR_INPUT)
        await _create(
            store,
            user_id=queued_user,
            workspace_key=queued_workspace,
            root_key="queued-root",
        )
        service = _steering_service(
            store,
            max_queued_per_user=1 if scope == "user" else 10,
            max_queued_per_workspace=1,
        )

        result = await service.steer_from_tool(
            _control_context(), task_id=paused.id, message="please continue"
        )

        assert result == {
            "task_id": paused.id,
            "accepted": False,
            "error": expected_error,
            "status": CodingTaskStatus.WAITING_FOR_INPUT.value,
        }
        refreshed = await store.get_task(paused.id)
        assert refreshed is not None
        assert refreshed.status == CodingTaskStatus.WAITING_FOR_INPUT
        assert [event.kind for event in await store.events(paused.id)].count("steering") == 0
        assert not service._wake.is_set()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_two_paused_resumes_racing_for_one_user_slot_admit_exactly_one(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        store = CodingTaskStore(db)
        first = await _create(store, workspace_key="u1__g1", root_key="first")
        second = await _create(store, workspace_key="u1__g2", root_key="second")
        await store.set_status(first.id, CodingTaskStatus.WAITING_FOR_INPUT)
        await store.set_status(second.id, CodingTaskStatus.WAITING_FOR_INPUT)

        results = await asyncio.gather(
            store.steer_active_task(
                first.id,
                "first steering",
                max_queued_per_user=1,
                max_queued_per_workspace=1,
            ),
            store.steer_active_task(
                second.id,
                "second steering",
                max_queued_per_user=1,
                max_queued_per_workspace=1,
            ),
            return_exceptions=True,
        )

        accepted = [result for result in results if not isinstance(result, BaseException)]
        rejected = [result for result in results if isinstance(result, CodingTaskQueueFull)]
        assert len(accepted) == 1
        assert accepted[0] is not None
        assert accepted[0].status == CodingTaskStatus.QUEUED
        assert len(rejected) == 1 and rejected[0].scope == "user"
        refreshed = [await store.get_task(first.id), await store.get_task(second.id)]
        assert {task.status for task in refreshed if task is not None} == {
            CodingTaskStatus.QUEUED,
            CodingTaskStatus.WAITING_FOR_INPUT,
        }
        events = [*(await store.events(first.id)), *(await store.events(second.id))]
        assert [event.kind for event in events].count("steering") == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_running_task_steering_ignores_queued_caps(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        store = CodingTaskStore(db)
        task = await _create(store)
        await store.set_status(task.id, CodingTaskStatus.RUNNING)

        refreshed = await store.steer_active_task(
            task.id,
            "keep going",
            max_queued_per_user=0,
            max_queued_per_workspace=0,
        )

        assert refreshed is not None and refreshed.status == CodingTaskStatus.RUNNING
        steering = [event for event in await store.events(task.id) if event.kind == "steering"]
        assert [event.payload for event in steering] == [{"message": "keep going"}]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_checkpoint_journal_stores_metadata_not_full_transcript(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        store = CodingTaskStore(db)
        task = await _create(store)
        await store.set_checkpoint(
            task.id,
            {
                "messages": [{"role": "user", "content": "large transcript"}],
                "event_cursor": 7,
            },
        )

        event = (await store.events(task.id))[-1]
        assert event.kind == "checkpoint"
        assert event.payload == {"message_count": 1, "event_cursor": 7}
        refreshed = await store.get_task(task.id)
        assert refreshed is not None
        assert refreshed.checkpoint["messages"][0]["content"] == "large transcript"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_coding_task_keeps_caller_tier_and_current_tool_policy(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        store = CodingTaskStore(db)
        task = await store.create_task(
            conversation_id=None,
            root_key="root-1",
            workspace_key="u1__g1",
            user_id="u1",
            user_name="User",
            guild_id="g1",
            channel_id="c1",
            thread_id=None,
            trigger_discord_message_id="m1",
            objective="Fix the project",
            acceptance_criteria=["Tests pass"],
            context_text="",
            max_seconds=3600,
            initial_checkpoint={"trust_tier": TrustTier.REGULAR.value},
        )

        context = CodingTaskService._context_from_checkpoint(
            task,
            blocked_tools=frozenset({"delete_file"}),
            tool_configs={"write_file": {"max_bytes": 1024}},
        )

        assert CodingTaskService._trust_tier_from_checkpoint(task) is TrustTier.REGULAR
        assert context.blocked_tools == frozenset({"delete_file"})
        assert context.tool_configs == {"write_file": {"max_bytes": 1024}}
        assert "resuming after an interruption" not in CodingTaskService._task_prompt(task)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_legacy_coding_task_defaults_to_member_tier(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        task = await _create(CodingTaskStore(db))
        assert CodingTaskService._trust_tier_from_checkpoint(task) is TrustTier.MEMBER
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_cancel_queued_task_is_terminal_and_preserves_record(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        store = CodingTaskStore(db)
        task = await _create(store)
        assert await store.request_cancel(task.id, reason="stop") is True
        cancelled = await store.get_task(task.id)
        assert cancelled is not None
        assert cancelled.status == CodingTaskStatus.CANCELLED
        assert cancelled.cancel_requested is True
        assert cancelled.objective == "Fix the project"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_cancel_waiting_for_input_task_is_terminal(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        store = CodingTaskStore(db)
        task = await _create(store)
        await store.set_status(task.id, CodingTaskStatus.WAITING_FOR_INPUT)

        assert await store.request_cancel(task.id, reason="stop") is True

        cancelled = await store.get_task(task.id)
        assert cancelled is not None
        assert cancelled.status == CodingTaskStatus.CANCELLED
        assert cancelled.cancel_requested is True
        assert cancelled.finished_at is not None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_restart_preserves_waiting_for_input_until_user_steers(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        store = CodingTaskStore(db)
        task = await _create(store)
        await store.set_status(task.id, CodingTaskStatus.WAITING_FOR_INPUT)

        recovered = await store.recover_interrupted()

        assert [item.id for item in recovered] == [task.id]
        refreshed = await store.get_task(task.id)
        assert refreshed is not None
        assert refreshed.status == CodingTaskStatus.WAITING_FOR_INPUT
        assert await store.claim_next() is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_waiting_for_input_expires_at_total_deadline(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        store = CodingTaskStore(db)
        task = await _create(store)
        await store.set_status(task.id, CodingTaskStatus.WAITING_FOR_INPUT)
        async with db.write_transaction() as conn:
            await conn.execute("UPDATE coding_tasks SET deadline_at = 0 WHERE id = ?", (task.id,))

        expired = await store.expire_waiting_for_input()

        assert [item.id for item in expired] == [task.id]
        assert expired[0].status == CodingTaskStatus.TIMED_OUT
        assert expired[0].delivery_state == "final_pending"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_terminal_delivery_failure_uses_durable_backoff(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        store = CodingTaskStore(db)
        task = await _create(store)
        await store.finish(task.id, CodingTaskStatus.COMPLETED, result_text="done")
        finished = await store.get_task(task.id)
        assert finished is not None and finished.finished_at is not None
        attempted_at = finished.finished_at + 1

        failed = await store.record_delivery_failure(
            task.id,
            "temporary Discord failure",
            now=attempted_at,
        )

        assert failed is not None
        assert failed.delivery_state == "final_pending"
        retry = failed.checkpoint["delivery_retry"]
        assert retry["attempts"] == 1
        assert retry["next_attempt_at"] == attempted_at + 10
        assert await store.list_pending_delivery(now=attempted_at + 9) == []
        assert [item.id for item in await store.list_pending_delivery(now=attempted_at + 10)] == [
            task.id
        ]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_permanent_delivery_failure_requires_manual_reset(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        store = CodingTaskStore(db)
        task = await _create(store)
        await store.finish(task.id, CodingTaskStatus.COMPLETED, result_text="done")

        failed = await store.record_delivery_failure(
            task.id,
            "Discord channel is unavailable",
            permanent=True,
        )

        assert failed is not None and failed.delivery_state == "failed"
        assert await store.list_pending_delivery() == []
        assert await store.reset_delivery_retry(task.id) is True
        reset = await store.get_task(task.id)
        assert reset is not None and reset.delivery_state == "final_pending"
        assert "delivery_retry" not in reset.checkpoint
        assert [item.id for item in await store.list_pending_delivery()] == [task.id]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_delivery_retries_stop_after_ten_attempts(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        store = CodingTaskStore(db)
        task = await _create(store)
        await store.finish(task.id, CodingTaskStatus.COMPLETED, result_text="done")
        finished = await store.get_task(task.id)
        assert finished is not None and finished.finished_at is not None

        refreshed = finished
        for attempt in range(10):
            updated = await store.record_delivery_failure(
                task.id,
                "temporary Discord failure",
                now=finished.finished_at + attempt + 1,
            )
            assert updated is not None
            refreshed = updated

        assert refreshed.delivery_state == "failed"
        assert refreshed.checkpoint["delivery_retry"]["attempts"] == 10
        assert refreshed.checkpoint["delivery_retry"]["exhausted"] is True
        assert await store.list_pending_delivery(now=finished.finished_at + 100_000) == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_terminal_notify_records_an_incomplete_delivery_attempt(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        store = CodingTaskStore(db)
        task = await _create(store)
        await store.finish(task.id, CodingTaskStatus.COMPLETED, result_text="done")
        terminal = await store.get_task(task.id)
        assert terminal is not None

        @contextlib.asynccontextmanager
        async def user_activity(_user_id: str):
            yield

        service = object.__new__(CodingTaskService)
        service._store = store
        service._runtime = cast(
            Any,
            SimpleNamespace(
                settings=SimpleNamespace(coding_status_min_interval_seconds=0),
                user_activity=user_activity,
                notifier=AsyncMock(),
            ),
        )
        service._last_published = {}
        service._publishers = {}

        await service._notify(terminal)

        refreshed = await store.get_task(task.id)
        assert refreshed is not None
        assert refreshed.checkpoint["delivery_retry"]["attempts"] == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_cancel_wins_over_stale_waiting_input_resume(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        store = CodingTaskStore(db)
        task = await _create(store)
        await store.set_status(task.id, CodingTaskStatus.WAITING_FOR_INPUT)
        await store.request_cancel(task.id, reason="stop")

        resumed = await store.steer_active_task(
            task.id,
            "stale steering",
            max_queued_per_user=1,
            max_queued_per_workspace=1,
        )

        assert resumed is None
        refreshed = await store.get_task(task.id)
        assert refreshed is not None and refreshed.status == CodingTaskStatus.CANCELLED
        assert [event.kind for event in await store.events(task.id)].count("steering") == 0
        assert await store.claim_next() is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_restart_requeues_task_and_interrupts_uncertain_job(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        store = CodingTaskStore(db)
        task = await _create(store)
        claimed = await store.claim_next()
        assert claimed is not None
        job = await store.create_job(task.id, {"path": "test.sh"})
        await store.update_job(job.id, CodingJobStatus.RUNNING)

        recovered = await store.recover_interrupted()

        assert [item.id for item in recovered] == [task.id]
        refreshed = await store.get_task(task.id)
        refreshed_job = await store.get_job(job.id)
        assert refreshed is not None and refreshed.status == CodingTaskStatus.QUEUED
        assert refreshed_job is not None
        assert refreshed_job.status == CodingJobStatus.INTERRUPTED
        events = await store.events(task.id)
        assert events[-1].kind == "recovered"
        assert "inspect the workspace" in str(events[-1].payload["message"])
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_recovery_stops_the_exact_persisted_systemd_unit(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        store = CodingTaskStore(db)
        task = await _create(store)
        job = await store.create_job(task.id, {"path": "test.sh"})
        await store.update_job(
            job.id,
            CodingJobStatus.RUNNING,
            unit_name=f"coding-job-{job.id}.scope",
        )
        stopped: list[str] = []

        async def stop(unit_name: str) -> None:
            stopped.append(unit_name)

        monkeypatch.setattr("app.coding_jobs.stop_sandbox_unit", stop)
        manager = CodingJobManager(
            store=store,
            workspace_manager=WorkspaceManager(tmp_path / "workspaces"),
            workspace_locks=UserLocks(),
            sandbox_config=SandboxConfig(),
            max_seconds=60,
            max_cpu_seconds=10,
            runtime_guards=CodeExecRuntimeGuards.create(
                max_concurrency=1,
                network_weekly_limit=0,
            ),
            usage_store=UsageStore(db),
        )

        await manager.stop_recovered_units()

        assert stopped == [f"coding-job-{job.id}.scope"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_recovery_attempts_every_persisted_unit_before_failing(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        store = CodingTaskStore(db)
        first = await _create(store, root_key="r1")
        second = await _create(store, user_id="u2", workspace_key="u2__g1", root_key="r2")
        first_job = await store.create_job(first.id, {"path": "first.sh"})
        second_job = await store.create_job(second.id, {"path": "second.sh"})
        await store.update_job(first_job.id, CodingJobStatus.RUNNING, unit_name="first.scope")
        await store.update_job(second_job.id, CodingJobStatus.RUNNING, unit_name="second.scope")
        attempts: list[str] = []

        async def stop(unit_name: str) -> None:
            attempts.append(unit_name)
            if unit_name == "first.scope":
                raise RuntimeError("uncertain")

        monkeypatch.setattr(coding_jobs_module, "stop_sandbox_unit", stop)
        manager = CodingJobManager(
            store=store,
            workspace_manager=WorkspaceManager(tmp_path / "workspaces"),
            workspace_locks=UserLocks(),
            sandbox_config=SandboxConfig(),
            max_seconds=60,
            max_cpu_seconds=10,
            runtime_guards=CodeExecRuntimeGuards.create(
                max_concurrency=1,
                network_weekly_limit=0,
            ),
            usage_store=UsageStore(db),
        )

        with pytest.raises(RuntimeError, match="Could not confirm"):
            await manager.stop_recovered_units()

        assert attempts == ["first.scope", "second.scope"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_recovery_attempts_later_units_before_propagating_cancellation(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        store = CodingTaskStore(db)
        first = await _create(store, root_key="r1")
        second = await _create(store, user_id="u2", workspace_key="u2__g1", root_key="r2")
        first_job = await store.create_job(first.id, {"path": "first.sh"})
        second_job = await store.create_job(second.id, {"path": "second.sh"})
        await store.update_job(first_job.id, CodingJobStatus.RUNNING, unit_name="first.scope")
        await store.update_job(second_job.id, CodingJobStatus.RUNNING, unit_name="second.scope")
        attempts: list[str] = []

        async def stop(unit_name: str) -> None:
            attempts.append(unit_name)
            if unit_name == "first.scope":
                raise asyncio.CancelledError

        monkeypatch.setattr(coding_jobs_module, "stop_sandbox_unit", stop)
        manager = CodingJobManager(
            store=store,
            workspace_manager=WorkspaceManager(tmp_path / "workspaces"),
            workspace_locks=UserLocks(),
            sandbox_config=SandboxConfig(),
            max_seconds=60,
            max_cpu_seconds=10,
            runtime_guards=CodeExecRuntimeGuards.create(
                max_concurrency=1,
                network_weekly_limit=0,
            ),
            usage_store=UsageStore(db),
        )

        with pytest.raises(asyncio.CancelledError):
            await manager.stop_recovered_units()

        assert attempts == ["first.scope", "second.scope"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_cancel_serializes_with_job_admission(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        store = CodingTaskStore(db)
        task = await _create(store)
        workspace_locks = UserLocks()
        manager = CodingJobManager(
            store=store,
            workspace_manager=WorkspaceManager(tmp_path / "workspaces"),
            workspace_locks=workspace_locks,
            sandbox_config=SandboxConfig(),
            max_seconds=60,
            max_cpu_seconds=10,
            runtime_guards=CodeExecRuntimeGuards.create(
                max_concurrency=1,
                network_weekly_limit=0,
            ),
            usage_store=UsageStore(db),
        )
        admission_entered = asyncio.Event()
        allow_admission = asyncio.Event()
        original = store.create_job_if_active

        async def delayed_admission(task_id: str, request: dict[str, Any]):
            admission_entered.set()
            await allow_admission.wait()
            return await original(task_id, request)

        monkeypatch.setattr(store, "create_job_if_active", delayed_admission)
        async with manager.workspace_activity(task.workspace_key):
            starting = asyncio.create_task(
                manager.start(
                    task_id=task.id,
                    workspace_key=task.workspace_key,
                    request={"path": "test.sh", "mode": "shell"},
                )
            )
            await admission_entered.wait()
            await store.request_cancel(task.id, reason="stop")
            cancelling = asyncio.create_task(manager.cancel_task(task.id))
            allow_admission.set()

            with pytest.raises(RuntimeError, match="no longer active"):
                await starting
            await cancelling

        assert await store.list_active_jobs(task_id=task.id) == []
    finally:
        await db.close()


@_requires_dir_fd
@pytest.mark.asyncio
async def test_durable_quota_cleanup_removes_new_paths_and_preserves_existing_changes(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        store = CodingTaskStore(db)
        task = await _create(store)
        workspace_manager = WorkspaceManager(tmp_path / "workspaces")
        root = workspace_manager.user_files_dir(task.workspace_key)
        script = root / "test.sh"
        script.write_text("exit 0", encoding="utf-8")
        existing = root / "keep.txt"
        existing.write_text("before", encoding="utf-8")

        async def quota_run(*args, **kwargs) -> SandboxResult:
            del args, kwargs
            existing.write_text("modified", encoding="utf-8")
            (root / "oversize.bin").write_bytes(b"x" * 1024)
            return SandboxResult(
                exit_code=1,
                stdout="",
                stderr="Workspace quota exceeded.",
                timed_out=False,
                duration_ms=1,
                quota_exceeded=True,
            )

        monkeypatch.setattr(coding_jobs_module, "run_workspace_file_in_sandbox", quota_run)
        manager = _coding_job_manager(db, store, workspace_manager)

        job_id = await _run_coding_job(manager, task)

        job = await store.get_job(job_id)
        assert job is not None and job.status == CodingJobStatus.FAILED
        assert not (root / "oversize.bin").exists()
        assert existing.read_text(encoding="utf-8") == "modified"
        assert "Workspace quota exceeded." in job.stderr
        assert "Quota cleanup removed 1 entry (1024 bytes)." in job.stderr
    finally:
        await db.close()


@_requires_dir_fd
@pytest.mark.asyncio
async def test_durable_environment_quota_cleanup_removes_regenerable_roots(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        store = CodingTaskStore(db)
        task = await _create(store)
        workspace_manager = WorkspaceManager(tmp_path / "workspaces")
        root = workspace_manager.user_files_dir(task.workspace_key)
        (root / "test.sh").write_text("exit 0", encoding="utf-8")
        for env_name in (".venv", ".pio"):
            (root / env_name).mkdir()
            (root / env_name / "package.bin").write_bytes(b"package")

        async def quota_run(*args, **kwargs) -> SandboxResult:
            del args, kwargs
            return SandboxResult(
                exit_code=1,
                stdout="",
                stderr="Environment quota exceeded.",
                timed_out=False,
                duration_ms=1,
                quota_exceeded=True,
                environment_quota_exceeded=True,
            )

        monkeypatch.setattr(coding_jobs_module, "run_workspace_file_in_sandbox", quota_run)
        manager = _coding_job_manager(db, store, workspace_manager)

        job_id = await _run_coding_job(manager, task)

        job = await store.get_job(job_id)
        assert job is not None and job.status == CodingJobStatus.FAILED
        assert not (root / ".venv").exists()
        assert not (root / ".pio").exists()
        assert "including 2 environment roots" in job.stderr
    finally:
        await db.close()


@_requires_dir_fd
@pytest.mark.asyncio
async def test_durable_incomplete_snapshot_preserves_uncertain_ordinary_paths(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        store = CodingTaskStore(db)
        task = await _create(store)
        workspace_manager = WorkspaceManager(tmp_path / "workspaces")
        root = workspace_manager.user_files_dir(task.workspace_key)
        (root / "test.sh").write_text("exit 0", encoding="utf-8")
        (root / "preexisting.txt").write_text("keep", encoding="utf-8")

        async def quota_run(*args, **kwargs) -> SandboxResult:
            del args, kwargs
            (root / "uncertain-new.txt").write_text("preserve", encoding="utf-8")
            return SandboxResult(
                exit_code=1,
                stdout="",
                stderr="Workspace quota exceeded.",
                timed_out=False,
                duration_ms=1,
                quota_exceeded=True,
            )

        monkeypatch.setattr(coding_jobs_module, "run_workspace_file_in_sandbox", quota_run)
        manager = _coding_job_manager(
            db,
            store,
            workspace_manager,
            sandbox_config=SandboxConfig(max_workspace_files=1),
        )

        job_id = await _run_coding_job(manager, task)

        job = await store.get_job(job_id)
        assert job is not None and job.status == CodingJobStatus.FAILED
        assert (root / "uncertain-new.txt").read_text(encoding="utf-8") == "preserve"
        assert "Ordinary paths were preserved" in job.stderr
    finally:
        await db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exit_code", "expected_status"),
    [(0, CodingJobStatus.SUCCEEDED), (2, CodingJobStatus.FAILED)],
)
async def test_durable_nonquota_jobs_do_not_run_cleanup(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    exit_code: int,
    expected_status: CodingJobStatus,
) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        store = CodingTaskStore(db)
        task = await _create(store)
        workspace_manager = WorkspaceManager(tmp_path / "workspaces")
        root = workspace_manager.user_files_dir(task.workspace_key)
        (root / "test.sh").write_text("exit 0", encoding="utf-8")
        cleanup_calls = 0

        async def ordinary_run(*args, **kwargs) -> SandboxResult:
            del args, kwargs
            (root / "output.txt").write_text("retain", encoding="utf-8")
            return SandboxResult(exit_code, "", "ordinary failure", False, 1)

        def unexpected_cleanup(*args, **kwargs):
            nonlocal cleanup_calls
            del args, kwargs
            cleanup_calls += 1
            raise AssertionError("cleanup must not run")

        monkeypatch.setattr(coding_jobs_module, "run_workspace_file_in_sandbox", ordinary_run)
        monkeypatch.setattr(coding_jobs_module, "cleanup_quota_created_entries", unexpected_cleanup)
        manager = _coding_job_manager(db, store, workspace_manager)

        job_id = await _run_coding_job(manager, task)

        job = await store.get_job(job_id)
        assert job is not None and job.status == expected_status
        assert cleanup_calls == 0
        assert (root / "output.txt").read_text(encoding="utf-8") == "retain"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_durable_cleanup_failure_is_reported_without_changing_failure_status(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        store = CodingTaskStore(db)
        task = await _create(store)
        workspace_manager = WorkspaceManager(tmp_path / "workspaces")
        root = workspace_manager.user_files_dir(task.workspace_key)
        (root / "test.sh").write_text("exit 0", encoding="utf-8")

        async def quota_run(*args, **kwargs) -> SandboxResult:
            del args, kwargs
            return SandboxResult(
                exit_code=0,
                stdout="",
                stderr="Quota enforcement stopped the job.",
                timed_out=False,
                duration_ms=1,
                quota_exceeded=True,
            )

        def failed_cleanup(*args, **kwargs):
            del args, kwargs
            raise RuntimeError("internal cleanup details")

        monkeypatch.setattr(coding_jobs_module, "run_workspace_file_in_sandbox", quota_run)
        monkeypatch.setattr(coding_jobs_module, "cleanup_quota_created_entries", failed_cleanup)
        manager = _coding_job_manager(db, store, workspace_manager)

        job_id = await _run_coding_job(manager, task)

        job = await store.get_job(job_id)
        assert job is not None and job.status == CodingJobStatus.FAILED
        assert "Quota enforcement stopped the job." in job.stderr
        assert "Automatic quota cleanup could not be completed." in job.stderr
        assert "internal cleanup details" not in job.stderr
        assert "Traceback" not in job.stderr
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_delivery_snapshot_gets_fresh_retention_mtime(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        store = CodingTaskStore(db)
        task = await _create(store)
        manager = WorkspaceManager(tmp_path / "workspaces")
        source_root = manager.user_files_dir(task.workspace_key)
        source = source_root / "report.txt"
        source.write_text("finished", encoding="utf-8")
        os.utime(source, (1, 1))
        service = object.__new__(CodingTaskService)
        service._runtime = cast(Any, SimpleNamespace(workspace_manager=manager))

        files, roots = service._snapshot_delivery_outputs(task, [str(source)], [str(source_root)])

        assert len(files) == 1 and len(roots) == 1
        snapshot = Path(files[0]).resolve(strict=True)
        assert snapshot.read_text(encoding="utf-8") == "finished"
        assert snapshot.stat().st_mtime > source.stat().st_mtime
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_teardown_uncertainty_stays_active_until_unit_is_confirmed(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        store = CodingTaskStore(db)
        task = await _create(store)
        workspace_manager = WorkspaceManager(tmp_path / "workspaces")
        workspace_locks = UserLocks()
        script = workspace_manager.user_files_dir(task.workspace_key) / "test.sh"
        script.write_text("exit 0", encoding="utf-8")
        manager = CodingJobManager(
            store=store,
            workspace_manager=workspace_manager,
            workspace_locks=workspace_locks,
            sandbox_config=SandboxConfig(),
            max_seconds=60,
            max_cpu_seconds=10,
            runtime_guards=CodeExecRuntimeGuards.create(
                max_concurrency=1,
                network_weekly_limit=0,
            ),
            usage_store=UsageStore(db),
        )
        stop_started = asyncio.Event()
        allow_stop = asyncio.Event()

        async def unsafe_run(*args, **kwargs):
            del args, kwargs
            raise SandboxTeardownError("unit state unknown")

        async def stop_when_allowed(unit_name: str) -> None:
            del unit_name
            stop_started.set()
            await allow_stop.wait()

        monkeypatch.setattr(coding_jobs_module, "run_workspace_file_in_sandbox", unsafe_run)
        monkeypatch.setattr(coding_jobs_module, "stop_sandbox_unit", stop_when_allowed)
        async with manager.workspace_activity(task.workspace_key):
            job_id = await manager.start(
                task_id=task.id,
                workspace_key=task.workspace_key,
                request={"path": "test.sh", "mode": "shell"},
            )
            await stop_started.wait()

            unsafe = await store.get_job(job_id)
            assert unsafe is not None and unsafe.status == CodingJobStatus.UNSAFE
            cancellation = asyncio.create_task(manager.cancel(job_id))
            await asyncio.sleep(0)
            assert not cancellation.done()

            allow_stop.set()
            assert await cancellation is True
            cleaned = await store.get_job(job_id)
            assert cleaned is not None and cleaned.status == CodingJobStatus.INTERRUPTED
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_active_operation_can_be_stopped_outside_the_turn_lock() -> None:
    registry = ActiveOperationRegistry()
    entered = asyncio.Event()

    async def foreground() -> None:
        async with registry.register(user_id="u1", root_key="r1", channel_id="c1"):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(foreground())
    await entered.wait()

    count, clean = await registry.cancel(
        user_id="u1",
        root_key="r1",
        channel_id="c1",
        all_operations=False,
        wait_seconds=1,
    )

    assert count == 1
    assert clean is True
    assert task.cancelled()


@pytest.mark.asyncio
async def test_stop_tracks_detached_child_until_it_really_exits() -> None:
    registry = ActiveOperationRegistry()
    child_entered = asyncio.Event()
    release_child = asyncio.Event()

    async def child() -> None:
        async with registry.register(
            user_id="u1",
            root_key="r1",
            channel_id="c1",
            cancel_on_stop=False,
        ):
            child_entered.set()
            while not release_child.is_set():
                try:
                    await release_child.wait()
                except asyncio.CancelledError:
                    continue

    async def foreground() -> None:
        async with registry.register(user_id="u1", root_key="r1", channel_id="c1"):
            worker = asyncio.create_task(child())
            await child_entered.wait()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.shield(worker)

    root = asyncio.create_task(foreground())
    await child_entered.wait()
    try:
        count, clean = await registry.cancel(
            user_id="u1",
            root_key="r1",
            channel_id="c1",
            all_operations=False,
            wait_seconds=0.01,
        )
        assert count == 1
        assert clean is False
        with contextlib.suppress(asyncio.CancelledError):
            await root

        release_child.set()
        count, clean = await registry.cancel(
            user_id="u1",
            root_key="r1",
            channel_id="c1",
            all_operations=False,
            wait_seconds=1,
        )
        assert count == 1
        assert clean is True
    finally:
        release_child.set()
        await registry.cancel(
            user_id="u1",
            root_key="r1",
            channel_id="c1",
            all_operations=False,
            wait_seconds=1,
        )


def test_coding_registry_is_a_least_privilege_allowlist() -> None:
    source = ToolRegistry()

    async def unused(_args: dict, _ctx: MessageContext) -> str:
        return "unused"

    for name in {*CODING_WORKSPACE_TOOLS, "browser", "run_code", "block_user"}:
        source.register(
            name=name,
            description=name,
            parameters={"type": "object", "properties": {}},
            handler=unused,
        )

    registry = build_coding_registry(source, cast(CodingTaskControls, cast(Any, object())))

    assert registry.registered_names() == CODING_WORKSPACE_TOOLS | {
        "coding_plan",
        "coding_progress",
        "coding_request_input",
        "coding_job_start",
        "coding_job_status",
        "coding_job_cancel",
    }
    assert {
        schema["name"] for schema in registry.get_tool_schemas(TrustTier.MEMBER)
    } == registry.registered_names()


def test_coding_controls_are_visible_to_members() -> None:
    registry = ToolRegistry()
    init_coding_control_tools(registry, cast(CodingTaskControls, cast(Any, object())))

    visible = {schema["name"] for schema in registry.get_tool_schemas(TrustTier.MEMBER)}

    assert visible >= CODING_CONTROL_TOOLS


@pytest.mark.asyncio
async def test_successful_coding_start_sets_terminal_handoff() -> None:
    task_id = "3ff8bac7f9e24ed19a65d267c188d7ea"

    class Controls:
        async def start_from_tool(self, *_args, **_kwargs):
            return {"accepted": True, "task_id": task_id, "status": "queued"}

    registry = ToolRegistry()
    init_coding_control_tools(registry, cast(CodingTaskControls, Controls()))
    ctx = MessageContext(
        user_id="u1",
        user_name="User",
        guild_id="g1",
        channel_id="c1",
        thread_id=None,
        trust_tier=TrustTier.MEMBER,
    )

    result = await registry.dispatch("start_coding_task", {"task": "Fix it"}, ctx)

    assert f'"task_id": "{task_id}"' in result
    assert ctx.terminal_handoff is not None
    assert ctx.terminal_handoff.reason == "coding_task"
    assert ctx.terminal_handoff.task_id == task_id
    assert ctx.terminal_handoff.allowed_followup_tools == frozenset({"move_to_thread"})
    assert ctx.terminal_handoff.response_text == (
        "Coding task `3ff8bac7` was queued. Progress and the final result will appear here."
    )


@pytest.mark.asyncio
async def test_rejected_coding_start_does_not_end_foreground_turn() -> None:
    class Controls:
        async def start_from_tool(self, *_args, **_kwargs):
            return {"accepted": False, "error": "queue full"}

    registry = ToolRegistry()
    init_coding_control_tools(registry, cast(CodingTaskControls, Controls()))
    ctx = MessageContext(
        user_id="u1",
        user_name="User",
        guild_id="g1",
        channel_id="c1",
        thread_id=None,
        trust_tier=TrustTier.MEMBER,
    )

    result = await registry.dispatch("start_coding_task", {"task": "Fix it"}, ctx)

    assert '"accepted": false' in result
    assert ctx.terminal_handoff is None


@pytest.mark.asyncio
async def test_coding_delivery_retry_control_dispatches_for_member() -> None:
    requested: list[str] = []

    class Controls:
        async def retry_delivery_from_tool(self, _ctx, *, task_id: str):
            requested.append(task_id)
            return {"task_id": task_id, "delivery_retry_requested": True}

    registry = ToolRegistry()
    init_coding_control_tools(registry, cast(CodingTaskControls, Controls()))
    ctx = MessageContext(
        user_id="u1",
        user_name="User",
        guild_id="g1",
        channel_id="c1",
        thread_id=None,
        trust_tier=TrustTier.MEMBER,
    )

    result = await registry.dispatch(
        "coding_task_retry_delivery",
        {"task_id": "task-1"},
        ctx,
    )

    assert requested == ["task-1"]
    assert '"delivery_retry_requested": true' in result


@pytest.mark.asyncio
async def test_stopped_foreground_cancels_delegation_committed_at_boundary() -> None:
    stop_event = asyncio.Event()
    cancelled: list[str] = []

    class Controls:
        async def start_from_tool(self, *_args, **_kwargs):
            stop_event.set()
            return {"task_id": "task-1"}

        async def cancel_from_tool(self, _ctx, *, task_id: str, reason: str):
            assert "stopped" in reason.lower()
            cancelled.append(task_id)
            return {"task_id": task_id, "status": "cancelled"}

    registry = ToolRegistry()
    init_coding_control_tools(registry, cast(CodingTaskControls, Controls()))
    ctx = MessageContext(
        user_id="u1",
        user_name="User",
        guild_id="g1",
        channel_id="c1",
        thread_id=None,
        trust_tier=TrustTier.MEMBER,
        stop_event=stop_event,
    )

    result = await registry.dispatch(
        "start_coding_task",
        {"task": "Fix it"},
        ctx,
    )

    assert "delegated task was cancelled" in result
    assert cancelled == ["task-1"]
    assert ctx.terminal_handoff is None
