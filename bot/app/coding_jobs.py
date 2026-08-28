from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import replace
from functools import partial
from typing import Any, cast

from sandbox.netns_lease import NetnsLeaseSafetyError
from sandbox.runner import (
    SandboxConfig,
    SandboxRunMode,
    run_workspace_file_in_sandbox,
    stop_sandbox_unit,
)
from sandbox.workspace_quota import (
    QuotaCleanup,
    cleanup_quota_created_entries,
    snapshot_workspace,
)
from storage.coding_tasks import ACTIVE_JOB_STATUSES, CodingJobStatus, CodingTaskStore
from storage.usage import UsageStore
from tools.code_exec import CodeExecRuntimeGuards
from tools.workspace import UserLocks
from trust.tiers import TrustTier
from utils.asyncio import await_uncancellable
from workspace import WorkspaceKey, WorkspaceManager

logger = logging.getLogger(__name__)
UserActivityGuard = Callable[[str], AbstractAsyncContextManager[None]]

# How long a netns job waits for the shared namespace after asking the browser
# service to yield. Long enough for a worker teardown, short enough that a
# foreground browser turn owned by someone else fails the job promptly.
CODING_JOB_LEASE_WAIT_SECONDS = 30.0


@asynccontextmanager
async def _noop_user_activity(_user_id: str) -> AsyncIterator[None]:
    yield


async def _run_locked_thread[T](function: Callable[[], T]) -> T:
    """Keep a workspace lock held until its filesystem worker really stops."""
    worker = asyncio.create_task(asyncio.to_thread(function))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError as cancellation:
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
        with contextlib.suppress(Exception):
            worker.result()
        raise cancellation


def _append_job_stderr(stderr: str, note: str) -> str:
    current = stderr.rstrip()
    return f"{current}\n\n{note}" if current else note


def _quota_cleanup_summary(cleanup: QuotaCleanup, *, snapshot_complete: bool) -> str:
    entry_label = "entry" if cleanup.removed_entries == 1 else "entries"
    summary = (
        f"Quota cleanup removed {cleanup.removed_entries} {entry_label} "
        f"({cleanup.removed_bytes} bytes)"
    )
    if cleanup.removed_env_dirs:
        root_label = "root" if cleanup.removed_env_dirs == 1 else "roots"
        summary += f", including {cleanup.removed_env_dirs} environment {root_label}"
    summary += "."
    if not snapshot_complete:
        summary += " Ordinary paths were preserved because the pre-run snapshot was incomplete."
    return summary


class CodingJobManager:
    """Own cancellable sandbox processes addressed by durable job handles."""

    def __init__(
        self,
        *,
        store: CodingTaskStore,
        workspace_manager: WorkspaceManager,
        workspace_locks: UserLocks,
        sandbox_config: SandboxConfig,
        max_seconds: float,
        max_cpu_seconds: int,
        runtime_guards: CodeExecRuntimeGuards,
        usage_store: UsageStore,
        user_activity: UserActivityGuard = _noop_user_activity,
    ) -> None:
        self._store = store
        self._workspace_manager = workspace_manager
        self._workspace_locks = workspace_locks
        self._config = replace(
            sandbox_config,
            wall_timeout_seconds=max_seconds,
            max_cpu_seconds=max_cpu_seconds,
        )
        self._max_seconds = max_seconds
        self._runtime_guards = runtime_guards
        self._usage_store = usage_store
        self._user_activity = user_activity
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._done: dict[str, asyncio.Event] = {}
        self._admission_lock = asyncio.Lock()
        self._closed = False

    async def start(
        self,
        *,
        task_id: str,
        workspace_key: str,
        request: dict[str, Any],
    ) -> str:
        async with self._admission_lock:
            if self._closed:
                raise RuntimeError("coding job service is shutting down")
            job = await self._store.create_job_if_active(task_id, request)
            if job is None:
                raise RuntimeError("coding task is no longer active")
            parent = await self._store.get_task(task_id)
            if parent is None:
                raise RuntimeError("parent coding task no longer exists")
            unit_name = self._unit_name(job.id)
            # Persist the exact manager-owned unit before a launch task exists. A
            # restart can therefore prove it inactive before recovering the writer.
            await self._store.update_job(
                job.id,
                CodingJobStatus.QUEUED,
                unit_name=unit_name,
            )
            done = asyncio.Event()
            self._done[job.id] = done
            worker = asyncio.create_task(
                self._run(
                    job.id,
                    WorkspaceKey(workspace_key),
                    parent.user_id,
                    request,
                    unit_name,
                    done,
                ),
                name=f"coding_job:{job.id}",
            )
            self._tasks[job.id] = worker
            worker.add_done_callback(partial(self._job_done, job_id=job.id))
            return job.id

    def _unit_name(self, job_id: str) -> str:
        suffix = "service" if self._config.network_mode == "netns" else "scope"
        return f"coding-job-{job_id}.{suffix}"

    @property
    def uses_netns(self) -> bool:
        return self._config.network_mode == "netns"

    @asynccontextmanager
    async def _run_lease(self, user_id: str) -> AsyncIterator[None]:
        """Hold the sandbox's process-wide lease for one job.

        Host/none modes keep the fail-fast semaphore check. In netns mode the
        physical namespace is shared with the same user's browser, which keeps
        it until its idle TTL, so a job first asks the browser service to yield
        and then waits a bounded time; ``async with`` on the lease preserves its
        poison-on-safety-error contract.
        """

        if not self.uses_netns:
            semaphore = self._runtime_guards.semaphore
            if semaphore.locked():
                raise RuntimeError(
                    "The shared execution sandbox is busy; retry this coding job later."
                )
            async with semaphore:
                yield
            return
        lease = self._runtime_guards.netns_lease
        # Do not inspect locked() first: the same user's browser can acquire
        # between that observation and our separately scheduled acquire().
        if self._runtime_guards.netns_yield is not None:
            await self._runtime_guards.netns_yield(user_id)
        try:
            await asyncio.wait_for(lease.acquire(), CODING_JOB_LEASE_WAIT_SECONDS)
        except TimeoutError:
            raise RuntimeError(
                "The shared VPN sandbox is busy with the browser or other networked "
                "code; retry this coding job later."
            ) from None
        # Already acquired: re-enter so __aexit__ handles release and poisoning.
        try:
            yield
        except BaseException as exc:
            await await_uncancellable(lease.__aexit__(type(exc), exc, exc.__traceback__))
            raise
        else:
            await await_uncancellable(lease.__aexit__(None, None, None))

    def workspace_activity(self, workspace_key: str) -> AbstractAsyncContextManager[None]:
        return self._workspace_locks.writer(WorkspaceKey(workspace_key))

    def workspace_child_activity(self, workspace_key: str) -> AbstractAsyncContextManager[None]:
        return self._workspace_locks.writer_reference(WorkspaceKey(workspace_key))

    def workspace_sweep_barrier(self) -> AbstractAsyncContextManager[None]:
        return self._workspace_locks.barrier()

    async def wait_workspace_idle(self, workspace_key: str) -> None:
        await self._workspace_locks.wait_writer_idle(WorkspaceKey(workspace_key))

    async def stop_recovered_units(self) -> None:
        """Prove every pre-crash command inactive before tasks are requeued."""

        failures: list[tuple[str, BaseException]] = []
        cancellation: asyncio.CancelledError | None = None
        for job in await self._store.list_active_jobs():
            if job.unit_name is None:
                if job.status == CodingJobStatus.RUNNING:
                    failures.append(
                        (
                            job.id,
                            RuntimeError(
                                f"Running coding job {job.id} has no recoverable systemd unit"
                            ),
                        )
                    )
                continue
            try:
                await stop_sandbox_unit(job.unit_name)
            except asyncio.CancelledError as exc:
                cancellation = cancellation or exc
            except Exception as exc:
                failures.append((job.id, exc))
        if cancellation is not None:
            raise cancellation
        if failures:
            failed_ids = ", ".join(job_id for job_id, _exc in failures)
            raise RuntimeError(
                f"Could not confirm {len(failures)} recovered coding unit(s) inactive: {failed_ids}"
            ) from failures[0][1]

    def _job_done(self, _task: asyncio.Task[None], *, job_id: str) -> None:
        self._tasks.pop(job_id, None)
        self._done.pop(job_id, None)
        with contextlib.suppress(BaseException):
            _task.result()

    async def wait(self, job_id: str, timeout: float = 0) -> None:
        event = self._done.get(job_id)
        if event is None or event.is_set() or timeout <= 0:
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(event.wait(), timeout=min(timeout, self._max_seconds + 5.0))

    async def cancel(self, job_id: str) -> bool:
        job = await self._store.get_job(job_id)
        if job is None:
            return False
        worker = self._tasks.get(job_id)
        if worker is None:
            if job.status in {CodingJobStatus.QUEUED, CodingJobStatus.RUNNING}:
                await self._store.update_job(job_id, CodingJobStatus.INTERRUPTED)
            return True
        worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker
        refreshed = await self._store.get_job(job_id)
        if refreshed is not None and refreshed.status in ACTIVE_JOB_STATUSES:
            await self._store.update_job(job_id, CodingJobStatus.CANCELLED)
        return True

    async def cancel_task(self, task_id: str) -> None:
        # Serialize with start() so cancellation observes either no admitted job
        # or a fully registered worker plus its durable row.
        async with self._admission_lock:
            jobs = [job.id for job in await self._store.list_active_jobs(task_id=task_id)]
        await asyncio.gather(*(self.cancel(job_id) for job_id in jobs))

    async def close(self) -> None:
        async with self._admission_lock:
            self._closed = True
            jobs = list(self._tasks)
        await asyncio.gather(
            *(self.cancel(job_id) for job_id in jobs),
        )

    async def _run(
        self,
        job_id: str,
        workspace_key: WorkspaceKey,
        user_id: str,
        request: dict[str, Any],
        unit_name: str,
        done: asyncio.Event,
    ) -> None:
        try:
            async with self._user_activity(user_id):
                async with self._workspace_locks.writer_reference(workspace_key):
                    await self._run_with_writer(job_id, workspace_key, request, unit_name)
        finally:
            done.set()

    async def _run_with_writer(
        self,
        job_id: str,
        workspace_key: WorkspaceKey,
        request: dict[str, Any],
        unit_name: str,
    ) -> None:
        try:
            await self._store.update_job(job_id, CodingJobStatus.RUNNING, unit_name=unit_name)
            job = await self._store.get_job(job_id)
            if job is None:
                raise RuntimeError("coding job no longer exists")
            task = await self._store.get_task(job.task_id)
            if task is None:
                raise RuntimeError("parent coding task no longer exists")
            async with (
                self._workspace_locks.owned_operation(workspace_key),
                self._run_lease(task.user_id),
            ):
                if self._config.network_mode != "none":
                    quota_error = await self._runtime_guards.reserve_network_run(
                        usage_store=self._usage_store,
                        user_id=task.user_id,
                        user_name=task.user_name,
                        channel_id=task.channel_id,
                        guild_id=task.guild_id,
                        trust_tier=TrustTier.REGULAR,
                        operation="coding_job",
                    )
                    if quota_error is not None:
                        raise RuntimeError(quota_error)
                path_text = str(request.get("path", "")).strip()
                if not path_text:
                    raise ValueError("path is required")
                raw_mode = str(request.get("mode", "")).strip().lower() or "direct"
                if raw_mode not in {"python", "shell", "direct"}:
                    raise ValueError("mode must be python, shell, or direct")
                mode = cast(SandboxRunMode, raw_mode)
                argv_raw = request.get("argv") or []
                if not isinstance(argv_raw, list) or not all(
                    isinstance(value, str) for value in argv_raw
                ):
                    raise ValueError("argv must be a list of strings")
                stdin = request.get("stdin")
                if stdin is not None and not isinstance(stdin, str):
                    raise ValueError("stdin must be a string")
                path = self._workspace_manager.resolve_user_file_path(
                    workspace_key, path_text, must_exist=True
                )
                if path.is_symlink() or not path.is_file():
                    raise ValueError("path is not a regular file")
                before, snapshot_complete = await _run_locked_thread(
                    partial(
                        snapshot_workspace,
                        self._workspace_manager,
                        workspace_key,
                        max_workspace_files=self._config.max_workspace_files,
                        max_env_roots=self._config.max_env_files,
                    )
                )
                result = await run_workspace_file_in_sandbox(
                    self._config,
                    self._workspace_manager.user_files_dir(workspace_key),
                    path,
                    stdin=stdin,
                    mode=mode,
                    argv=tuple(argv_raw),
                    unit_name=unit_name,
                )
                if result.quota_exceeded:
                    try:
                        cleanup = await _run_locked_thread(
                            partial(
                                cleanup_quota_created_entries,
                                self._workspace_manager,
                                workspace_key,
                                before,
                                remove_preexisting_envs=(result.environment_quota_exceeded),
                                remove_new_ordinary=snapshot_complete,
                            )
                        )
                    except Exception:
                        logger.warning(
                            "Coding job %s quota cleanup failed",
                            job_id,
                            exc_info=True,
                        )
                        result = replace(
                            result,
                            stderr=_append_job_stderr(
                                result.stderr,
                                "Automatic quota cleanup could not be completed.",
                            ),
                        )
                    else:
                        cleanup_note = _quota_cleanup_summary(
                            cleanup,
                            snapshot_complete=snapshot_complete,
                        )
                        if not cleanup.complete:
                            logger.warning(
                                "Coding job %s quota cleanup was incomplete",
                                job_id,
                            )
                            cleanup_note += " Automatic quota cleanup could not be completed."
                        result = replace(
                            result,
                            stderr=_append_job_stderr(
                                result.stderr,
                                cleanup_note,
                            ),
                        )
            status = (
                CodingJobStatus.TIMED_OUT
                if result.timed_out
                else CodingJobStatus.SUCCEEDED
                if result.exit_code == 0 and not result.quota_exceeded
                else CodingJobStatus.FAILED
            )
            await self._store.update_job(
                job_id,
                status,
                stdout=result.stdout,
                stderr=result.stderr,
                exit_code=result.exit_code,
                timed_out=result.timed_out,
            )
        except asyncio.CancelledError:
            # sandbox.runner shields teardown until the process group/unit is
            # confirmed inactive, so this state means cancellation is complete.
            await self._store.update_job(job_id, CodingJobStatus.CANCELLED)
            raise
        except NetnsLeaseSafetyError:
            logger.critical(
                "Coding job %s teardown is unconfirmed; workspace remains blocked",
                job_id,
                exc_info=True,
            )
            await self._store.update_job(
                job_id,
                CodingJobStatus.UNSAFE,
                stderr="Sandbox teardown is still being confirmed.",
                unit_name=unit_name,
            )
            await self._confirm_unit_inactive(unit_name)
            await self._store.update_job(
                job_id,
                CodingJobStatus.INTERRUPTED,
                stderr="Sandbox teardown was confirmed after an earlier failure.",
            )
        except Exception as exc:
            logger.warning("Coding job %s failed", job_id, exc_info=True)
            await self._store.update_job(
                job_id,
                CodingJobStatus.FAILED,
                stderr=f"Job could not start: {type(exc).__name__}",
            )

    async def _confirm_unit_inactive(self, unit_name: str) -> None:
        """Retry despite cancellation until the persisted unit is inactive."""

        while True:
            try:
                await stop_sandbox_unit(unit_name)
                return
            except asyncio.CancelledError:
                continue
            except NetnsLeaseSafetyError:
                try:
                    await asyncio.sleep(1.0)
                except asyncio.CancelledError:
                    continue
