from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest

from app import coding_jobs as coding_jobs_module
from app.coding_jobs import CodingJobManager
from sandbox import workspace_quota
from sandbox.runner import SandboxConfig, SandboxResult
from sandbox.workspace_quota import cleanup_quota_created_entries, snapshot_workspace
from storage.coding_tasks import (
    CodingJobStatus,
    CodingTask,
    CodingTaskQueueFull,
    CodingTaskStatus,
    CodingTaskStore,
)
from storage.db import Database
from storage.usage import UsageStore
from tools.code_exec import CodeExecRuntimeGuards
from tools.workspace.common import UserLocks
from workspace import WorkspaceKey, WorkspaceManager


_requires_dir_fd = pytest.mark.skipif(
    os.name != "posix" or not os.supports_dir_fd,
    reason="quota cleanup requires POSIX dir_fd operations",
)


async def _create_task(
    store: CodingTaskStore,
    *,
    root_key: str,
    workspace_key: str,
) -> CodingTask:
    return await store.create_task(
        conversation_id=None,
        root_key=root_key,
        workspace_key=workspace_key,
        user_id="u1",
        user_name="User",
        guild_id="g1",
        channel_id="c1",
        thread_id=None,
        trigger_discord_message_id=f"message-{root_key}",
        objective="Fix the project",
        acceptance_criteria=[],
        context_text="",
        max_seconds=60,
    )


@pytest.mark.asyncio
async def test_paused_resume_queue_cap_is_atomic_across_database_connections(tmp_path) -> None:
    database_path = tmp_path / "bot.db"
    first_db = Database(database_path)
    second_db = Database(database_path)
    await first_db.connect()
    await second_db.connect()
    try:
        first_store = CodingTaskStore(first_db)
        second_store = CodingTaskStore(second_db)
        first = await _create_task(
            first_store,
            root_key="first",
            workspace_key="u1__g1",
        )
        second = await _create_task(
            first_store,
            root_key="second",
            workspace_key="u1__g2",
        )
        assert await first_store.transition_active_status(
            first.id,
            CodingTaskStatus.WAITING_FOR_INPUT,
            from_statuses=frozenset({CodingTaskStatus.QUEUED}),
        )
        assert await first_store.transition_active_status(
            second.id,
            CodingTaskStatus.WAITING_FOR_INPUT,
            from_statuses=frozenset({CodingTaskStatus.QUEUED}),
        )

        results = await asyncio.gather(
            first_store.steer_active_task(
                first.id,
                "first steering",
                max_queued_per_user=1,
                max_queued_per_workspace=1,
            ),
            second_store.steer_active_task(
                second.id,
                "second steering",
                max_queued_per_user=1,
                max_queued_per_workspace=1,
            ),
            return_exceptions=True,
        )

        accepted = [result for result in results if isinstance(result, CodingTask)]
        rejected = [result for result in results if isinstance(result, CodingTaskQueueFull)]
        assert len(accepted) == 1
        assert accepted[0].status == CodingTaskStatus.QUEUED
        assert len(rejected) == 1 and rejected[0].scope == "user"
        refreshed = [await first_store.get_task(first.id), await first_store.get_task(second.id)]
        assert {task.status for task in refreshed if task is not None} == {
            CodingTaskStatus.QUEUED,
            CodingTaskStatus.WAITING_FOR_INPUT,
        }
        events = [*(await first_store.events(first.id)), *(await first_store.events(second.id))]
        assert [event.kind for event in events].count("steering") == 1
    finally:
        await second_db.close()
        await first_db.close()


@_requires_dir_fd
def test_incomplete_snapshot_removes_new_environment_root_without_walking_ordinary(
    tmp_path: Path,
) -> None:
    manager = WorkspaceManager(tmp_path / "workspaces")
    workspace_key = WorkspaceKey("u1__g1")
    root = manager.user_files_dir(workspace_key)
    (root / "one.txt").write_text("one", encoding="utf-8")
    (root / "two.txt").write_text("two", encoding="utf-8")
    before, complete = snapshot_workspace(
        manager,
        workspace_key,
        max_workspace_files=1,
        max_env_roots=10,
    )
    assert complete is False
    env = root / ".venv"
    env.mkdir()
    (env / "package.bin").write_bytes(b"package")
    uncertain = root / "uncertain-new.txt"
    uncertain.write_text("preserve", encoding="utf-8")

    cleanup = cleanup_quota_created_entries(
        manager,
        workspace_key,
        before,
        remove_preexisting_envs=True,
        remove_new_ordinary=False,
    )

    assert not env.exists()
    assert uncertain.read_text(encoding="utf-8") == "preserve"
    assert cleanup.removed_env_dirs == 1
    assert cleanup.complete is True


@_requires_dir_fd
@pytest.mark.asyncio
async def test_partial_filesystem_cleanup_is_reported_as_incomplete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    database = Database(tmp_path / "bot.db")
    await database.connect()
    try:
        store = CodingTaskStore(database)
        task = await _create_task(store, root_key="root", workspace_key="u1__g1")
        workspace_manager = WorkspaceManager(tmp_path / "workspaces")
        root = workspace_manager.user_files_dir(WorkspaceKey(task.workspace_key))
        (root / "test.sh").write_text("exit 0", encoding="utf-8")

        async def quota_run(*args: Any, **kwargs: Any) -> SandboxResult:
            del args, kwargs
            residue = root / "residue"
            residue.mkdir()
            (residue / "payload.bin").write_bytes(b"payload")
            return SandboxResult(
                exit_code=1,
                stdout="",
                stderr="Workspace quota exceeded.",
                timed_out=False,
                duration_ms=1,
                quota_exceeded=True,
            )

        def denied_removal(*args: Any, **kwargs: Any):
            del args, kwargs
            raise PermissionError("simulated cleanup denial")

        monkeypatch.setattr(coding_jobs_module, "run_workspace_file_in_sandbox", quota_run)
        monkeypatch.setattr(workspace_quota, "remove_owned_tree_at", denied_removal)
        manager = CodingJobManager(
            store=store,
            workspace_manager=workspace_manager,
            workspace_locks=UserLocks(),
            sandbox_config=SandboxConfig(),
            max_seconds=60,
            max_cpu_seconds=10,
            runtime_guards=CodeExecRuntimeGuards.create(
                max_concurrency=1,
                network_weekly_limit=0,
            ),
            usage_store=UsageStore(database),
        )

        async with manager.workspace_activity(task.workspace_key):
            job_id = await manager.start(
                task_id=task.id,
                workspace_key=task.workspace_key,
                request={"path": "test.sh", "mode": "shell"},
            )
            await manager.wait(job_id, timeout=1)

        job = await store.get_job(job_id)
        assert job is not None and job.status == CodingJobStatus.FAILED
        assert (root / "residue").exists()
        assert "Quota cleanup removed 0 entries" in job.stderr
        assert "Automatic quota cleanup could not be completed." in job.stderr
        assert "simulated cleanup denial" not in job.stderr
        assert "quota cleanup was incomplete" in caplog.text
    finally:
        await database.close()
