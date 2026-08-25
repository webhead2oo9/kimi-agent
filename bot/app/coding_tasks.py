from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from functools import partial
from typing import Any, Literal, cast
from pathlib import Path

from agent.context import ConversationContext
from agent.core import ConversationRunRequest, run_conversation
from app.coding_jobs import CodingJobManager
from config.model_config import Scope
from config.settings import Settings
from providers.types import ContentPart, ConversationMessage, ToolCall
from storage.coding_tasks import (
    ACTIVE_TASK_STATUSES,
    CodingJobStatus,
    CodingTask,
    CodingTaskQueueFull,
    CodingTaskStatus,
    CodingTaskStore,
)
from storage.usage import UsageStore
from tools.coding_tasks import build_coding_registry
from tools.registry import MessageContext, ToolRegistry
from trust.tiers import TrustTier
from usage.pricing import price_usage_call
from usage.normalization import LLMUsageCall
from workspace import WorkspaceManager

logger = logging.getLogger(__name__)

TaskNotifier = Callable[[CodingTask, ConversationContext | None], Awaitable[None]]
UserActivityGuard = Callable[[str], AbstractAsyncContextManager[None]]
BlockedToolsResolver = Callable[[str, str], frozenset[str]]
ToolConfigResolver = Callable[[Any], dict[str, dict[str, Any]]]


@dataclass(frozen=True, slots=True)
class CodingTaskRuntime:
    settings: Settings
    store: CodingTaskStore
    usage_store: UsageStore
    provider_manager: Any
    source_registry: ToolRegistry
    jobs: CodingJobManager
    llm_semaphore: asyncio.Semaphore
    compactor: Any
    model_config: Any
    notifier: TaskNotifier
    user_activity: UserActivityGuard
    workspace_manager: WorkspaceManager
    blocked_tools: BlockedToolsResolver
    tool_configs: ToolConfigResolver


class CodingTaskService:
    """Queue, run, steer, recover, and cancel durable coding agents."""

    def __init__(self, runtime: CodingTaskRuntime) -> None:
        self._runtime = runtime
        self._store = runtime.store
        self._coding_registry = build_coding_registry(runtime.source_registry, self)
        self._wake = asyncio.Event()
        self._scheduler: asyncio.Task[None] | None = None
        self._workers: dict[str, asyncio.Task[None]] = {}
        self._publishers: dict[str, asyncio.Task[None]] = {}
        self._delivery_retries: dict[str, asyncio.Task[None]] = {}
        self._delivery_semaphore = asyncio.Semaphore(4)
        self._last_published: dict[str, float] = {}
        self._closed = False
        self._last_delivery_retry = 0.0

    async def start(self) -> None:
        if self._scheduler is not None:
            return
        await self._runtime.jobs.stop_recovered_units()
        await self._store.recover_interrupted()
        pending_handoffs = await self._store.list_handoff_pending()
        for task in pending_handoffs:
            await self._finalize_handoff(task)
        self._scheduler = asyncio.create_task(self._scheduler_loop(), name="coding_task_scheduler")
        self._wake.set()

    async def close(self) -> None:
        self._closed = True
        if self._scheduler is not None:
            self._scheduler.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._scheduler
            self._scheduler = None
        for worker in list(self._workers.values()):
            worker.cancel()
        await asyncio.gather(*self._workers.values(), return_exceptions=True)
        self._workers.clear()
        for retry in self._delivery_retries.values():
            retry.cancel()
        await asyncio.gather(*self._delivery_retries.values(), return_exceptions=True)
        self._delivery_retries.clear()
        for publisher in self._publishers.values():
            publisher.cancel()
        await asyncio.gather(*self._publishers.values(), return_exceptions=True)
        self._publishers.clear()
        await self._runtime.jobs.close()

    async def start_from_tool(
        self,
        ctx: MessageContext,
        *,
        objective: str,
        acceptance_criteria: list[str],
        context_text: str,
    ) -> dict[str, object]:
        settings = self._runtime.settings
        try:
            task = await self._store.create_task(
                conversation_id=ctx.conversation_id,
                root_key=ctx.context_key,
                workspace_key=str(ctx.workspace_key),
                user_id=ctx.user_id,
                user_name=ctx.user_name,
                guild_id=ctx.guild_id,
                channel_id=ctx.channel_id,
                thread_id=ctx.thread_id,
                handoff_pending=True,
                trigger_discord_message_id=ctx.trigger_discord_message_id,
                objective=objective,
                acceptance_criteria=acceptance_criteria,
                context_text=context_text,
                max_seconds=settings.coding_task_max_seconds,
                initial_checkpoint={"trust_tier": ctx.trust_tier.value},
                max_queued_per_user=settings.coding_task_max_queued_per_user,
                max_queued_per_workspace=settings.coding_task_max_queued_per_workspace,
            )
        except CodingTaskQueueFull as exc:
            error = (
                "Your coding-task queue is full."
                if exc.scope == "user"
                else "This workspace's coding queue is full."
            )
            return {"accepted": False, "error": error}
        if ctx.turn_finalization_started:
            await self._store.abandon_handoff(
                task.id,
                reason="Foreground turn ended before coding delegation was acknowledged",
            )
            return {"accepted": False, "error": "The foreground turn ended before delegation"}
        return {"accepted": True, "task_id": task.id, "status": task.status.value}

    async def finalize_handoff(
        self,
        task_id: str,
        *,
        channel_id: str | None = None,
        thread_id: str | None = None,
    ) -> bool:
        if not await self.prepare_handoff(
            task_id,
            channel_id=channel_id,
            thread_id=thread_id,
        ):
            return False
        return await self.release_handoff(task_id)

    async def prepare_handoff(
        self,
        task_id: str,
        *,
        channel_id: str | None = None,
        thread_id: str | None = None,
    ) -> bool:
        task = await self._store.get_task(task_id)
        if task is None or not task.handoff_pending:
            return False
        if channel_id is not None:
            bound = await self._store.bind_handoff_target(
                task_id,
                channel_id=channel_id,
                thread_id=thread_id,
            )
            if not bound:
                return False
            task = await self._store.get_task(task_id)
            if task is None:
                return False
        return True

    async def _finalize_handoff(self, task: CodingTask) -> bool:
        if not await self.prepare_handoff(task.id):
            return False
        return await self.release_handoff(task.id)

    async def release_handoff(self, task_id: str) -> bool:
        task = await self._store.get_task(task_id)
        if task is None or not task.handoff_pending:
            return False
        # Runtime calls this only after the foreground acknowledgement has been
        # attempted. Paint the editable queued status now, while the task is
        # still held, so it cannot be claimed and edited to "running" above a
        # newer "was queued" acknowledgement.
        await self._notify(task)
        released = await self._store.release_handoff(task_id)
        if released:
            self._wake.set()
        return released

    async def status_from_tool(
        self, ctx: MessageContext, *, task_id: str | None
    ) -> dict[str, object] | None:
        task = await self._resolve_control_task(ctx, task_id)
        if task is None:
            return None
        return self._task_payload(task)

    async def retry_delivery_from_tool(
        self,
        ctx: MessageContext,
        *,
        task_id: str,
    ) -> dict[str, object] | None:
        task = await self._authorized_task(ctx, task_id)
        if task is None:
            return None
        reset = await self._store.reset_delivery_retry(task.id)
        if reset:
            self._last_delivery_retry = 0.0
            self._wake.set()
        refreshed = await self._store.get_task(task.id)
        payload = self._task_payload(refreshed)
        payload["delivery_retry_requested"] = reset
        return payload

    async def steer_from_tool(
        self, ctx: MessageContext, *, task_id: str, message: str
    ) -> dict[str, object] | None:
        task = await self._authorized_task(ctx, task_id)
        if task is None or task.status not in ACTIVE_TASK_STATUSES:
            return None
        settings = self._runtime.settings
        try:
            refreshed = await self._store.steer_active_task(
                task.id,
                message,
                max_queued_per_user=settings.coding_task_max_queued_per_user,
                max_queued_per_workspace=settings.coding_task_max_queued_per_workspace,
            )
        except CodingTaskQueueFull as exc:
            error = (
                "Your coding-task queue is full."
                if exc.scope == "user"
                else "This workspace's coding queue is full."
            )
            return {
                "task_id": task.id,
                "accepted": False,
                "error": error,
                "status": CodingTaskStatus.WAITING_FOR_INPUT.value,
            }
        self._wake.set()
        return {
            "task_id": task.id,
            "accepted": (
                refreshed is not None
                and refreshed.status in ACTIVE_TASK_STATUSES
                and not refreshed.cancel_requested
            ),
            "status": refreshed.status.value if refreshed is not None else task.status.value,
        }

    async def cancel_from_tool(
        self, ctx: MessageContext, *, task_id: str, reason: str
    ) -> dict[str, object] | None:
        task = await self._authorized_task(ctx, task_id)
        if task is None:
            return None
        await self.cancel_task(task.id, reason=reason)
        refreshed = await self._store.get_task(task.id)
        return self._task_payload(refreshed) if refreshed is not None else None

    async def cancel_task(self, task_id: str, *, reason: str = "") -> bool:
        task = await self._store.get_task(task_id)
        if task is None:
            return False
        if task.status not in ACTIVE_TASK_STATUSES:
            return True
        await self._store.request_cancel(task_id, reason=reason)
        worker = self._workers.get(task_id)
        if worker is not None:
            worker.cancel()
            done, pending = await asyncio.wait(
                {worker},
                timeout=self._runtime.settings.coding_stop_cleanup_wait_seconds,
            )
            for completed in done:
                with contextlib.suppress(BaseException):
                    completed.result()
            if pending:
                logger.warning("Coding task %s cancellation cleanup is still running", task_id)
        else:
            refreshed = await self._store.get_task(task_id)
            if refreshed is not None and refreshed.status == CodingTaskStatus.CANCELLED:
                await self._notify(refreshed)
        self._wake.set()
        return True

    async def cancel_for_scope(
        self,
        *,
        user_id: str,
        root_key: str | None,
        channel_id: str | None = None,
        all_tasks: bool = False,
    ) -> tuple[list[str], bool]:
        if all_tasks:
            tasks = await self._store.list_active(user_id=user_id)
        elif root_key:
            tasks = await self._store.list_active(user_id=user_id, root_key=root_key)
        elif channel_id:
            tasks = await self._store.list_active(user_id=user_id, channel_id=channel_id)
        else:
            tasks = []
        cancelled: list[str] = []
        clean = True
        for task in tasks:
            if await self.cancel_task(task.id, reason="Stopped by user"):
                cancelled.append(task.id)
                refreshed = await self._store.get_task(task.id)
                worker = self._workers.get(task.id)
                clean = clean and bool(
                    refreshed is not None
                    and refreshed.status not in ACTIVE_TASK_STATUSES
                    and (worker is None or worker.done())
                )
        return cancelled, clean

    async def cleanup_complete(self, task_id: str) -> bool:
        worker = self._workers.get(task_id)
        if worker is not None and not worker.done():
            return False
        task = await self._store.get_task(task_id)
        return task is not None and task.status not in ACTIVE_TASK_STATUSES

    async def set_plan(self, task_id: str, steps: list[dict[str, str]]) -> None:
        await self._store.set_plan(task_id, steps)
        await self._notify_id(task_id)

    async def set_progress(self, task_id: str, message: str) -> None:
        await self._store.set_milestone(task_id, message)
        await self._notify_id(task_id)

    async def request_input(self, task_id: str, message: str) -> None:
        await self._store.set_milestone(task_id, message)
        changed = await self._store.transition_active_status(
            task_id,
            CodingTaskStatus.WAITING_FOR_INPUT,
            from_statuses=frozenset({CodingTaskStatus.RUNNING, CodingTaskStatus.WAITING_FOR_JOB}),
        )
        if not changed:
            raise RuntimeError("coding task is no longer active")
        await self._notify_id(task_id)

    async def start_job(self, task_id: str, request: dict[str, object]) -> str:
        task = await self._store.get_task(task_id)
        if task is None or task.cancel_requested:
            raise RuntimeError("coding task is no longer active")
        job_id = await self._runtime.jobs.start(
            task_id=task_id,
            workspace_key=task.workspace_key,
            request=request,
        )
        changed = await self._store.transition_active_status(
            task_id,
            CodingTaskStatus.WAITING_FOR_JOB,
            from_statuses=frozenset({CodingTaskStatus.RUNNING, CodingTaskStatus.WAITING_FOR_JOB}),
        )
        if not changed:
            await self._runtime.jobs.cancel(job_id)
            raise RuntimeError("coding task was cancelled while the job was starting")
        await self._notify_id(task_id)
        return job_id

    async def job_status(
        self, task_id: str, job_id: str, wait_seconds: float
    ) -> dict[str, object] | None:
        await self._runtime.jobs.wait(job_id, max(0.0, wait_seconds))
        job = await self._store.get_job(job_id)
        if job is None or job.task_id != task_id:
            return None
        if job.status not in {CodingJobStatus.QUEUED, CodingJobStatus.RUNNING}:
            task = await self._store.get_task(task_id)
            if task is not None and task.status == CodingTaskStatus.WAITING_FOR_JOB:
                await self._store.transition_active_status(
                    task_id,
                    CodingTaskStatus.RUNNING,
                    from_statuses=frozenset({CodingTaskStatus.WAITING_FOR_JOB}),
                )
                await self._notify_id(task_id)
        return {
            "job_id": job.id,
            "status": job.status.value,
            "exit_code": job.exit_code,
            "timed_out": job.timed_out,
            "stdout": job.stdout,
            "stderr": job.stderr,
        }

    async def cancel_job(self, task_id: str, job_id: str) -> bool:
        job = await self._store.get_job(job_id)
        if job is None or job.task_id != task_id:
            return False
        return await self._runtime.jobs.cancel(job_id)

    async def _scheduler_loop(self) -> None:
        while not self._closed:
            try:
                for expired in await self._store.expire_waiting_for_input():
                    if expired.id in self._delivery_retries:
                        continue
                    retry = asyncio.create_task(
                        self._retry_pending_delivery(expired),
                        name=f"coding_delivery_retry:{expired.id}",
                    )
                    self._delivery_retries[expired.id] = retry
                    retry.add_done_callback(partial(self._delivery_retry_done, task_id=expired.id))
                now = time.monotonic()
                if now - self._last_delivery_retry >= 10.0:
                    self._last_delivery_retry = now
                    for pending in await self._store.list_pending_delivery():
                        if pending.id in self._delivery_retries:
                            continue
                        retry = asyncio.create_task(
                            self._retry_pending_delivery(pending),
                            name=f"coding_delivery_retry:{pending.id}",
                        )
                        self._delivery_retries[pending.id] = retry
                        retry.add_done_callback(
                            partial(self._delivery_retry_done, task_id=pending.id)
                        )
                while len(self._workers) < self._runtime.settings.coding_task_max_concurrency:
                    task = await self._store.claim_next()
                    if task is None:
                        break
                    worker = asyncio.create_task(
                        self._run_task_guarded(task), name=f"coding_task:{task.id}"
                    )
                    self._workers[task.id] = worker
                    worker.add_done_callback(partial(self._worker_done, task_id=task.id))
                self._wake.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), timeout=1.0)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Coding task scheduler failed")
                await asyncio.sleep(1.0)

    async def _retry_pending_delivery(self, task: CodingTask) -> None:
        async with self._delivery_semaphore:
            await self._notify(task)

    def _delivery_retry_done(self, completed: asyncio.Task[None], *, task_id: str) -> None:
        self._delivery_retries.pop(task_id, None)
        if not completed.cancelled():
            with contextlib.suppress(Exception):
                completed.result()

    def _worker_done(self, _completed: asyncio.Task[None], *, task_id: str) -> None:
        worker = self._workers.pop(task_id, None)
        if worker is not None and not worker.cancelled():
            with contextlib.suppress(Exception):
                worker.result()
        self._wake.set()

    async def _run_task_guarded(self, task: CodingTask) -> None:
        heartbeat = asyncio.create_task(
            self._heartbeat_loop(task.id), name=f"coding_heartbeat:{task.id}"
        )
        context: ConversationContext | None = None
        cancellation: asyncio.CancelledError | None = None
        try:
            try:
                async with self._runtime.user_activity(task.user_id):
                    async with self._runtime.jobs.workspace_activity(task.workspace_key):
                        context = await self._run_task(task)
            except asyncio.CancelledError as exc:
                cancellation = exc
            # Terminal delivery reads queued artifacts, so it must happen after
            # the long-lived writer lock is released. Detached tool children keep
            # writer references and are drained before STOP can report cleanup.
            finalizer = asyncio.create_task(
                self._finalize_task_run(task, context),
                name=f"coding_finalize:{task.id}",
            )
            while not finalizer.done():
                try:
                    await asyncio.shield(finalizer)
                except asyncio.CancelledError as exc:
                    # Keep the worker registered until detached commands release
                    # their writer references and terminal delivery has finished.
                    # Repeated STOP/shutdown cancellation must not orphan cleanup.
                    if cancellation is None:
                        cancellation = exc
            await finalizer
            if cancellation is not None:
                raise cancellation
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat

    async def _finalize_task_run(
        self, task: CodingTask, context: ConversationContext | None
    ) -> None:
        # A shielded coding_job_start may have crossed its last model boundary
        # just as the root was cancelled. Job admission is serialized against
        # this sweep, so anything admitted is now registered and cancellable.
        await self._runtime.jobs.cancel_task(task.id)
        await self._runtime.jobs.wait_workspace_idle(task.workspace_key)
        await self._notify_id(task.id, context)

    async def _heartbeat_loop(self, task_id: str) -> None:
        interval = min(30.0, max(5.0, self._runtime.settings.coding_worker_stall_seconds / 3))
        while True:
            await asyncio.sleep(interval)
            await self._store.heartbeat(task_id)

    async def _run_task(self, task: CodingTask) -> ConversationContext:
        context = self._context_from_checkpoint(
            task,
            blocked_tools=self._runtime.blocked_tools(task.guild_id or "", task.channel_id),
            tool_configs=self._runtime.tool_configs(self._coding_registry.config_specs()),
        )
        checkpoint_history = context.get_history()
        raw_event_cursor = task.checkpoint.get("event_cursor", 0)
        event_cursor = int(raw_event_cursor) if isinstance(raw_event_cursor, int | float) else 0
        usage_calls: list[LLMUsageCall] = []
        persisted_usage_count = 0
        usage_lock = asyncio.Lock()

        @asynccontextmanager
        async def child_activity(_user_id: str):
            async with self._runtime.user_activity(task.user_id):
                async with self._runtime.jobs.workspace_child_activity(task.workspace_key):
                    yield

        async def flush_usage(calls: list[LLMUsageCall]) -> None:
            nonlocal persisted_usage_count
            async with usage_lock:
                usage_calls[:] = calls
                end = len(usage_calls)
                if persisted_usage_count >= end:
                    return
                pending = usage_calls[persisted_usage_count:end]
                try:
                    await self._runtime.usage_store.record_turn(
                        user_id=task.user_id,
                        user_name=task.user_name,
                        channel_id=task.channel_id,
                        guild_id=task.guild_id,
                        calls=[
                            price_usage_call(call, self._runtime.model_config) for call in pending
                        ],
                        turn_id=f"coding:{task.id}",
                    )
                except Exception:
                    logger.warning("Coding task usage ledger write failed", exc_info=True)
                    return
                persisted_usage_count = end

        async def checkpoint(
            messages: list[ConversationMessage],
            provider_state: dict[str, Any],
            plan: list[dict[str, str]],
        ) -> None:
            nonlocal event_cursor
            await self._store.set_checkpoint(
                task.id,
                {
                    "messages": [
                        self._serialize_message(message)
                        for message in [*checkpoint_history, *messages]
                    ],
                    "provider_state": provider_state,
                    "event_cursor": event_cursor,
                    "trust_tier": self._trust_tier_from_checkpoint(task).value,
                    "delivery": {
                        "output_files": list(context.pending_output_files),
                        "allowed_file_roots": list(context.pending_allowed_file_roots),
                    },
                },
            )
            if plan:
                await self._store.set_plan(task.id, plan)
            await self._notify_id(task.id)

        async def external_messages() -> list[str]:
            nonlocal event_cursor
            new_events = await self._store.events(task.id, after_id=event_cursor)
            if new_events:
                event_cursor = new_events[-1].id
            messages: list[str] = []
            for event in new_events:
                message = str(event.payload.get("message", "")).strip()
                if not message:
                    continue
                if event.kind == "steering":
                    messages.append(f"Additional instruction from the user: {message}")
                elif event.kind == "recovered":
                    messages.append(f"Recovery safety notice: {message}")
            return messages

        try:
            await self._notify_id(task.id)
            remaining = max(0.1, task.deadline_at - time.time())
            if remaining <= 0.1:
                await self._store.finish(
                    task.id,
                    CodingTaskStatus.TIMED_OUT,
                    error_text="The coding task reached its total time limit.",
                )
                return context
            provider = self._runtime.provider_manager.resolve(
                "coding",
                Scope(
                    guild_id=task.guild_id,
                    channel_id=task.channel_id,
                    user_id=task.user_id,
                    command="coding",
                ),
            )
            result = await run_conversation(
                ConversationRunRequest(
                    user_message=self._task_prompt(task),
                    context=context,
                    trust_tier=self._trust_tier_from_checkpoint(task),
                    user_name=task.user_name,
                    user_id=task.user_id,
                    provider=provider,
                    registry=self._coding_registry,
                    max_iterations=self._runtime.settings.coding_task_max_iterations,
                    max_tokens=self._runtime.settings.react_max_tokens,
                    temperature=self._runtime.settings.react_temperature,
                    channel_name="coding task",
                    guild_id=task.guild_id,
                    channel_id=task.channel_id,
                    thread_id=task.thread_id,
                    bot_name=self._runtime.settings.bot_name,
                    command_template="coding",
                    llm_semaphore=self._runtime.llm_semaphore,
                    provider_state=dict(task.checkpoint.get("provider_state") or {}),
                    compactor=self._runtime.compactor,
                    usage_store=self._runtime.usage_store,
                    timeout_seconds=min(remaining, self._runtime.settings.coding_task_max_seconds),
                    turn_id=f"coding:{task.id}",
                    checkpoint_sink=checkpoint,
                    external_messages_source=external_messages,
                    provider_call_timeout_seconds=(
                        self._runtime.settings.coding_provider_call_timeout_seconds
                    ),
                    usage_sink=usage_calls,
                    usage_checkpoint=flush_usage,
                    user_activity=child_activity,
                    workspace_lock_held=True,
                    resume_output_files=True,
                )
            )
            await flush_usage(result.llm_calls)
            current = await self._store.get_task(task.id)
            if current is not None and current.status == CodingTaskStatus.WAITING_FOR_INPUT:
                await self._runtime.jobs.cancel_task(task.id)
                return context
            # Jobs are application-owned children, not part of the model call.
            # Never let a final response, timeout, or iteration exit leave one
            # mutating the workspace after the task becomes terminal.
            await self._runtime.jobs.cancel_task(task.id)
            async with self._runtime.jobs.workspace_sweep_barrier():
                snapshot_task = asyncio.create_task(
                    asyncio.to_thread(
                        self._snapshot_delivery_outputs,
                        task,
                        list(context.pending_output_files),
                        list(context.pending_allowed_file_roots),
                    ),
                    name=f"coding_delivery_snapshot:{task.id}",
                )
                try:
                    snapshot_files, snapshot_roots = await asyncio.shield(snapshot_task)
                    context.pending_output_files = snapshot_files
                    context.pending_allowed_file_roots = snapshot_roots
                except asyncio.CancelledError as cancellation:
                    while not snapshot_task.done():
                        try:
                            await asyncio.shield(snapshot_task)
                        except asyncio.CancelledError:
                            continue
                    with contextlib.suppress(Exception):
                        snapshot_task.result()
                    raise cancellation
                except Exception:
                    logger.warning(
                        "Could not snapshot coding task %s output attachments",
                        task.id,
                        exc_info=True,
                    )
                    context.pending_output_files = []
                    context.pending_allowed_file_roots = []
            latest = await self._store.get_task(task.id)
            delivery_checkpoint = dict(latest.checkpoint if latest is not None else {})
            delivery_checkpoint["delivery"] = {
                "output_files": list(context.pending_output_files),
                "allowed_file_roots": list(context.pending_allowed_file_roots),
            }
            await self._store.set_checkpoint(task.id, delivery_checkpoint)
            current = await self._store.get_task(task.id)
            if current is not None and current.cancel_requested:
                await self._store.finish(task.id, CodingTaskStatus.CANCELLED)
            elif result.timed_out:
                await self._store.finish(
                    task.id, CodingTaskStatus.TIMED_OUT, error_text=result.text
                )
            elif result.termination_reason != "completed":
                await self._store.finish(
                    task.id,
                    CodingTaskStatus.FAILED,
                    error_text=result.text,
                )
            else:
                await self._store.finish(
                    task.id, CodingTaskStatus.COMPLETED, result_text=result.text
                )
            return context
        except asyncio.CancelledError:
            current = await self._store.get_task(task.id)
            if current is not None and current.cancel_requested:
                await self._store.finish(task.id, CodingTaskStatus.CANCELLED)
            else:
                await self._store.transition_active_status(
                    task.id,
                    CodingTaskStatus.RECOVERING,
                    from_statuses=frozenset(
                        {
                            CodingTaskStatus.RUNNING,
                            CodingTaskStatus.WAITING_FOR_JOB,
                            CodingTaskStatus.WAITING_FOR_INPUT,
                        }
                    ),
                )
            await self._runtime.jobs.cancel_task(task.id)
            raise
        except Exception as exc:
            logger.exception("Coding task %s failed", task.id)
            await self._runtime.jobs.cancel_task(task.id)
            await self._store.finish(
                task.id,
                CodingTaskStatus.FAILED,
                error_text=f"Coding worker failed: {type(exc).__name__}",
            )
            return context

    def _snapshot_delivery_outputs(
        self,
        task: CodingTask,
        output_files: list[str],
        allowed_roots: list[str],
    ) -> tuple[list[str], list[str]]:
        """Copy queued outputs to immutable, task-owned retry artifacts."""

        roots = [Path(value).resolve(strict=False) for value in allowed_roots]
        sources: list[Path] = []
        for raw in output_files:
            raw_path = Path(raw)
            if raw_path.is_symlink():
                continue
            source = raw_path.resolve(strict=True)
            if (
                source.is_file()
                and not source.is_symlink()
                and any(source.is_relative_to(root) for root in roots)
            ):
                sources.append(source)
        if not sources:
            return [], []
        destination_root = self._runtime.workspace_manager.generated_job_dir(
            f"coding-delivery-{task.id}",
            uuid.uuid4().hex,
            owner_user_id=task.user_id,
        )
        snapshots: list[str] = []
        for index, source in enumerate(sources, start=1):
            destination = destination_root / f"{index:02d}-{source.name}"
            # Retain the mode but intentionally give the durable retry copy a
            # fresh mtime; copy2 would inherit an old source mtime and make the
            # sweeper delete a just-created delivery artifact immediately.
            shutil.copy(source, destination, follow_symlinks=False)
            snapshots.append(str(destination.resolve()))
        return snapshots, [str(destination_root.resolve())]

    async def _resolve_control_task(
        self, ctx: MessageContext, task_id: str | None
    ) -> CodingTask | None:
        if task_id:
            return await self._authorized_task(ctx, task_id)
        candidates = await self._store.list_active(user_id=ctx.user_id, root_key=ctx.context_key)
        if not candidates:
            candidates = await self._store.list_active(
                user_id=ctx.user_id, channel_id=ctx.channel_id
            )
        return candidates[0] if len(candidates) == 1 else None

    async def _authorized_task(self, ctx: MessageContext, task_id: str) -> CodingTask | None:
        task = await self._store.get_task(task_id)
        if task is None:
            return None
        if task.user_id != ctx.user_id:
            same_guild_staff = (
                ctx.trust_tier >= TrustTier.STAFF
                and task.guild_id is not None
                and task.guild_id == ctx.guild_id
            )
            globally_configured_staff = ctx.user_id in self._runtime.settings.staff_ids
            if not same_guild_staff and not globally_configured_staff:
                return None
        return task

    async def _notify_id(self, task_id: str, context: ConversationContext | None = None) -> None:
        task = await self._store.get_task(task_id)
        if task is not None:
            await self._notify(task, context)

    async def _notify(self, task: CodingTask, context: ConversationContext | None = None) -> None:
        terminal = task.status not in ACTIVE_TASK_STATUSES
        elapsed = time.monotonic() - self._last_published.get(task.id, 0.0)
        interval = self._runtime.settings.coding_status_min_interval_seconds
        pending = self._publishers.pop(task.id, None) if terminal else self._publishers.get(task.id)
        if pending is not None and terminal:
            pending.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pending
        if not terminal and elapsed < interval:
            if pending is None or pending.done():
                publisher = asyncio.create_task(
                    self._publish_after(task.id, interval - elapsed),
                    name=f"coding_status:{task.id}",
                )
                self._publishers[task.id] = publisher
            return
        failure_reason = "Discord delivery did not complete"
        try:
            await self._publish_with_activity(task, context)
            self._last_published[task.id] = time.monotonic()
        except Exception as exc:
            failure_reason = f"Coding delivery notifier raised {type(exc).__name__}"
            logger.warning("Could not publish coding task %s status", task.id, exc_info=True)
        if terminal:
            refreshed = await self._store.get_task(task.id)
            if (
                refreshed is not None
                and refreshed.final_discord_message_id is None
                and refreshed.delivery_state == "final_pending"
            ):
                await self._store.record_delivery_failure(task.id, failure_reason)

    async def _publish_after(self, task_id: str, delay: float) -> None:
        try:
            await asyncio.sleep(max(0.0, delay))
            task = await self._store.get_task(task_id)
            if task is not None:
                await self._publish_with_activity(task, None)
                self._last_published[task.id] = time.monotonic()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Could not publish coding task %s status", task_id, exc_info=True)
        finally:
            self._publishers.pop(task_id, None)

    async def _publish_with_activity(
        self, task: CodingTask, context: ConversationContext | None
    ) -> None:
        async with self._runtime.user_activity(task.user_id):
            await self._runtime.notifier(task, context)

    @staticmethod
    def _task_payload(task: CodingTask | None) -> dict[str, object]:
        if task is None:
            return {}
        return {
            "task_id": task.id,
            "status": task.status.value,
            "objective": task.objective,
            "plan": task.plan,
            "milestone": task.milestone,
            "result": task.result_text,
            "error": task.error_text,
            "cancel_requested": task.cancel_requested,
            "delivery_state": task.delivery_state,
            "delivery_retry": task.checkpoint.get("delivery_retry", {}),
        }

    @staticmethod
    def _task_prompt(task: CodingTask) -> str:
        criteria = "\n".join(f"- {item}" for item in task.acceptance_criteria)
        context = f"\n\nAdditional context:\n{task.context_text}" if task.context_text else ""
        recovery = (
            "\n\nThis task is resuming after an interruption. Inspect the workspace before "
            "continuing and do not replay an uncertain command."
            if any(
                key in task.checkpoint
                for key in ("messages", "provider_state", "event_cursor", "delivery")
            )
            else ""
        )
        return (
            f"Coding task:\n{task.objective}"
            f"\n\nAcceptance criteria:\n{criteria or '- Satisfy the requested outcome.'}"
            f"{context}{recovery}"
        )

    @classmethod
    def _context_from_checkpoint(
        cls,
        task: CodingTask,
        *,
        blocked_tools: frozenset[str] = frozenset(),
        tool_configs: dict[str, dict[str, Any]] | None = None,
    ) -> ConversationContext:
        raw_messages = task.checkpoint.get("messages")
        messages = (
            [cls._deserialize_message(value) for value in raw_messages if isinstance(value, dict)]
            if isinstance(raw_messages, list)
            else []
        )
        context = ConversationContext(
            key=f"coding:{task.id}",
            db_conversation_id=task.conversation_id or 0,
            messages=messages,
            max_history=200,
            user_id=task.user_id,
            user_name=task.user_name,
            channel_name="coding task",
            blocked_tools=blocked_tools,
            tool_configs=tool_configs or {},
        )
        delivery = task.checkpoint.get("delivery")
        if isinstance(delivery, dict):
            output_files = delivery.get("output_files")
            allowed_roots = delivery.get("allowed_file_roots")
            if isinstance(output_files, list):
                context.pending_output_files = [
                    str(value) for value in output_files if isinstance(value, str)
                ]
            if isinstance(allowed_roots, list):
                context.pending_allowed_file_roots = [
                    str(value) for value in allowed_roots if isinstance(value, str)
                ]
        return context

    @staticmethod
    def _trust_tier_from_checkpoint(task: CodingTask) -> TrustTier:
        raw = task.checkpoint.get("trust_tier")
        if isinstance(raw, str):
            with contextlib.suppress(ValueError):
                return TrustTier(raw)
        # Tasks created before trust was recorded get the lowest usable tier.
        return TrustTier.MEMBER

    @staticmethod
    def _serialize_message(message: ConversationMessage) -> dict[str, Any]:
        return {
            "role": message.role,
            "content": [
                {
                    "type": part.type.value,
                    "text": part.text,
                    "image_url": part.image_url,
                    "media_type": part.media_type,
                    "detail": part.detail,
                }
                for part in message.content
            ],
            "tool_call_id": message.tool_call_id,
            "tool_name": message.tool_name,
            "tool_calls": [
                {"id": call.id, "name": call.name, "arguments": call.arguments}
                for call in message.tool_calls
            ],
        }

    @staticmethod
    def _deserialize_message(value: dict[str, Any]) -> ConversationMessage:
        content = [
            ContentPart.from_text(str(part.get("text", "")))
            for part in value.get("content", [])
            if isinstance(part, dict) and part.get("type") == "text"
        ]
        tool_calls: list[ToolCall] = []
        for call in value.get("tool_calls", []):
            if not isinstance(call, dict):
                continue
            raw_arguments = call.get("arguments")
            arguments = dict(raw_arguments) if isinstance(raw_arguments, dict) else {}
            tool_calls.append(
                ToolCall(
                    id=str(call.get("id", "")),
                    name=str(call.get("name", "")),
                    arguments=arguments,
                )
            )
        role = str(value.get("role", "assistant"))
        if role not in {"user", "assistant", "tool"}:
            role = "assistant"
        narrowed_role = cast(Literal["user", "assistant", "tool"], role)
        return ConversationMessage(
            role=narrowed_role,
            content=content,
            tool_call_id=(
                str(value["tool_call_id"]) if value.get("tool_call_id") is not None else None
            ),
            tool_name=str(value["tool_name"]) if value.get("tool_name") is not None else None,
            tool_calls=tool_calls,
        )
