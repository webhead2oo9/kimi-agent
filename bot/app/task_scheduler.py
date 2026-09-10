"""Runner lease, occurrence admission, and cancellation lifecycle."""

from __future__ import annotations
from storage.task_types import TaskRecord
import asyncio
import logging
import time
from tools.scheduled_tasks import TaskDefinition
from utils.plugin_privacy import PrivacyDeletionCallbackResult, PrivacyDeletionScope
from app.task_runtime import ScheduledTaskRuntime, ActiveRuns

from app.task_authority import TaskAuthority

from app.task_executor import TaskExecutor

from app.task_publisher import TaskPublisher

from app.task_approvals import TaskApprovals

log = logging.getLogger(__name__)


class TaskScheduler:
    def __init__(
        self,
        runtime: ScheduledTaskRuntime,
        authority: TaskAuthority,
        runs: ActiveRuns,
        executor: TaskExecutor,
        publisher: TaskPublisher,
        approvals: TaskApprovals,
    ) -> None:
        self.r, self.authority, self.runs = runtime, authority, runs
        self.executor, self.publisher, self.approvals = executor, publisher, approvals
        self._workers: dict[str, asyncio.Task[None]] = {}
        self._loop_task: asyncio.Task[None] | None = None
        self._publisher: asyncio.Task[None] | None = None
        self._approval_worker: asyncio.Task[bool] | None = None
        self._owns_lease = False

    async def close(self) -> None:
        await self.executor.close()
        if self._loop_task is not None:
            self._loop_task.cancel()
            await asyncio.gather(self._loop_task, return_exceptions=True)
            self._loop_task = None
        for worker in list(self._workers.values()):
            worker.cancel()
        await asyncio.gather(*self._workers.values(), return_exceptions=True)
        self._workers.clear()
        if self._approval_worker is not None:
            self._approval_worker.cancel()
            await asyncio.gather(self._approval_worker, return_exceptions=True)
            self._approval_worker = None
        if self._publisher is not None:
            self._publisher.cancel()
            await asyncio.gather(self._publisher, return_exceptions=True)
            self._publisher = None
        if self._owns_lease:
            await self.r.store.release(self.authority.token)
        self._owns_lease = False

    async def cancel(self, task_id: str) -> None:
        await self.executor.cancel_preview(task_id)
        worker = self._workers.get(task_id)
        if worker is not None:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    async def delete_user(
        self, user_id: str, scope: PrivacyDeletionScope
    ) -> PrivacyDeletionCallbackResult:
        ids = await self.r.store.owner_tasks(user_id)
        for task_id in ids:
            await self.cancel(task_id)
        await self.r.store.delete_owner(user_id)
        await self.r.store.clear_owner_wizards(user_id)
        return PrivacyDeletionCallbackResult(
            True, (f"Deleted {len(ids)} scheduled task(s), skills, and run records.",)
        )

    async def loop(self) -> None:
        while True:
            try:
                if not await self.r.store.lease(self.authority.token, time.time()):
                    self._owns_lease = False
                    for worker in list(self._workers.values()):
                        worker.cancel()
                    if self._publisher is not None:
                        self._publisher.cancel()
                    if self._approval_worker is not None:
                        self._approval_worker.cancel()
                else:
                    if not self._owns_lease:
                        await self.r.store.recover()
                        self._owns_lease = True
                    for task_id in await self.r.store.due(time.time()):
                        if len(self._workers) >= 2:
                            break
                        if task_id not in self._workers:
                            self._workers[task_id] = asyncio.create_task(self.run(task_id))
                    if self._publisher is None or self._publisher.done():
                        if self._publisher is not None:
                            await asyncio.gather(self._publisher, return_exceptions=True)
                        self._publisher = asyncio.create_task(self.publisher.deliver_pending())
                    if self._approval_worker is None or self._approval_worker.done():
                        if self._approval_worker is not None:
                            await asyncio.gather(self._approval_worker, return_exceptions=True)
                        self._approval_worker = asyncio.create_task(self.approvals.reconcile())
                    await self.r.store.prune()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Scheduled task tick failed")
            await asyncio.sleep(5)

    async def run(self, task_id: str) -> None:
        run_id: str | None = None
        task: TaskRecord | None = None
        try:
            task = await self.r.store.get(task_id, active=True)
            definition = TaskDefinition.model_validate(task["definition"])
            now = time.time()
            run_id = await self.r.store.claim(task, definition.schedule.next_after(now))
            if run_id is None:
                return
            self.runs.register(run_id, task)
            async with self.r.privacy.activity(task["owner_id"]):
                ctx = await self.r.access.context(
                    task["guild_id"], task["owner_id"], task["channel_id"], run_id=run_id
                )
                ctx = await self.authority.fresh(ctx)
                await self.authority.validate_definition(ctx, definition)
                if definition.schedule.missed == "skip" and now - (task["next_run"] or now) > 60:
                    await self.publisher.finish(
                        task, run_id, "no_change", "Missed occurrence skipped", task["state"], []
                    )
                    return
                await self.executor.execute(task, run_id, ctx, definition)
        except asyncio.CancelledError:
            if run_id and task:
                await self.publisher.finish(
                    task,
                    run_id,
                    "failed",
                    "Run interrupted; inspect before retrying",
                    task["state"],
                    [],
                )
            raise
        except Exception as exc:
            log.exception("Scheduled task %s failed", task_id)
            if run_id and task:
                await self.publisher.finish(
                    task, run_id, "failed", str(exc)[:1000], task["state"], []
                )
        finally:
            if run_id:
                self.runs.pop(run_id, None)
            self._workers.pop(task_id, None)

    async def start(self) -> None:
        if self._loop_task is None:
            self._loop_task = asyncio.create_task(self.loop(), name="scheduled-tasks")
