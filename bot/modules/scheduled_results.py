"""Bounded delivery of the durable scheduled-result outbox to installed modules."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable

from kimi_agent_module_api import (
    ModulePermissions,
    ScheduledResult,
    ScheduledResultAccessError,
    ScheduledResultAttachment,
    ScheduledResultHandler,
    ScheduledResultMessage,
)
from kimi_agent_module_api.contracts import MessageRef
from kimi_agent_module_api.scheduled_results import (
    MAX_RESULT_FILE_BYTES,
    MAX_RESULT_TOTAL_FILE_BYTES,
    validate_result_read_limit,
    validate_result_subscription,
)
from modules.health import HealthRegistry
from modules.tasks import cancel_with_grace, run_bounded
from storage.task_results import ResultNotification, TaskResultStore
from utils.privacy_barrier import PrivacyDeletionPendingError, UserPrivacyBarrier

log = logging.getLogger(__name__)
type CheckAccess = Callable[[ResultNotification], Awaitable[None]]


class ResultFiles:
    def __init__(
        self, store: TaskResultStore, row: ResultNotification, check: Callable[[], Awaitable[None]]
    ) -> None:
        self.store, self.row, self.check = store, row, check
        self.open = True
        self.remaining = MAX_RESULT_TOTAL_FILE_BYTES
        self.lock = asyncio.Lock()

    async def read(self, attachment_id: str, *, max_bytes: int = MAX_RESULT_FILE_BYTES) -> bytes:
        validate_result_read_limit(max_bytes)
        async with self.lock:
            if not self.open or self.remaining <= 0:
                raise ScheduledResultAccessError(
                    "Result files are closed or the read budget is used"
                )
            await self.check()
            try:
                data = await self.store.read_file(
                    self.row, attachment_id, min(max_bytes, self.remaining)
                )
            except ValueError as exc:
                raise ScheduledResultAccessError(str(exc)) from exc
            # Cancellation/deletion/access changes can happen across either await.
            await self.check()
            if not self.open:
                raise ScheduledResultAccessError("Result files are closed")
            self.remaining -= len(data)
            return data


class ModuleResultView:
    def __init__(
        self,
        runtime: ScheduledResultRuntime,
        module: str,
        permissions: ModulePermissions,
        is_guild_active: Callable[[int], bool],
    ) -> None:
        self.runtime, self.module = runtime, module
        self.permissions, self.is_guild_active = permissions, is_guild_active
        self.handlers: dict[tuple[str, int], ScheduledResultHandler] = {}
        self.open = True

    def _validate(self, name: str, guild_id: int) -> None:
        validate_result_subscription(name, guild_id)
        if not self.open or name not in self.permissions.scheduled_results:
            raise ScheduledResultAccessError(
                "Scheduled-result subscription is closed or undeclared"
            )

    async def subscribe(self, name: str, *, guild_id: int, handler: ScheduledResultHandler) -> None:
        self._validate(name, guild_id)
        await self.runtime.store.subscribe(self.module, name, guild_id)
        self._validate(name, guild_id)
        self.handlers[name, guild_id] = handler

    async def unsubscribe(self, name: str, *, guild_id: int) -> None:
        self._validate(name, guild_id)
        self.handlers.pop((name, guild_id), None)
        await self.runtime.store.unsubscribe(self.module, name, guild_id)


class ScheduledResultRuntime:
    def __init__(
        self,
        store: TaskResultStore,
        *,
        check_access: CheckAccess,
        privacy: UserPrivacyBarrier,
        health: HealthRegistry,
        timeout: float = 30.0,
        cancel_grace: float = 5.0,
    ) -> None:
        self.store, self.check_access = store, check_access
        self.privacy, self.health = privacy, health
        self.timeout, self.cancel_grace = timeout, cancel_grace
        self.views: dict[str, ModuleResultView] = {}
        self._health_counts: dict[str, dict[str, int]] = {}
        self._invocations: dict[str, set[asyncio.Task[None]]] = {}
        self._quarantined: set[str] = set()
        self._dispatch_lock = asyncio.Lock()
        self.worker: asyncio.Task[None] | None = None

    def view_for(
        self, module: str, permissions: ModulePermissions, is_guild_active: Callable[[int], bool]
    ) -> ModuleResultView:
        self.unregister_module(module)
        view = ModuleResultView(self, module, permissions, is_guild_active)
        self.views[module] = view
        return view

    def unregister_module(self, module: str) -> None:
        self._health_counts.pop(module, None)
        view = self.views.pop(module, None)
        if view is not None:
            view.open = False
            view.handlers.clear()

    def start(self) -> None:
        if self.worker is None:
            self.worker = asyncio.create_task(self._loop(), name="module-scheduled-results")

    async def close(self) -> None:
        for module in tuple(self.views):
            self.unregister_module(module)
        if self.worker is not None:
            await cancel_with_grace(
                (self.worker,), grace=self.cancel_grace + 1, what="scheduled-result dispatcher"
            )
            self.worker = None
        await cancel_with_grace(
            [task for tasks in self._invocations.values() for task in tasks],
            grace=self.cancel_grace,
            what="scheduled-result invocations",
        )

    async def _loop(self) -> None:
        while True:
            try:
                await self.dispatch_once()
            except Exception:
                log.exception("Scheduled-result dispatcher failed; retrying independently")
            await asyncio.sleep(2)

    async def dispatch_once(self) -> None:
        async with self._dispatch_lock:
            await self._dispatch_once()

    async def _dispatch_once(self) -> None:
        await self.store.prune()
        # A coroutine that ignores cancellation still owns a real slot and privacy
        # lease. Never accumulate new attempts behind it, including other runs.
        slots = max(0, 4 - sum(len(tasks) for tasks in self._invocations.values()))
        rows = await self.store.claim(limit=slots) if slots else []
        await asyncio.gather(*(self._deliver(row) for row in rows))
        counts = await self.store.health_counts()
        for module in self.views:
            current = counts.get(module, {})
            stuck = len(self._invocations.get(module, ())) if module in self._quarantined else 0
            if stuck:
                current["stuck"] = stuck
            if self._health_counts.get(module) == current:
                continue
            self._health_counts[module] = current
            blocked, retrying = current.get("blocked", 0), current.get("retry", 0)
            self.health.set_constraint(
                module,
                "scheduled_results",
                "failed" if stuck else "degraded" if blocked or retrying else "healthy",
                "Subscriber ignored cancellation; paused until it exits or the host restarts"
                if stuck
                else f"Scheduled results: {blocked} blocked, {retrying} retrying"
                if blocked or retrying
                else "",
                metrics={f"results_{status}": float(count) for status, count in current.items()},
            )

    async def _deliver(self, row: ResultNotification) -> None:
        if row["module"] in self._quarantined:
            await self.store.settle(row, "blocked", "Subscriber still running after cancellation")
            return
        files: ResultFiles | None = None
        status, detail = "retry", "Subscriber processing failed"

        async def invoke() -> None:
            nonlocal files, status, detail
            view = self.views.get(row["module"])
            key = (row["name"], int(row["guild_id"]))
            handler = view.handlers.get(key) if view is not None else None

            def check_module() -> None:
                if (
                    view is None
                    or not view.open
                    or self.views.get(row["module"]) is not view
                    or view.handlers.get(key) is not handler
                    or handler is None
                    or row["name"] not in view.permissions.scheduled_results
                ):
                    raise ScheduledResultAccessError("Subscriber unavailable or permission removed")
                if not view.is_guild_active(key[1]):
                    raise ScheduledResultAccessError("Subscriber inactive in this guild")

            async def check() -> None:
                check_module()
                if not await self.store.live(row):
                    raise ScheduledResultAccessError("Notification deleted, expired, or lease lost")
                try:
                    await self.check_access(row)
                except Exception as exc:
                    raise ScheduledResultAccessError(
                        "Task owner or destination access unavailable"
                    ) from exc
                # Access checks perform I/O: fence module shutdown and deletion again.
                if not await self.store.live(row):
                    raise ScheduledResultAccessError("Notification access changed")
                check_module()

            async with self.privacy.activity(row["owner_id"]):
                try:
                    await check()
                    result = await self._result(row)
                    await check()
                except ScheduledResultAccessError as exc:
                    status, detail = "blocked", str(exc)
                    raise
                assert handler is not None
                files = ResultFiles(self.store, row, check)
                try:
                    await handler(result, files)
                finally:
                    files.open = False
                status, detail = "acknowledged", ""

        invocation = asyncio.create_task(invoke(), name=f"scheduled-result:{row['id']}")
        self._invocations.setdefault(row["module"], set()).add(invocation)
        invocation.add_done_callback(lambda task: self._finished(row["module"], task))

        async def join() -> None:
            await invocation

        try:
            outcome = await run_bounded(
                join(),
                timeout=self.timeout,
                grace=self.cancel_grace,
                what=f"scheduled-result subscriber {row['module']}/{row['name']}",
            )
            if outcome.abandoned:
                status, detail = "blocked", "Subscriber ignored cancellation; processing paused"
            elif outcome.timed_out or outcome.cancelled:
                status, detail = "retry", "Subscriber timed out or cancelled"
            elif isinstance(outcome.error, PrivacyDeletionPendingError):
                status, detail = "blocked", "Owner privacy deletion is pending"
            elif outcome.error is not None and status != "blocked":
                status, detail = "retry", f"Subscriber raised {type(outcome.error).__name__}"
        finally:
            if files is not None:
                files.open = False
            if not invocation.done():
                self._quarantined.add(row["module"])
        await self.store.settle(row, status, detail)

    def _finished(self, module: str, task: asyncio.Task[None]) -> None:
        tasks = self._invocations.get(module)
        if tasks is not None:
            tasks.discard(task)
            if not tasks:
                self._invocations.pop(module, None)
                self._quarantined.discard(module)

    async def _result(self, row: ResultNotification) -> ScheduledResult:
        saved = await self.store.attachments(row["run_id"])
        messages: list[ScheduledResultMessage] = []
        for delivery in await self.store.messages(row["run_id"]):
            payload = json.loads(delivery["payload_json"])
            attachments = tuple(
                ScheduledResultAttachment(
                    str(file_id),
                    saved[str(file_id)]["filename"],
                    saved[str(file_id)]["size_bytes"],
                    saved[str(file_id)]["description"],
                )
                for file_id in payload.get("file_ids", ())
                if str(file_id) in saved
            )
            embed = payload.get("published_embed")
            embed_json = json.dumps(embed, ensure_ascii=False) if embed else None
            embed_unavailable_reason = None
            if embed_json is not None and len(embed_json.encode()) > 32768:
                embed_json = None
                embed_unavailable_reason = "Published embed exceeds the 32 KiB snapshot limit"
            messages.append(
                ScheduledResultMessage(
                    MessageRef(
                        int(row["guild_id"]),
                        int(delivery["channel_id"]),
                        int(delivery["message_id"]),
                    ),
                    payload.get("content", ""),
                    attachments,
                    embed_json,
                    embed_unavailable_reason,
                )
            )
        return ScheduledResult(
            notification_id=row["id"],
            subscription=row["name"],
            task_id=row["task_id"],
            run_id=row["run_id"],
            revision=row["revision"],
            guild_id=int(row["guild_id"]),
            owner_id=int(row["owner_id"]),
            published_at=row["published_at"],
            expires_at=row["expires_at"],
            messages=tuple(messages),
        )
