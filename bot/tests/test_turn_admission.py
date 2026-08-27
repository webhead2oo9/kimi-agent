from __future__ import annotations

import asyncio

import pytest

from app.admission import AdmissionRejection, TurnAdmissionController


@pytest.mark.asyncio
async def test_same_user_distinct_turns_hit_per_user_limit_without_waiting() -> None:
    controller = TurnAdmissionController(max_active=4, max_active_per_user=2)

    first = await controller.try_acquire("alice")
    second = await controller.try_acquire("alice")
    rejected = await asyncio.wait_for(controller.try_acquire("alice"), timeout=0.1)

    assert first.admitted
    assert second.admitted
    assert rejected.rejection is AdmissionRejection.USER_LIMIT
    assert (await controller.snapshot()).active_by_user == {"alice": 2}

    assert first.lease is not None
    assert second.lease is not None
    await first.lease.release()
    await second.lease.release()


@pytest.mark.asyncio
async def test_one_user_cannot_consume_capacity_reserved_for_other_users() -> None:
    controller = TurnAdmissionController(max_active=3, max_active_per_user=1)

    alice = await controller.try_acquire("alice")
    alice_again = await controller.try_acquire("alice")
    bob = await controller.try_acquire("bob")
    carol = await controller.try_acquire("carol")
    global_rejection = await controller.try_acquire("dave")

    assert alice.admitted
    assert alice_again.rejection is AdmissionRejection.USER_LIMIT
    assert bob.admitted
    assert carol.admitted
    assert global_rejection.rejection is AdmissionRejection.GLOBAL_LIMIT

    for decision in (alice, bob, carol):
        assert decision.lease is not None
        await decision.lease.release()


@pytest.mark.asyncio
async def test_lease_releases_on_cancellation_and_can_be_reused() -> None:
    controller = TurnAdmissionController(max_active=1, max_active_per_user=1)
    entered = asyncio.Event()

    async def admitted_work() -> None:
        decision = await controller.try_acquire("alice")
        assert decision.lease is not None
        async with decision.lease:
            entered.set()
            await asyncio.Future()

    task = asyncio.create_task(admitted_work())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert (await controller.snapshot()).active_total == 0
    replacement = await controller.try_acquire("bob")
    assert replacement.admitted
    assert replacement.lease is not None
    await replacement.lease.release()
    await replacement.lease.release()
    assert (await controller.snapshot()).active_total == 0


@pytest.mark.asyncio
async def test_lease_releases_after_repeated_cancellation_while_lock_is_busy() -> None:
    controller = TurnAdmissionController(max_active=1, max_active_per_user=1)
    decision = await controller.try_acquire("alice")
    lease = decision.lease
    assert lease is not None
    entered = asyncio.Event()

    async def admitted_work() -> None:
        async with lease:
            entered.set()
            await asyncio.Future()

    task = asyncio.create_task(admitted_work())
    await entered.wait()
    async with controller._lock:
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)

    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await controller.snapshot()).active_total == 0


@pytest.mark.asyncio
async def test_close_rejects_new_admission_while_existing_lease_can_drain() -> None:
    controller = TurnAdmissionController(max_active=2, max_active_per_user=1)
    existing = await controller.try_acquire("alice")
    assert existing.lease is not None

    await controller.close()
    rejected = await controller.try_acquire("bob")

    assert rejected.rejection is AdmissionRejection.SHUTTING_DOWN
    await existing.lease.release()
    assert (await controller.snapshot()).active_total == 0
