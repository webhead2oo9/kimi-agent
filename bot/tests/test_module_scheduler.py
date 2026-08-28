"""Durable module scheduler: leases, recovery, backoff, overlap, handler binding."""

from __future__ import annotations

import asyncio
import random
from pathlib import Path

import pytest

from kimi_agent_module_api.contracts import Backoff, JobRun, ModuleContractError
from modules.scheduler import (
    FOREIGN_RUNNER_DETAIL,
    RUNNER_TABLE,
    TABLE,
    DurableScheduler,
)
from storage.db import Database


class _Clock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


async def _db(tmp_path: Path) -> Database:
    db = Database(str(tmp_path / "bot.db"))
    await db.connect()
    return db


def _scheduler(db: Database, clock: _Clock, **kwargs: object) -> DurableScheduler:
    return DurableScheduler(
        db,
        clock=clock,
        lease_seconds=30.0,
        rng=random.Random(7),
        **kwargs,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_one_shot_and_periodic_jobs_persist_and_run_when_due(tmp_path: Path) -> None:
    db = await _db(tmp_path)
    clock = _Clock()
    scheduler = _scheduler(db, clock)
    ran: list[tuple[str, int]] = []

    async def handler(run: JobRun) -> None:
        ran.append((run.key, run.attempt))

    view = scheduler.view_for("mod")
    view.register("tick", handler)
    await view.run_at("once", clock.now + 10, "tick", {"n": 1})
    await view.run_every("often", 60, "tick", jitter_seconds=5)
    try:
        assert await scheduler.run_due() == 1  # only the periodic one is due now
        assert ran == [("often", 1)]
        clock.now += 10
        assert await scheduler.run_due() == 1
        assert ran[-1] == ("once", 1)
        jobs = {job.key: job for job in await view.list()}
        assert "once" not in jobs  # one-shot deleted on success
        assert 60 <= jobs["often"].next_run_at - 1_000.0 <= 65  # interval + jitter
        assert jobs["often"].attempt == 0
        # Same key replaces the schedule instead of duplicating it.
        await view.run_every("often", 30, "tick")
        assert len(await view.list()) == 1
        assert await view.cancel("often") is True
        assert await view.cancel("often") is False
    finally:
        await scheduler.close()
        await db.close()


@pytest.mark.asyncio
async def test_failures_back_off_and_a_lease_prevents_overlap(tmp_path: Path) -> None:
    db = await _db(tmp_path)
    clock = _Clock()
    scheduler = _scheduler(db, clock)
    attempts: list[int] = []

    async def flaky(run: JobRun) -> None:
        attempts.append(run.attempt)
        raise RuntimeError("hub down")

    view = scheduler.view_for("mod")
    view.register("sync", flaky)
    await view.run_every("hub", 300, "sync", backoff=Backoff(base_seconds=10, max_seconds=40))
    try:
        await scheduler.run_due()
        (job,) = await view.list()
        assert job.attempt == 1 and job.last_error is not None and "hub down" in job.last_error
        assert job.next_run_at == clock.now + 10
        clock.now += 10
        await scheduler.run_due()
        (job,) = await view.list()
        assert job.attempt == 2 and job.next_run_at == clock.now + 20
        clock.now += 20
        await scheduler.run_due()
        clock.now += 40
        await scheduler.run_due()
        (job,) = await view.list()
        assert job.next_run_at == clock.now + 40  # capped
        assert attempts == [1, 2, 3, 4]

        # A live lease blocks a second claim of the same key.
        async with db.write_transaction() as conn:
            await conn.execute(
                f"UPDATE {TABLE} SET run_at = ?, leased_until = ?, lease_token = 'x'",
                (clock.now, clock.now + 30),
            )
        assert await scheduler.run_due() == 0
        # ...until it expires, e.g. after a crash.
        clock.now += 31
        assert await scheduler.run_due() == 1
    finally:
        await scheduler.close()
        await db.close()


@pytest.mark.asyncio
async def test_failure_backoff_caps_before_extreme_multiplier_overflows(tmp_path: Path) -> None:
    db = await _db(tmp_path)
    clock = _Clock()
    scheduler = _scheduler(db, clock)
    attempts: list[int] = []

    async def fails(run: JobRun) -> None:
        attempts.append(run.attempt)
        raise RuntimeError("still down")

    view = scheduler.view_for("mod")
    view.register("sync", fails)
    await view.run_every(
        "extreme",
        300,
        "sync",
        backoff=Backoff(base_seconds=1, max_seconds=5, multiplier=1e308),
    )
    try:
        await scheduler.run_due()
        clock.now += 1
        await scheduler.run_due()
        clock.now += 5
        await scheduler.run_due()

        (job,) = await view.list()
        assert attempts == [1, 2, 3]
        assert job.attempt == 3
        assert job.next_run_at == clock.now + 5
        assert job.last_error is not None and "still down" in job.last_error
    finally:
        await scheduler.close()
        await db.close()


@pytest.mark.asyncio
async def test_jobs_survive_restart_and_pause_without_a_handler(tmp_path: Path) -> None:
    db = await _db(tmp_path)
    clock = _Clock()
    first = _scheduler(db, clock)

    async def noop(run: JobRun) -> None:
        pass

    first.view_for("mod").register("tick", noop)
    await first.view_for("mod").run_every("often", 60, "tick")
    await first.close()

    health: list[tuple[str, str, str]] = []
    second = _scheduler(db, clock, on_health=lambda m, s, d: health.append((m, s, d)))
    try:
        # No handler registered yet: the job is paused, not lost, and health says so.
        assert await second.run_due() == 0
        assert health == [("mod", "degraded", "scheduled job 'often' has no handler")]
        (job,) = await second.list_jobs("mod")
        assert job.last_error == "no handler 'tick'"
        assert job.next_run_at == clock.now + 30
        clock.now += 30
        ran: list[str] = []

        async def handler(run: JobRun) -> None:
            ran.append(run.key)

        second.view_for("mod").register("tick", handler)
        assert health[-1] == ("mod", "healthy", "")
        assert await second.run_due() == 1
        assert ran == ["often"]
    finally:
        await second.close()
        await db.close()


@pytest.mark.asyncio
async def test_orphaned_due_job_does_not_starve_runnable_due_job(tmp_path: Path) -> None:
    db = await _db(tmp_path)
    clock = _Clock()
    scheduler = _scheduler(db, clock)
    ran: list[str] = []

    async def handler(run: JobRun) -> None:
        ran.append(run.key)

    view = scheduler.view_for("mod")
    view.register("tick", handler)
    # Ensure the orphan is the first row considered by the due-job ordering.
    await view.run_at("orphan", clock.now - 1, "missing")
    await view.run_at("runnable", clock.now, "tick")
    try:
        assert await scheduler.run_due() == 1
        assert ran == ["runnable"]
        (orphan,) = await view.list()
        assert orphan.key == "orphan"
        assert orphan.last_error == "no handler 'missing'"
        assert orphan.next_run_at == clock.now + 30
    finally:
        await scheduler.close()
        await db.close()


@pytest.mark.asyncio
async def test_rescheduling_running_one_shot_survives_stale_settlement(tmp_path: Path) -> None:
    db = await _db(tmp_path)
    clock = _Clock()
    scheduler = _scheduler(db, clock)
    replacement_runs: list[JobRun] = []
    view = scheduler.view_for("mod")

    async def replace(_run: JobRun) -> None:
        await view.run_at("job", clock.now + 60, "replacement", {"generation": 2})

    async def replacement(run: JobRun) -> None:
        replacement_runs.append(run)

    view.register("replace", replace)
    view.register("replacement", replacement)
    await view.run_at("job", clock.now, "replace")
    try:
        assert await scheduler.run_due() == 1
        (job,) = await view.list()
        assert job.key == "job"
        assert job.handler == "replacement"
        assert job.next_run_at == clock.now + 60
        assert job.attempt == 0

        clock.now += 60
        assert await scheduler.run_due() == 1
        assert [run.payload for run in replacement_runs] == [{"generation": 2}]
        assert await view.list() == []
    finally:
        await scheduler.close()
        await db.close()


@pytest.mark.asyncio
async def test_rescheduling_running_periodic_job_preserves_replacement_definition(
    tmp_path: Path,
) -> None:
    db = await _db(tmp_path)
    clock = _Clock()
    scheduler = _scheduler(db, clock)
    replacement_started = asyncio.Event()
    view = scheduler.view_for("mod")

    async def replace(_run: JobRun) -> None:
        await view.run_every("job", 300, "replacement", {"generation": 2})

    async def replacement(_run: JobRun) -> None:
        replacement_started.set()

    view.register("replace", replace)
    view.register("replacement", replacement)
    await view.run_every("job", 60, "replace")
    try:
        assert await scheduler.run_due(limit=1) == 1
        (job,) = await view.list()
        assert job.handler == "replacement"
        assert job.interval_seconds == 300
        assert job.next_run_at == clock.now
        assert job.attempt == 0

        # Settlement releases the completed execution's still-owned lease, so
        # its due replacement is immediately claimable without overlap.
        assert await scheduler.run_due() == 1
        assert replacement_started.is_set()
        (job,) = await view.list()
        assert job.handler == "replacement"
        assert job.interval_seconds == 300
        assert job.next_run_at == clock.now + 300
    finally:
        await scheduler.close()
        await db.close()


@pytest.mark.asyncio
async def test_rescheduled_job_keeps_running_execution_lease_heartbeating(
    tmp_path: Path,
) -> None:
    db = await _db(tmp_path)
    clock = _Clock()
    scheduler = DurableScheduler(db, clock=clock, lease_seconds=0.2)
    replacement_runs: list[str] = []
    view = scheduler.view_for("mod")

    async def replace(_run: JobRun) -> None:
        await view.run_at("job", clock.now, "replacement")
        original_deadline = clock.now + 0.2
        clock.now += 0.15
        for _ in range(100):
            cursor = await db.conn.execute(
                f"SELECT leased_until FROM {TABLE} WHERE job_id = ?", ("mod:job",)
            )
            row = await cursor.fetchone()
            if row is not None and row[0] is not None and float(row[0]) > original_deadline:
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("running execution did not heartbeat its replacement's lease")
        clock.now += 0.1
        # The replacement is already due, but cannot overlap this execution.
        assert await scheduler.run_due() == 0

    async def replacement(run: JobRun) -> None:
        replacement_runs.append(run.key)

    view.register("replace", replace)
    view.register("replacement", replacement)
    await view.run_at("job", clock.now, "replace")
    try:
        assert await scheduler.run_due(limit=1) == 1
        assert replacement_runs == []
        # Completion releases the old execution's lease without changing the
        # replacement definition, making it immediately claimable.
        assert await scheduler.run_due() == 1
        assert replacement_runs == ["job"]
    finally:
        await scheduler.close()
        await db.close()


@pytest.mark.asyncio
async def test_cancelling_the_last_paused_job_clears_scheduler_health(tmp_path: Path) -> None:
    db = await _db(tmp_path)
    clock = _Clock()
    health: list[tuple[str, str, str]] = []
    scheduler = _scheduler(db, clock, on_health=lambda m, s, d: health.append((m, s, d)))
    view = scheduler.view_for("mod")
    try:
        await view.run_at("orphan", clock.now, "missing")
        assert await scheduler.run_due() == 0
        assert health[-1][1] == "degraded"
        assert await view.cancel("orphan") is True
        assert health[-1] == ("mod", "healthy", "")
    finally:
        await scheduler.close()
        await db.close()


@pytest.mark.asyncio
async def test_runner_loop_wakes_on_schedule_and_heartbeats_long_jobs(tmp_path: Path) -> None:
    db = await _db(tmp_path)
    clock = _Clock()
    scheduler = DurableScheduler(db, clock=clock, lease_seconds=0.2, poll_seconds=0.05)
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow(run: JobRun) -> None:
        started.set()
        await release.wait()

    view = scheduler.view_for("mod")
    view.register("slow", slow)
    scheduler.start()
    try:
        await view.run_at("job", clock.now, "slow")
        await asyncio.wait_for(started.wait(), timeout=2)
        await asyncio.sleep(0.35)  # more than one lease period
        cursor = await db.conn.execute(f"SELECT leased_until FROM {TABLE}")
        row = await cursor.fetchone()
        assert row is not None and row[0] > clock.now  # heartbeat kept the lease alive
        release.set()
        for _ in range(40):
            await asyncio.sleep(0.05)
            if not await view.list():
                break
        assert await view.list() == []
    finally:
        await scheduler.close()
        await db.close()


@pytest.mark.asyncio
async def test_schedule_validation(tmp_path: Path) -> None:
    db = await _db(tmp_path)
    scheduler = _scheduler(db, _Clock())
    view = scheduler.view_for("mod")
    try:
        with pytest.raises(ModuleContractError):
            await view.run_every("k", 0, "h")
        with pytest.raises(ModuleContractError):
            await view.run_at("", 1.0, "h")
        with pytest.raises(ModuleContractError):
            await view.run_at("k", 1.0, "")
    finally:
        await scheduler.close()
        await db.close()


@pytest.mark.asyncio
async def test_runner_runs_modules_concurrently_but_one_job_per_module(tmp_path: Path) -> None:
    db = await _db(tmp_path)
    clock = _Clock()
    scheduler = DurableScheduler(db, clock=clock, poll_seconds=0.02, max_concurrent=4)
    in_flight: set[str] = set()
    overlap: list[set[str]] = []
    release = asyncio.Event()

    def blocking(name: str):
        async def handler(run: JobRun) -> None:
            in_flight.add(name)
            overlap.append(set(in_flight))
            await release.wait()
            in_flight.discard(name)

        return handler

    scheduler.view_for("a").register("h", blocking("a"))
    scheduler.view_for("b").register("h", blocking("b"))
    await scheduler.view_for("a").run_at("first", clock.now, "h")
    await scheduler.view_for("a").run_at("second", clock.now, "h")
    await scheduler.view_for("b").run_at("first", clock.now, "h")
    scheduler.start()
    try:
        for _ in range(50):
            await asyncio.sleep(0.02)
            if len(in_flight) == 2:
                break
        # Both modules run at once; a's second job waits for a's first.
        assert in_flight == {"a", "b"}
        assert len(scheduler._running) == 2
        release.set()
        for _ in range(100):
            await asyncio.sleep(0.02)
            if not await scheduler.list_jobs("a") and not await scheduler.list_jobs("b"):
                break
        assert await scheduler.list_jobs("a") == []
        assert max(len(seen) for seen in overlap) == 2
    finally:
        await scheduler.close()
        await db.close()


@pytest.mark.asyncio
async def test_runner_pauses_while_another_runner_holds_the_lease(tmp_path: Path) -> None:
    db = await _db(tmp_path)
    clock = _Clock()
    health: list[tuple[str, str, str]] = []
    scheduler = DurableScheduler(
        db,
        clock=clock,
        lease_seconds=30.0,
        poll_seconds=0.02,
        on_health=lambda m, s, d: health.append((m, s, d)),
    )
    ran: list[str] = []

    async def handler(run: JobRun) -> None:
        ran.append(run.key)

    view = scheduler.view_for("mod")
    view.register("h", handler)
    await view.run_at("mine", clock.now, "h")
    # Another process holds the singleton runner lease.
    async with db.write_transaction() as conn:
        await conn.execute(
            f"UPDATE {RUNNER_TABLE} SET token = 'other-process', leased_until = ?",
            (clock.now + 30,),
        )
    scheduler.start()
    try:
        await asyncio.sleep(0.1)
        assert scheduler.paused_for_foreign_runner
        assert ran == []
        assert health == [("mod", "degraded", FOREIGN_RUNNER_DETAIL)]

        # The other process crashed: its lease expires and this runner takes over.
        clock.now += 31
        for _ in range(50):
            await asyncio.sleep(0.02)
            if ran:
                break
        assert ran == ["mine"]
        assert not scheduler.paused_for_foreign_runner
        assert health[-1] == ("mod", "healthy", "")
    finally:
        await scheduler.close()
        cursor = await db.conn.execute(f"SELECT token FROM {RUNNER_TABLE}")
        row = await cursor.fetchone()
        assert row is not None and row[0] is None, "close releases the runner lease"
        await db.close()


@pytest.mark.asyncio
async def test_two_runners_cannot_both_hold_the_lease(tmp_path: Path) -> None:
    db = await _db(tmp_path)
    clock = _Clock()
    first = _scheduler(db, clock)
    second = _scheduler(db, clock)
    try:
        assert await first._acquire_runner_lease(clock.now)
        assert not await second._acquire_runner_lease(clock.now)
        # Renewal by the holder keeps working; the other still cannot take it.
        clock.now += 10
        assert await first._acquire_runner_lease(clock.now)
        assert not await second._acquire_runner_lease(clock.now)
        await first.close()
        assert await second._acquire_runner_lease(clock.now)
    finally:
        await second.close()
        await db.close()


@pytest.mark.asyncio
async def test_run_due_shares_one_job_per_module_with_the_runner(tmp_path: Path) -> None:
    db = await _db(tmp_path)
    clock = _Clock()
    scheduler = DurableScheduler(db, clock=clock, poll_seconds=0.02)
    entered = asyncio.Event()
    release = asyncio.Event()
    overlapped = False
    in_flight = 0

    async def handler(run: JobRun) -> None:
        nonlocal in_flight, overlapped
        in_flight += 1
        overlapped = overlapped or in_flight > 1
        entered.set()
        await release.wait()
        in_flight -= 1

    view = scheduler.view_for("mod")
    view.register("h", handler)
    await view.run_at("a", clock.now, "h")
    await view.run_at("b", clock.now, "h")
    try:
        inline = asyncio.create_task(scheduler.run_due())
        await asyncio.wait_for(entered.wait(), timeout=2)
        scheduler.start()
        await asyncio.sleep(0.1)
        assert in_flight == 1, "the runner must not start b while run_due runs a"
        assert len(scheduler._running) == 1 and scheduler._running_modules == {"mod"}
        release.set()
        await inline
        for _ in range(100):
            await asyncio.sleep(0.02)
            if not await view.list():
                break
        assert not overlapped
        assert await view.list() == []
    finally:
        await scheduler.close()
        await db.close()


def test_max_concurrent_must_be_positive(tmp_path: Path) -> None:
    with pytest.raises(ModuleContractError):
        DurableScheduler(object(), max_concurrent=0)


@pytest.mark.asyncio
async def test_run_due_also_needs_the_runner_lease(tmp_path: Path) -> None:
    db = await _db(tmp_path)
    clock = _Clock()
    health: list[tuple[str, str, str]] = []
    scheduler = _scheduler(db, clock, on_health=lambda m, s, d: health.append((m, s, d)))

    async def handler(run: JobRun) -> None:
        pass

    scheduler.view_for("mod").register("h", handler)
    await scheduler.view_for("mod").run_at("j", clock.now, "h")
    async with db.write_transaction() as conn:
        await conn.execute(
            f"UPDATE {RUNNER_TABLE} SET token = 'other', leased_until = ?", (clock.now + 30,)
        )
    try:
        assert await scheduler.run_due() == 0
        assert health == [("mod", "degraded", FOREIGN_RUNNER_DETAIL)]
        # A module registering during the pause is told, too.
        scheduler.view_for("late").register("h", handler)
        assert health[-1] == ("late", "degraded", FOREIGN_RUNNER_DETAIL)
        clock.now += 31
        assert await scheduler.run_due() == 1
        assert ("mod", "healthy", "") in health and ("late", "healthy", "") in health
    finally:
        await scheduler.close()
        await db.close()


@pytest.mark.asyncio
async def test_resume_restores_the_same_orphan_detail(tmp_path: Path) -> None:
    db = await _db(tmp_path)
    clock = _Clock()
    health: list[tuple[str, str, str]] = []
    first = _scheduler(db, clock)

    async def handler(run: JobRun) -> None:
        pass

    first.view_for("mod").register("h", handler)
    await first.view_for("mod").run_at("orphan", clock.now, "h")
    await first.close()
    second = _scheduler(db, clock, on_health=lambda m, s, d: health.append((m, s, d)))
    second.view_for("mod").register("other", handler)
    try:
        assert await second.run_due() == 0
        orphan_detail = health[-1]
        assert orphan_detail == ("mod", "degraded", "scheduled job 'orphan' has no handler")
        # Foreign pause and resume must restore exactly that detail.
        async with db.write_transaction() as conn:
            await conn.execute(
                f"UPDATE {RUNNER_TABLE} SET token = 'other', leased_until = ?", (clock.now + 30,)
            )
        assert await second.run_due() == 0
        clock.now += 31
        assert await second.run_due() == 0
        assert health[-1] == orphan_detail
    finally:
        await second.close()
        await db.close()
