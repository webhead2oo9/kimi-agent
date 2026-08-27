"""Web tools for the durable coding worker and the netns lease handoff."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast

import pytest

from app.coding_jobs import CodingJobManager
from sandbox.netns_lease import NetnsLease
from sandbox.runner import SandboxConfig
from tools.browser import _acquire_rooted_turn, _browser_session, _turn_id
from tools.code_exec import CodeExecRuntimeGuards
from tools.coding_tasks import CodingTaskControls, build_coding_registry
from tools.registry import MessageContext, ToolRegistry
from tools.workspace.common import UserLocks
from trust.tiers import TrustTier
from web_browser.service import BrowserNetworkMode, BrowserService, BrowserServiceConfig
from workspace import WorkspaceManager


def _worker_context(*, background: bool = True, conversation_id: int | None = 0) -> MessageContext:
    return MessageContext(
        user_id="u1",
        user_name="User",
        guild_id="g1",
        channel_id="c1",
        thread_id=None,
        trust_tier=TrustTier.MEMBER,
        conversation_id=conversation_id,
        context_key="coding:task-1",
        tool_event_turn_id="coding:task-1",
        background_task=background,
    )


class _FakeControls:
    def __init__(self, *, status: str = "succeeded", cancelled: bool = True) -> None:
        self.status = status
        self.cancelled = cancelled
        self.next_job = 1

    async def start_job(self, task_id: str, request: dict[str, object]) -> str:
        job_id = f"job-{self.next_job}"
        self.next_job += 1
        return job_id

    async def job_status(
        self, task_id: str, job_id: str, wait_seconds: float
    ) -> dict[str, object] | None:
        return {"job_id": job_id, "status": self.status}

    async def cancel_job(self, task_id: str, job_id: str) -> bool:
        return self.cancelled


def _registry(controls: _FakeControls, *, netns_jobs: bool) -> ToolRegistry:
    return build_coding_registry(
        ToolRegistry(), cast(CodingTaskControls, controls), netns_jobs=netns_jobs
    )


async def _call(registry: ToolRegistry, name: str, args: dict, ctx: MessageContext) -> dict:
    return json.loads(await registry.dispatch(name, args, ctx))


# --- worker job tools mark the shared namespace as busy ---------------------


@pytest.mark.asyncio
async def test_netns_jobs_toggle_networked_exec_inflight() -> None:
    controls = _FakeControls(status="running")
    registry = _registry(controls, netns_jobs=True)
    ctx = _worker_context()

    await _call(registry, "coding_job_start", {"path": "scripts/test.sh"}, ctx)
    assert ctx.networked_exec_inflight is True

    await _call(registry, "coding_job_status", {"job_id": "job-1", "wait_seconds": 0}, ctx)
    assert ctx.networked_exec_inflight is True, "an active job still holds the namespace"

    controls.status = "failed"
    await _call(registry, "coding_job_status", {"job_id": "job-1", "wait_seconds": 0}, ctx)
    assert ctx.networked_exec_inflight is False


@pytest.mark.asyncio
async def test_job_cancel_clears_networked_exec_inflight() -> None:
    registry = _registry(_FakeControls(status="running"), netns_jobs=True)
    ctx = _worker_context()

    await _call(registry, "coding_job_start", {"path": "scripts/test.sh"}, ctx)
    await _call(registry, "coding_job_cancel", {"job_id": "job-1"}, ctx)

    assert ctx.networked_exec_inflight is False


@pytest.mark.asyncio
async def test_one_terminal_job_does_not_clear_another_active_netns_job() -> None:
    controls = _FakeControls(status="running")
    registry = _registry(controls, netns_jobs=True)
    ctx = _worker_context()

    await _call(registry, "coding_job_start", {"path": "one.sh"}, ctx)
    await _call(registry, "coding_job_start", {"path": "two.sh"}, ctx)
    assert ctx.networked_exec_job_ids == {"job-1", "job-2"}

    controls.status = "succeeded"
    await _call(registry, "coding_job_status", {"job_id": "job-1", "wait_seconds": 0}, ctx)

    assert ctx.networked_exec_job_ids == {"job-2"}
    assert ctx.networked_exec_inflight is True


@pytest.mark.asyncio
async def test_unsuccessful_cancel_keeps_netns_job_active() -> None:
    registry = _registry(_FakeControls(status="running", cancelled=False), netns_jobs=True)
    ctx = _worker_context()

    await _call(registry, "coding_job_start", {"path": "test.sh"}, ctx)
    await _call(registry, "coding_job_cancel", {"job_id": "job-1"}, ctx)

    assert ctx.networked_exec_job_ids == {"job-1"}
    assert ctx.networked_exec_inflight is True


@pytest.mark.asyncio
async def test_host_mode_jobs_leave_networked_exec_flag_alone() -> None:
    registry = _registry(_FakeControls(status="running"), netns_jobs=False)
    ctx = _worker_context()

    await _call(registry, "coding_job_start", {"path": "scripts/test.sh"}, ctx)

    assert ctx.networked_exec_inflight is False


# --- browser tool: background contexts release per call --------------------


def test_background_task_browser_turn_releases_after_each_call() -> None:
    turn_id, release_after_call = _turn_id(_worker_context(background=True))
    assert release_after_call is True
    assert turn_id.startswith("coding:task-1:")
    assert _turn_id(_worker_context(background=True))[0] != turn_id


def test_rooted_discord_turn_keeps_browser_lease_for_the_turn() -> None:
    assert _turn_id(_worker_context(background=False)) == ("coding:task-1", False)


def test_browser_session_ignores_placeholder_conversation_id() -> None:
    assert _browser_session(_worker_context(conversation_id=0)).startswith("context-")
    assert _browser_session(_worker_context(conversation_id=7)) == "conversation-7"


@pytest.mark.asyncio
async def test_cancelled_rooted_browser_waiter_does_not_strand_turn(
    tmp_path: Path,
) -> None:
    lease = NetnsLease()
    service = BrowserService(
        _service_config(tmp_path, "netns"),
        netns_lease=lease,
    )
    await service.acquire_turn("current-owner", "current-turn")
    assert lease.locked() is True
    ctx = _worker_context(background=False)

    waiter = asyncio.create_task(_acquire_rooted_turn(service, ctx, "waiting-turn"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert waiter.done() is False

    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    await service.release_turn("current-owner", "current-turn")
    for _ in range(20):
        await asyncio.sleep(0)

    turn_stranded = service.has_active_turn(ctx.user_id, "waiting-turn")
    lease_stranded = lease.locked()
    if turn_stranded:
        await service.release_turn(ctx.user_id, "waiting-turn")
    await service.close()

    assert turn_stranded is False
    assert lease_stranded is False


# --- coding jobs wait for, and evict, the same user's idle browser ----------


def _manager(tmp_path: Path, guards: CodeExecRuntimeGuards, mode: str) -> CodingJobManager:
    return CodingJobManager(
        store=cast(Any, object()),
        workspace_manager=WorkspaceManager(tmp_path / "workspaces"),
        workspace_locks=UserLocks(),
        sandbox_config=SandboxConfig(network_mode=cast(Any, mode)),
        max_seconds=60,
        max_cpu_seconds=10,
        runtime_guards=guards,
        usage_store=cast(Any, object()),
    )


@pytest.mark.asyncio
async def test_netns_job_yields_the_browser_then_acquires(tmp_path: Path) -> None:
    lease = NetnsLease()
    await lease.acquire()  # the user's idle browser holds the namespace
    yielded: list[str] = []

    async def netns_yield(user_id: str) -> bool:
        yielded.append(user_id)
        await lease.release()
        return True

    guards = CodeExecRuntimeGuards.create(
        max_concurrency=1, network_weekly_limit=0, netns_lease=lease, netns_yield=netns_yield
    )
    manager = _manager(tmp_path, guards, "netns")
    assert manager.uses_netns is True

    async with manager._run_lease("u1"):
        assert lease.locked() is True

    assert yielded == ["u1"]
    assert lease.locked() is False


@pytest.mark.asyncio
async def test_netns_job_yields_without_racy_locked_precheck(tmp_path: Path) -> None:
    lease = NetnsLease()
    yielded: list[str] = []

    async def netns_yield(user_id: str) -> bool:
        yielded.append(user_id)
        return False

    guards = CodeExecRuntimeGuards.create(
        max_concurrency=1, network_weekly_limit=0, netns_lease=lease, netns_yield=netns_yield
    )
    manager = _manager(tmp_path, guards, "netns")

    async with manager._run_lease("u1"):
        assert lease.locked() is True

    assert yielded == ["u1"]


@pytest.mark.asyncio
async def test_netns_job_times_out_when_lease_stays_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import coding_jobs

    monkeypatch.setattr(coding_jobs, "CODING_JOB_LEASE_WAIT_SECONDS", 0.05)
    lease = NetnsLease()
    await lease.acquire()  # someone else's turn; yield cannot help

    async def netns_yield(_user_id: str) -> bool:
        return False

    guards = CodeExecRuntimeGuards.create(
        max_concurrency=1, network_weekly_limit=0, netns_lease=lease, netns_yield=netns_yield
    )
    manager = _manager(tmp_path, guards, "netns")

    with pytest.raises(RuntimeError, match="busy"):
        async with manager._run_lease("u1"):
            raise AssertionError("lease must not be acquired")

    assert lease.locked() is True


@pytest.mark.asyncio
async def test_host_mode_job_keeps_fail_fast_semaphore(tmp_path: Path) -> None:
    guards = CodeExecRuntimeGuards.create(max_concurrency=1, network_weekly_limit=0)
    manager = _manager(tmp_path, guards, "host")
    await guards.semaphore.acquire()
    try:
        with pytest.raises(RuntimeError, match="busy"):
            async with manager._run_lease("u1"):
                raise AssertionError("semaphore must not be acquired")
    finally:
        guards.semaphore.release()

    async with manager._run_lease("u1"):
        assert guards.semaphore.locked() is True
    assert guards.semaphore.locked() is False


# --- browser service: early yield of an idle worker -------------------------


class _Worker:
    def __init__(self, home: Path) -> None:
        self.home = home
        self.alive = True
        self.closed = False

    async def call(self, *, code: str, session: str) -> dict[str, Any]:
        return {"ok": True, "result": code}

    async def close(self) -> None:
        self.alive = False
        self.closed = True


def _service_config(tmp_path: Path, network_mode: BrowserNetworkMode) -> BrowserServiceConfig:
    return BrowserServiceConfig(
        runtime_dir=tmp_path / "runtime",
        profiles_dir=tmp_path / "profiles",
        bridge_script=tmp_path / "bridge.mjs",
        network_mode=network_mode,
        netns_helper_bin="/fixed/netns-helper",
        netns_resolv_conf="/fixed/resolv.conf",
        idle_ttl_seconds=3600,
    )


@pytest.mark.asyncio
async def test_close_idle_owner_releases_only_the_matching_idle_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lease = NetnsLease()
    workers: list[_Worker] = []

    async def factory(config: BrowserServiceConfig, owner: str, home: Path) -> _Worker:
        del config, owner
        worker = _Worker(home)
        workers.append(worker)
        return worker

    service = BrowserService(
        _service_config(tmp_path, "netns"), worker_factory=factory, netns_lease=lease
    )
    monkeypatch.setattr(service, "availability_error", lambda: None)

    await service.acquire_turn("u1", "turn-1")
    await service.run(owner_id="u1", turn_id="turn-1", session="s", code="1")
    assert lease.locked() is True

    # An active turn is never evicted, and another user cannot evict it either.
    assert await service.close_idle_owner("u1") is False
    assert await service.close_idle_owner("u2") is False
    assert lease.locked() is True

    await service.release_turn("u1", "turn-1")
    assert lease.locked() is True, "idle worker keeps the lease until its TTL"
    assert await service.close_idle_owner("u2") is False
    assert await service.close_idle_owner("u1") is True

    assert workers[0].closed is True
    assert lease.locked() is False
    await asyncio.sleep(0)
    await service.close()
