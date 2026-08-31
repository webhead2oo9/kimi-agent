"""Exercises app/coding_jobs.py and app/coding_tasks.py: the background
coding-job queue, per-workspace write admission, and handoff/claim
lifecycle backed by storage/coding_tasks.py.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import contextlib
import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from agent.attachments import AttachmentRef
from app.cancellation import ActiveOperationRegistry
from app import coding_jobs as coding_jobs_module
from app import coding_tasks as coding_tasks_app_module
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
from storage import coding_tasks as coding_tasks_module
from storage.db import Database, SCHEMA_VERSION
from storage.usage import UsageStore
from tools.code_exec import CodeExecRuntimeGuards
from tools.coding_tasks import (
    CODING_CONTROL_TOOLS,
    CODING_WORKER_TOOLS,
    CodingTaskControls,
    build_coding_registry,
    init_coding_control_tools,
)
from tools.registry import MessageContext, ToolRegistry
from providers.types import ContentPart, ConversationMessage
from tools.workspace.common import UserLocks
from tools.workspace.config import WorkspaceToolConfig
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
                staff_ids=frozenset(),
            )
        ),
    )
    return service


async def _nobody_blocked(_user_id: str) -> bool:
    return False


@asynccontextmanager
async def _no_user_activity(_user_id: str) -> AsyncIterator[None]:
    yield


async def _no_notifier(_task: object, _context: object = None) -> None:
    return None


def _start_service(
    store: CodingTaskStore,
    tmp_path: Path,
    *,
    max_queued_per_user: int = 10,
    max_queued_per_workspace: int = 10,
    user_blocked: Any = _nobody_blocked,
    notifier: Any = _no_notifier,
) -> CodingTaskService:
    service = object.__new__(CodingTaskService)
    service._store = store
    service._wake = asyncio.Event()
    service._publishers = {}
    service._last_published = {}
    service._runtime = cast(
        Any,
        SimpleNamespace(
            settings=SimpleNamespace(
                coding_task_max_seconds=60,
                coding_task_max_queued_per_user=max_queued_per_user,
                coding_task_max_queued_per_workspace=max_queued_per_workspace,
                coding_status_min_interval_seconds=0.0,
            ),
            workspace_manager=WorkspaceManager(tmp_path / "workspaces"),
            workspace_locks=UserLocks(),
            workspace_config=WorkspaceToolConfig(),
            user_blocked=user_blocked,
            notifier=notifier,
            user_activity=_no_user_activity,
        ),
    )
    return service


@pytest.mark.asyncio
async def test_claim_cancels_a_queued_task_whose_user_is_now_blocked(tmp_path) -> None:
    """A block that lands while a task waits must hold when it would start."""
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        store = CodingTaskStore(db)
        blocked = await _create(store, root_key="root-blocked")
        notified: list[str] = []

        async def user_blocked(user_id: str) -> bool:
            return user_id == blocked.user_id

        async def notifier(task, context) -> None:
            notified.append(task.status.value)

        service = _start_service(store, tmp_path, user_blocked=user_blocked, notifier=notifier)

        assert await service._claim_next_runnable() is None

        refreshed = await store.get_task(blocked.id)
        assert refreshed is not None
        assert refreshed.status is CodingTaskStatus.CANCELLED
        assert refreshed.error_text == "User is blocked"
        assert notified == ["cancelled"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_status_accepts_the_eight_character_task_reference_shown_to_users(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        store = CodingTaskStore(db)
        task = await _create(store)
        service = _steering_service(
            store,
            max_queued_per_user=10,
            max_queued_per_workspace=10,
        )

        result = await service.status_from_tool(_control_context(), task_id=task.id[:8])

        assert result is not None
        assert result["task_id"] == task.id
        assert result["display_summary"] == "Fix the project"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_short_task_reference_rejects_ambiguous_authorized_prefix(
    tmp_path, monkeypatch
) -> None:
    ids = iter(
        (
            "12345678aaaaaaaaaaaaaaaaaaaaaaaa",
            "12345678bbbbbbbbbbbbbbbbbbbbbbbb",
        )
    )
    monkeypatch.setattr(
        coding_tasks_module,
        "uuid4",
        lambda: SimpleNamespace(hex=next(ids)),
    )
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        store = CodingTaskStore(db)
        await _create(store, root_key="root-1")
        await _create(store, root_key="root-2")
        service = _steering_service(
            store,
            max_queued_per_user=10,
            max_queued_per_workspace=10,
        )

        result = await service.status_from_tool(_control_context(), task_id="12345678")

        assert result is None
    finally:
        await db.close()


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
async def test_coding_tables_use_current_schema(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        assert SCHEMA_VERSION == 4
        async with db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'coding_%'"
        ) as cursor:
            names = {str(row[0]) for row in await cursor.fetchall()}
        assert names == {"coding_tasks", "coding_task_events", "coding_command_jobs"}
        async with db.conn.execute("PRAGMA table_info(coding_tasks)") as cursor:
            columns = {str(row[1]) for row in await cursor.fetchall()}
        assert "handoff_pending" in columns
        assert {"display_summary", "context_messages_json", "input_files_json"} <= columns
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
        service = _start_service(
            store,
            tmp_path,
            max_queued_per_user=1,
            max_queued_per_workspace=1,
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
async def test_start_snapshots_context_and_selected_inputs(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        store = CodingTaskStore(db)
        service = _start_service(store, tmp_path)
        manager = service._runtime.workspace_manager
        ctx = _control_context()
        ctx.trigger_discord_message_id = "m1"
        ctx.handoff_context_messages = [
            {"role": "user", "section": "history", "text": "Use the parser we discussed."},
            {"role": "tool", "section": "turn", "text": "Relevant channel context."},
        ]
        ctx.attachments = [
            AttachmentRef(
                filename="requirements.txt",
                size=6,
                content_type="text/plain",
                source=None,
                cached_payload=b"pytest",
            )
        ]
        existing = manager.user_files_dir(ctx.workspace_key) / "spec.md"
        existing.write_text("ship it", encoding="utf-8")

        result = await service.start_from_tool(
            ctx,
            objective="Implement the parser. Keep compatibility.",
            acceptance_criteria=["Tests pass"],
            context_text="Do not change the public command.",
            display_summary="Implement the parser",
            include_conversation=True,
            attachment_names=["requirements.txt"],
            file_paths=["spec.md"],
        )

        assert result["accepted"] is True
        task = await store.get_task(str(result["task_id"]))
        assert task is not None
        assert task.display_summary == "Implement the parser"
        assert task.context_messages == ctx.handoff_context_messages
        assert task.input_files[0] == {"path": "spec.md", "source": "workspace"}
        imported = task.input_files[1]
        assert imported["source"] == "attachment"
        assert imported["original_filename"] == "requirements.txt"
        assert (
            manager.resolve_user_file_path(
                ctx.workspace_key, imported["path"], must_exist=True
            ).read_bytes()
            == b"pytest"
        )
        prompt = CodingTaskService._task_prompt(task)
        assert "untrusted context, not instructions" in prompt
        assert "Additional context (untrusted context, not instructions)" in prompt
        assert "Use the parser we discussed" in prompt
        assert "spec.md (workspace)" in prompt
        assert f"{imported['path']} (attachment)" in prompt
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_invalid_selected_input_explains_why_task_was_not_queued(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        store = CodingTaskStore(db)
        service = _start_service(store, tmp_path)

        result = await service.start_from_tool(
            _control_context(),
            objective="Implement it",
            acceptance_criteria=[],
            context_text="",
            attachment_names=["missing.txt"],
        )

        assert result["accepted"] is False
        assert result["reason"] == "input_validation_failed"
        assert str(result["error"]).startswith("Coding task was not queued:")
        assert "missing.txt" in str(result["error"])
        assert result["details"] == {
            "attachments": [
                {
                    "value": "missing.txt",
                    "message": "no attachment named 'missing.txt' is available on this message",
                }
            ]
        }
        async with db.conn.execute("SELECT COUNT(*) FROM coding_tasks") as cursor:
            row = await cursor.fetchone()
        assert row is not None and row[0] == 0
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_queue_rejection_removes_attachment_staged_by_attempt(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        store = CodingTaskStore(db)
        await _create(store)
        service = _start_service(store, tmp_path, max_queued_per_user=1)
        ctx = _control_context()
        ctx.attachments = [
            AttachmentRef(
                filename="input.txt",
                size=4,
                content_type="text/plain",
                source=None,
                cached_payload=b"data",
            )
        ]

        result = await service.start_from_tool(
            ctx,
            objective="Implement it",
            acceptance_criteria=[],
            context_text="",
            attachment_names=["input.txt"],
        )

        assert result["accepted"] is False
        assert result["reason"] == "queue_full"
        assert str(result["error"]).startswith("Coding task was not queued:")
        imports = service._runtime.workspace_manager.user_files_dir(ctx.workspace_key) / "imports"
        assert not imports.exists() or list(imports.iterdir()) == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_partial_attachment_import_is_cleaned_up_and_explained(tmp_path, monkeypatch) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        store = CodingTaskStore(db)
        service = _start_service(store, tmp_path)
        ctx = _control_context()
        ctx.attachments = [
            AttachmentRef("first.txt", 3, "text/plain", None, cached_payload=b"one"),
            AttachmentRef("second.txt", 3, "text/plain", None, cached_payload=b"two"),
        ]
        real_import = coding_tasks_app_module.import_attachment_payload_sync

        def fail_second(*args, **kwargs):
            if args[3] == "second.txt":
                return {"error": "workspace quota is full"}
            return real_import(*args, **kwargs)

        monkeypatch.setattr(
            coding_tasks_app_module,
            "import_attachment_payload_sync",
            fail_second,
        )

        result = await service.start_from_tool(
            ctx,
            objective="Implement it",
            acceptance_criteria=[],
            context_text="",
            attachment_names=["first.txt", "second.txt"],
        )

        assert result["accepted"] is False
        assert result["reason"] == "input_import_failed"
        assert "workspace quota is full" in str(result["error"])
        imports = service._runtime.workspace_manager.user_files_dir(ctx.workspace_key) / "imports"
        assert not imports.exists() or list(imports.iterdir()) == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_cancelling_attachment_staging_waits_for_write_then_cleans_up(
    tmp_path, monkeypatch
) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        store = CodingTaskStore(db)
        service = _start_service(store, tmp_path)
        ctx = _control_context()
        ctx.attachments = [
            AttachmentRef("input.txt", 4, "text/plain", None, cached_payload=b"data")
        ]
        started = threading.Event()
        release = threading.Event()
        real_import = coding_tasks_app_module.import_attachment_payload_sync

        def slow_import(*args, **kwargs):
            started.set()
            assert release.wait(timeout=2)
            return real_import(*args, **kwargs)

        monkeypatch.setattr(
            coding_tasks_app_module,
            "import_attachment_payload_sync",
            slow_import,
        )
        start = asyncio.create_task(
            service.start_from_tool(
                ctx,
                objective="Implement it",
                acceptance_criteria=[],
                context_text="",
                attachment_names=["input.txt"],
            )
        )
        assert await asyncio.to_thread(started.wait, 1)

        start.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await start

        imports = service._runtime.workspace_manager.user_files_dir(ctx.workspace_key) / "imports"
        assert not imports.exists() or list(imports.iterdir()) == []
        async with db.conn.execute("SELECT COUNT(*) FROM coding_tasks") as cursor:
            row = await cursor.fetchone()
        assert row is not None and row[0] == 0
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_definitive_await_propagates_child_cancellation() -> None:
    async def cancelled_child() -> None:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await CodingTaskService._await_definitive(cancelled_child())


@pytest.mark.asyncio
async def test_cancelling_task_persistence_cleans_up_staged_attachment(
    tmp_path, monkeypatch
) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        store = CodingTaskStore(db)
        service = _start_service(store, tmp_path)
        ctx = _control_context()
        ctx.attachments = [
            AttachmentRef("input.txt", 4, "text/plain", None, cached_payload=b"data")
        ]
        task_committed = asyncio.Event()
        release_read = asyncio.Event()
        original_get_task = store.get_task

        async def wait_after_commit(task_id: str):
            task_committed.set()
            await release_read.wait()
            return await original_get_task(task_id)

        monkeypatch.setattr(store, "get_task", wait_after_commit)
        start = asyncio.create_task(
            service.start_from_tool(
                ctx,
                objective="Implement it",
                acceptance_criteria=[],
                context_text="",
                attachment_names=["input.txt"],
            )
        )
        await asyncio.wait_for(task_committed.wait(), timeout=1)

        start.cancel()
        release_read.set()
        with pytest.raises(asyncio.CancelledError):
            await start

        imports = service._runtime.workspace_manager.user_files_dir(ctx.workspace_key) / "imports"
        assert not imports.exists() or list(imports.iterdir()) == []
        async with db.conn.execute(
            "SELECT status, handoff_pending, input_files_json FROM coding_tasks"
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        assert tuple(row[:2]) == ("cancelled", 0)
        assert json.loads(row[2]) == [
            {
                "path": "imports/input.txt",
                "source": "attachment",
                "original_filename": "input.txt",
            }
        ]
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
async def test_delivery_attachment_plan_is_frozen_and_status_metadata_is_sanitized(
    tmp_path,
) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        store = CodingTaskStore(db)
        task = await _create(store)
        await store.set_checkpoint(
            task.id,
            {
                "delivery": {
                    "thread_id": "99",
                    "output_files": ["C:/private/workspace/large.zip"],
                }
            },
        )
        first_plan = {
            "effective_limit_bytes": 10,
            "notice_text": "Delivery notice: large.zip was omitted.",
            "omitted": [
                {
                    "path": "C:/private/workspace/large.zip",
                    "filename": "large.zip",
                    "size_bytes": 11,
                    "reason": "oversize",
                }
            ],
        }
        competing_plan = {
            "effective_limit_bytes": 20,
            "notice_text": "different",
            "omitted": [],
        }

        frozen = await store.set_delivery_attachment_plan_if_absent(task.id, first_plan)
        repeated = await store.set_delivery_attachment_plan_if_absent(task.id, competing_plan)

        assert frozen == first_plan
        assert repeated == first_plan
        refreshed = await store.get_task(task.id)
        assert refreshed is not None
        delivery = refreshed.checkpoint["delivery"]
        assert delivery["thread_id"] == "99"
        assert delivery["output_files"] == ["C:/private/workspace/large.zip"]
        assert delivery["attachment_plan"] == first_plan
        assert [event.kind for event in await store.events(task.id)].count(
            "delivery_attachment_plan"
        ) == 1

        payload = CodingTaskService._task_payload(refreshed)
        assert payload["attachment_outcomes"] == {
            "effective_limit_bytes": 10,
            "omitted": [
                {
                    "filename": "large.zip",
                    "size_bytes": 11,
                    "reason": "oversize",
                }
            ],
        }
        assert "C:/private" not in json.dumps(payload["attachment_outcomes"])
        assert "notice_text" not in json.dumps(payload["attachment_outcomes"])
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
async def test_provisional_and_rooted_registration_count_as_one_response() -> None:
    registry = ActiveOperationRegistry()
    entered = asyncio.Event()

    async def foreground() -> None:
        with registry.register_provisional(user_id="u1", channel_id="c1"):
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
async def test_multiple_unresolved_provisional_turns_are_counted_separately() -> None:
    registry = ActiveOperationRegistry()
    entered = [asyncio.Event(), asyncio.Event()]

    async def foreground(index: int) -> None:
        with registry.register_provisional(user_id="u1", channel_id="c1"):
            entered[index].set()
            await asyncio.Event().wait()

    tasks = [asyncio.create_task(foreground(index)) for index in range(2)]
    await asyncio.gather(*(event.wait() for event in entered))

    count, clean = await registry.cancel(
        user_id="u1",
        root_key="r1",
        channel_id="c1",
        all_operations=False,
        wait_seconds=1,
    )

    assert count == 2
    assert clean is True
    assert all(task.cancelled() for task in tasks)


@pytest.mark.asyncio
async def test_bound_provisional_stop_matches_only_its_resolved_root() -> None:
    registry = ActiveOperationRegistry()
    entered = {"r1": asyncio.Event(), "r2": asyncio.Event()}

    async def foreground(root_key: str) -> None:
        with registry.register_provisional(user_id="u1", channel_id="c1"):
            registry.bind_current_provisional(root_key)
            entered[root_key].set()
            await asyncio.Event().wait()

    first = asyncio.create_task(foreground("r1"))
    second = asyncio.create_task(foreground("r2"))
    await asyncio.gather(*(event.wait() for event in entered.values()))

    count, clean = await registry.cancel(
        user_id="u1",
        root_key="r1",
        channel_id="c1",
        all_operations=False,
        wait_seconds=1,
    )

    assert count == 1
    assert clean is True
    assert first.cancelled()
    assert not second.done()

    second.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second


@pytest.mark.asyncio
async def test_cancel_all_rescans_cleanup_child_registered_during_root_exit() -> None:
    registry = ActiveOperationRegistry()
    root_entered = asyncio.Event()
    child_entered = asyncio.Event()
    child_stop = asyncio.Event()
    children: list[asyncio.Task[None]] = []

    async def child() -> None:
        async with registry.register(
            user_id="u1",
            root_key="r1",
            channel_id="c1",
            cancel_on_stop=False,
            stop_event=child_stop,
        ):
            child_entered.set()
            await child_stop.wait()

    async def root() -> None:
        async with registry.register(user_id="u1", root_key="r1", channel_id="c1"):
            root_entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                children.append(asyncio.create_task(child()))
                await child_entered.wait()
                raise

    root_task = asyncio.create_task(root())
    await root_entered.wait()

    await registry.cancel_all()

    assert root_task.cancelled()
    assert child_stop.is_set()
    assert children[0].done()


@pytest.mark.asyncio
async def test_cancel_all_cancels_each_task_only_once() -> None:
    registry = ActiveOperationRegistry()
    entered = asyncio.Event()
    cancellation_counts: list[int] = []

    async def foreground() -> None:
        with registry.register_provisional(user_id="u1", channel_id="c1"):
            async with registry.register(user_id="u1", root_key="r1", channel_id="c1"):
                entered.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    task = asyncio.current_task()
                    assert task is not None
                    cancellation_counts.append(task.cancelling())
                    raise

    task = asyncio.create_task(foreground())
    await entered.wait()

    await registry.cancel_all()

    assert task.cancelled()
    assert cancellation_counts == [1]


@pytest.mark.asyncio
async def test_cancel_all_bounds_non_cancellable_cleanup_wait() -> None:
    registry = ActiveOperationRegistry()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def cleanup() -> None:
        async with registry.register(
            user_id="u1",
            root_key="r1",
            channel_id="c1",
            cancel_on_stop=False,
        ):
            entered.set()
            await release.wait()

    task = asyncio.create_task(cleanup())
    await entered.wait()

    assert await registry.cancel_all(wait_seconds=0.01) is False
    assert task.done() is False

    release.set()
    await task


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

    for name in {*CODING_WORKER_TOOLS, "run_code", "block_user", "teach", "browse_tools"}:
        source.register(
            name=name,
            description=name,
            parameters={"type": "object", "properties": {}},
            handler=unused,
            searchable=name in {"extract_archive", "extract_document_text"},
        )

    registry = build_coding_registry(source, cast(CodingTaskControls, cast(Any, object())))

    assert {"browser", "internet_search", "fetch_url"} <= CODING_WORKER_TOOLS
    assert registry.registered_names() == CODING_WORKER_TOOLS | {
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


def test_coding_checkpoint_serialization_omits_image_payloads() -> None:
    message = ConversationMessage(
        role="user",
        content=[
            ContentPart.from_text("Transient browser screenshot."),
            ContentPart.from_image_url(
                url="data:image/png;base64," + ("A" * 10_000),
                media_type="image/png",
            ),
        ],
    )

    serialized = CodingTaskService._serialize_message(message)

    assert serialized["content"] == [{"type": "text", "text": "Transient browser screenshot."}]
    assert "base64" not in json.dumps(serialized)


def test_coding_registry_omits_web_tools_the_foreground_lacks() -> None:
    """Web tools follow the assistant's gates: not registered there, not here."""

    source = ToolRegistry()

    async def unused(_args: dict, _ctx: MessageContext) -> str:
        return "unused"

    for name in CODING_WORKER_TOOLS - {"browser", "internet_search", "fetch_url"}:
        source.register(
            name=name,
            description=name,
            parameters={"type": "object", "properties": {}},
            handler=unused,
        )

    registry = build_coding_registry(source, cast(CodingTaskControls, cast(Any, object())))

    assert not registry.registered_names() & {"browser", "internet_search", "fetch_url"}


def test_coding_controls_are_visible_to_members() -> None:
    registry = ToolRegistry()
    init_coding_control_tools(registry, cast(CodingTaskControls, cast(Any, object())))

    visible = {schema["name"] for schema in registry.get_tool_schemas(TrustTier.MEMBER)}

    assert visible >= CODING_CONTROL_TOOLS


def test_display_summary_fallback_is_deterministic_and_bounded() -> None:
    assert (
        CodingTaskService._display_summary(
            "  Fix   the parser. Then update every caller.  ",
            "",
        )
        == "Fix the parser."
    )
    shortened = CodingTaskService._display_summary("x" * 250, "")
    assert len(shortened) == 200
    assert shortened.endswith("…")


@pytest.mark.asyncio
async def test_invalid_coding_start_returns_structured_rejection_notice() -> None:
    registry = ToolRegistry()
    init_coding_control_tools(registry, cast(CodingTaskControls, cast(Any, object())))

    result = json.loads(
        await registry.dispatch(
            "start_coding_task",
            {"task": "Fix it", "files": [f"file-{index}" for index in range(21)]},
            _control_context(),
        )
    )

    assert result == {
        "accepted": False,
        "reason": "invalid_arguments",
        "error": "Coding task was not queued: files accepts at most 20 values.",
    }


@pytest.mark.asyncio
async def test_successful_coding_start_sets_terminal_handoff() -> None:
    task_id = "3ff8bac7f9e24ed19a65d267c188d7ea"
    captured: dict[str, object] = {}

    class Controls:
        async def start_from_tool(self, *_args, **kwargs):
            captured.update(kwargs)
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

    result = await registry.dispatch(
        "start_coding_task",
        {
            "task": "Fix it",
            "acceptance_criteria": ["Tests pass"],
            "context": "Keep the API stable",
            "display_summary": "Fix the parser",
            "include_conversation": True,
            "attachments": ["requirements.txt"],
            "files": ["spec.md"],
        },
        ctx,
    )

    assert f'"task_id": "{task_id}"' in result
    assert ctx.terminal_handoff is not None
    assert ctx.terminal_handoff.reason == "coding_task"
    assert ctx.terminal_handoff.task_id == task_id
    assert ctx.terminal_handoff.allowed_followup_tools == frozenset({"move_to_thread"})
    assert ctx.terminal_handoff.response_text == (
        "Coding task `3ff8bac7` was queued. Progress and the final result will appear here."
    )
    assert captured == {
        "objective": "Fix it",
        "acceptance_criteria": ["Tests pass"],
        "context_text": "Keep the API stable",
        "display_summary": "Fix the parser",
        "include_conversation": True,
        "attachment_names": ["requirements.txt"],
        "file_paths": ["spec.md"],
    }


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
