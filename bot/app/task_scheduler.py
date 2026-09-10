"""Runner lease, occurrence admission, and cancellation lifecycle."""

from __future__ import annotations
from dataclasses import dataclass
from typing import Literal
from storage.task_types import TaskRecord, DueTask
import asyncio
import logging
import time
from tools.scheduled_tasks import TaskDefinition
from utils.plugin_privacy import PrivacyDeletionCallbackResult, PrivacyDeletionScope
from app.task_runtime import ScheduledTaskRuntime, ActiveRuns
from app.task_reads import TaskReadError, retry_read
from tools.registry import MessageContext

from app.task_authority import TaskAuthority

from app.task_executor import TaskExecutor

from app.task_publisher import TaskPublisher

from app.task_approvals import TaskApprovals

log = logging.getLogger(__name__)


@dataclass
class Admission:
    candidate: DueTask
    lane: Literal["llm", "python", "waiting"]
    handoff_reserved: bool = False
    ready: asyncio.Future[None] | None = None


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
        self._admitted: dict[str, Admission] = {}
        self._owner_turn: dict[str, int] = {}
        self._turn = 0
        self._wakeup = asyncio.Event()
        self._llm_limit = runtime.settings.scheduled_task_llm_max_concurrency
        self._python_limit = runtime.settings.scheduled_task_python_max_concurrency

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
        self._admitted.clear()
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
            self._workers.pop(task_id, None)
            self._admitted.pop(task_id, None)
            self._wakeup.set()

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

    async def _stop_workers(self) -> None:
        tasks: list[asyncio.Task[None] | asyncio.Task[bool]] = [*self._workers.values()]
        if self._publisher is not None:
            tasks.append(self._publisher)
        if self._approval_worker is not None:
            tasks.append(self._approval_worker)
        for worker in tasks:
            worker.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._workers.clear()
        self._admitted.clear()

    def _slots_used(self, lane: str) -> int:
        return sum(admission.lane == lane for admission in self._admitted.values())

    async def admit_due(self, now: float) -> None:
        # The loop is the sole admission writer. Workers only yield/release their
        # existing reservations and wake it. Last admission rotates busy owners
        # behind others, while timestamps preserve order within an owner.
        candidates = await self.r.store.due_candidates(now)
        candidates.extend(
            admission.candidate
            for admission in self._admitted.values()
            if admission.lane == "waiting"
        )
        candidates.sort(
            key=lambda item: (
                self._owner_turn.get(item["owner_id"], 0),
                item["next_run"],
                item["id"],
            )
        )
        for candidate in candidates:
            task_id, owner_id = candidate["id"], candidate["owner_id"]
            admitted = self._admitted.get(task_id)
            if admitted is not None:
                if admitted.lane != "waiting" or self._slots_used("llm") >= self._llm_limit:
                    continue
                assert admitted.ready is not None
                if admitted.ready.done():
                    continue
                admitted.lane = "llm"
                admitted.handoff_reserved = False
                admitted.ready.set_result(None)
                self._wakeup.set()  # A previously skipped gate can now reserve handoff capacity.
            else:
                if any(item.candidate["owner_id"] == owner_id for item in self._admitted.values()):
                    continue
                lane: Literal["llm", "python"] = (
                    "llm" if candidate["execution"] == "llm" else "python"
                )
                limit = self._llm_limit if lane == "llm" else self._python_limit
                if self._slots_used(lane) >= limit:
                    continue
                gate = candidate["execution"] == "python_gate"
                if (
                    gate
                    and sum(item.handoff_reserved for item in self._admitted.values())
                    >= self._python_limit
                ):
                    continue
                self._admitted[task_id] = Admission(candidate, lane, gate)
                self._workers[task_id] = asyncio.create_task(self.run(task_id))
            self._turn += 1
            self._owner_turn[owner_id] = self._turn
        # Forget owners once they have neither due nor admitted work.
        owners = {item["owner_id"] for item in candidates} | {
            item.candidate["owner_id"] for item in self._admitted.values()
        }
        self._owner_turn = {
            owner: turn for owner, turn in self._owner_turn.items() if owner in owners
        }

    async def _handoff(self, task_id: str) -> None:
        admission = self._admitted[task_id]
        assert admission.lane == "python" and admission.handoff_reserved
        admission.lane = "waiting"
        admission.ready = asyncio.get_running_loop().create_future()
        self._wakeup.set()
        await admission.ready

    async def loop(self) -> None:
        last_pruned = 0.0
        while True:
            self._wakeup.clear()
            try:
                if not await self.r.store.lease(self.authority.token, time.time()):
                    self._owns_lease = False
                    await self._stop_workers()
                else:
                    if not self._owns_lease:
                        await self.r.store.recover()
                        self._owns_lease = True
                    await self.admit_due(time.time())
                    if self._publisher is None or self._publisher.done():
                        if self._publisher is not None:
                            await asyncio.gather(self._publisher, return_exceptions=True)
                        self._publisher = asyncio.create_task(self.publisher.deliver_pending())
                    if self._approval_worker is None or self._approval_worker.done():
                        if self._approval_worker is not None:
                            await asyncio.gather(self._approval_worker, return_exceptions=True)
                        self._approval_worker = asyncio.create_task(self.approvals.reconcile())
                    if time.monotonic() - last_pruned >= 3600:
                        await self.r.store.prune()
                        last_pruned = time.monotonic()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Scheduled task tick failed")
            try:
                async with asyncio.timeout(5):
                    await self._wakeup.wait()
            except TimeoutError:
                pass

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

                async def preflight() -> MessageContext:
                    assert task is not None
                    owner = await self.r.access.context(
                        task["guild_id"], task["owner_id"], task["channel_id"], run_id=run_id
                    )
                    owner = await self.authority.fresh(owner)
                    await self.authority.validate_definition(owner, definition)
                    return owner

                ctx = await retry_read(preflight)
                if definition.schedule.missed == "skip" and now - (task["next_run"] or now) > 60:
                    await self.publisher.finish(
                        task,
                        run_id,
                        "no_change",
                        "Missed occurrence skipped",
                        task["state"],
                        [],
                        recover_reads=False,
                    )
                    return
                await self.executor.execute(
                    task,
                    run_id,
                    ctx,
                    definition,
                    before_handoff=(lambda: self._handoff(task_id))
                    if task_id in self._admitted
                    else None,
                )
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
                    task,
                    run_id,
                    "read_failed" if isinstance(exc, TaskReadError) and exc.retryable else "failed",
                    str(exc)[:1000],
                    task["state"],
                    [],
                )
        finally:
            if run_id:
                self.runs.pop(run_id, None)
            self._workers.pop(task_id, None)
            self._admitted.pop(task_id, None)
            self._wakeup.set()

    async def start(self) -> None:
        if self._loop_task is None:
            self._loop_task = asyncio.create_task(self.loop(), name="scheduled-tasks")
