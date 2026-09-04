"""Deterministic probes for the production module scheduler."""

from __future__ import annotations

from modules.scheduler import DurableScheduler


async def run_due_jobs(scheduler: DurableScheduler, *, limit: int = 50) -> int:
    """Drive due jobs through the scheduler's real tick path, one at a time.

    Production owns the continuous runner loop. Tests use a single-capacity
    tick so each started task can be observed and awaited deterministically.
    """

    if scheduler._runner is not None:
        raise RuntimeError("cannot drive a scheduler whose runner loop is active")
    if limit < 1:
        return 0

    original_capacity = scheduler._max_concurrent
    scheduler._max_concurrent = 1
    ran = 0
    try:
        while ran < limit:
            progressed = await scheduler._tick()
            if not progressed:
                break
            (task,) = scheduler._running.values()
            await task
            ran += 1
    finally:
        scheduler._max_concurrent = original_capacity
    return ran


def scheduler_paused_for_foreign_runner(scheduler: DurableScheduler) -> bool:
    """Inspect pause state without exposing it as a production property."""

    return scheduler._foreign_paused
