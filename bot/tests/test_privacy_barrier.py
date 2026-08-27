from __future__ import annotations

import asyncio
import threading
import time
from contextvars import Context

import pytest

from agent.core import ConversationTurnTimeoutError, _await_guarded_with_deadline
from utils.privacy_barrier import PrivacyDeletionPendingError, UserPrivacyBarrier


@pytest.mark.asyncio
async def test_deletion_drains_existing_activity_and_blocks_later_activity() -> None:
    barrier = UserPrivacyBarrier()
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    deletion_entered = asyncio.Event()
    release_deletion = asyncio.Event()
    later_entered = asyncio.Event()
    order: list[str] = []

    async def first_activity() -> None:
        async with barrier.activity("42"):
            order.append("first")
            first_entered.set()
            await release_first.wait()

    async def delete() -> None:
        async with barrier.deletion("42"):
            order.append("delete")
            deletion_entered.set()
            await release_deletion.wait()

    async def later_activity() -> None:
        async with barrier.activity("42"):
            order.append("later")
            later_entered.set()

    first = asyncio.create_task(first_activity())
    await first_entered.wait()
    deletion = asyncio.create_task(delete())
    await asyncio.sleep(0)
    later = asyncio.create_task(later_activity())
    await asyncio.sleep(0)

    assert not deletion_entered.is_set()
    assert not later_entered.is_set()

    release_first.set()
    await deletion_entered.wait()
    assert not later_entered.is_set()

    release_deletion.set()
    await later_entered.wait()
    await asyncio.gather(first, deletion, later)
    assert order == ["first", "delete", "later"]


@pytest.mark.asyncio
async def test_descendant_activity_joins_group_ahead_of_waiting_deletion() -> None:
    barrier = UserPrivacyBarrier()
    deletion_entered = asyncio.Event()
    child_entered = asyncio.Event()
    release_child = asyncio.Event()

    async with barrier.activity("42"):
        # A nested helper in this task must not deadlock when deletion is queued.
        # The delete request is an independent Discord interaction, so do not
        # let create_task inherit this surface's activity ContextVar.
        deletion = asyncio.create_task(
            _enter_deletion(barrier, deletion_entered),
            context=Context(),
        )
        await asyncio.sleep(0)
        async with barrier.activity("42"):
            pass

        # The guarded child belongs to the already-running surface. It must join
        # that lease group even though writer preference now blocks genuinely new
        # top-level activity, or parent-awaits-child would deadlock until timeout.
        async def child() -> None:
            async with barrier.activity("42"):
                child_entered.set()
                await release_child.wait()

        child_task = asyncio.create_task(child())
        await child_entered.wait()
        assert not deletion_entered.is_set()

    # The root surface has exited, but its detached child keeps the group alive.
    await asyncio.sleep(0)
    assert not deletion_entered.is_set()
    release_child.set()
    await child_task
    await deletion_entered.wait()
    await deletion


async def _enter_deletion(
    barrier: UserPrivacyBarrier,
    entered: asyncio.Event,
) -> None:
    async with barrier.deletion("42"):
        entered.set()


@pytest.mark.asyncio
async def test_stale_descendant_context_cannot_join_closed_group_during_deletion() -> None:
    barrier = UserPrivacyBarrier()
    try_child = asyncio.Event()
    child_entered = asyncio.Event()

    async with barrier.activity("42"):

        async def delayed_child() -> None:
            await try_child.wait()
            async with barrier.activity("42"):
                child_entered.set()

        child = asyncio.create_task(delayed_child())
        await asyncio.sleep(0)

    deletion_entered = asyncio.Event()
    release_deletion = asyncio.Event()

    async def delete() -> None:
        async with barrier.deletion("42"):
            deletion_entered.set()
            await release_deletion.wait()

    deletion = asyncio.create_task(delete())
    await deletion_entered.wait()
    try_child.set()
    await asyncio.sleep(0)
    assert not child_entered.is_set()

    release_deletion.set()
    await deletion
    await child
    assert child_entered.is_set()


@pytest.mark.asyncio
async def test_cancelled_waiters_do_not_leak_barrier_state() -> None:
    barrier = UserPrivacyBarrier()
    deletion_entered = asyncio.Event()
    release_deletion = asyncio.Event()

    async def delete() -> None:
        async with barrier.deletion("42"):
            deletion_entered.set()
            await release_deletion.wait()

    deletion = asyncio.create_task(delete())
    await deletion_entered.wait()

    async def wait_for_activity() -> None:
        async with barrier.activity("42"):
            raise AssertionError("cancelled waiter unexpectedly entered")

    waiting = asyncio.create_task(wait_for_activity())
    await asyncio.sleep(0)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting

    release_deletion.set()
    await deletion
    # A clean lease after cancellation proves no phantom waiter/reader remains.
    async with barrier.activity("42"):
        pass


@pytest.mark.asyncio
async def test_waiter_queued_before_tombstone_cannot_enter_after_failed_delete() -> None:
    barrier = UserPrivacyBarrier()
    deletion_entered = asyncio.Event()
    release_deletion = asyncio.Event()
    activity_entered = asyncio.Event()

    async def delete() -> None:
        async with barrier.deletion("42"):
            deletion_entered.set()
            await release_deletion.wait()

    async def waiting_activity() -> None:
        async with barrier.activity("42"):
            activity_entered.set()

    deletion = asyncio.create_task(delete())
    await deletion_entered.wait()
    waiter = asyncio.create_task(waiting_activity())
    await asyncio.sleep(0)

    # The activity was already queued before the failed deletion installed its
    # durable tombstone. It must wake to the pending error, not slip through when
    # the exclusive lease exits.
    await barrier.mark_deletion_pending("42")
    release_deletion.set()
    await deletion

    with pytest.raises(PrivacyDeletionPendingError):
        await waiter
    assert not activity_entered.is_set()


@pytest.mark.asyncio
async def test_cancelled_deletion_waiter_does_not_block_future_activity() -> None:
    barrier = UserPrivacyBarrier()
    release_activity = asyncio.Event()

    async def activity() -> None:
        async with barrier.activity("42"):
            await release_activity.wait()

    active = asyncio.create_task(activity())
    await asyncio.sleep(0)
    deletion = asyncio.create_task(_enter_deletion(barrier, asyncio.Event()))
    await asyncio.sleep(0)
    deletion.cancel()
    with pytest.raises(asyncio.CancelledError):
        await deletion

    # The cancelled deletion must no longer receive writer priority.
    later_entered = asyncio.Event()

    async def later() -> None:
        async with barrier.activity("42"):
            later_entered.set()

    later_task = asyncio.create_task(later())
    await later_entered.wait()
    release_activity.set()
    await asyncio.gather(active, later_task)


@pytest.mark.asyncio
async def test_timed_out_worker_keeps_deletion_blocked_until_real_completion() -> None:
    barrier = UserPrivacyBarrier()
    surface_entered = asyncio.Event()
    start_guarded_worker = asyncio.Event()
    deletion_entered = asyncio.Event()
    release_deletion = asyncio.Event()
    worker_entered = threading.Event()
    release_worker = threading.Event()

    def slow_mutation() -> str:
        worker_entered.set()
        assert release_worker.wait(timeout=2.0)
        return "written"

    async def surface() -> None:
        async with barrier.activity("42"):
            surface_entered.set()
            await start_guarded_worker.wait()
            with pytest.raises(ConversationTurnTimeoutError):
                await _await_guarded_with_deadline(
                    lambda: asyncio.to_thread(slow_mutation),
                    deadline=time.monotonic() + 0.03,
                    user_id="42",
                    activity_guard=barrier.activity,
                )

    async def delete() -> None:
        async with barrier.deletion("42"):
            deletion_entered.set()
            await release_deletion.wait()

    surface_task = asyncio.create_task(surface())
    await surface_entered.wait()
    deletion_task = asyncio.create_task(delete())
    # Queue the writer before spawning the guarded child: the child must join the
    # surface's inherited group rather than deadlocking behind writer preference.
    await asyncio.sleep(0)
    start_guarded_worker.set()

    for _ in range(100):
        if worker_entered.is_set():
            break
        await asyncio.sleep(0.005)
    assert worker_entered.is_set()

    await surface_task
    assert not deletion_entered.is_set()

    release_worker.set()
    await deletion_entered.wait()
    release_deletion.set()
    await deletion_task
