from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from config.settings import Settings
from tests.test_scheduled_tasks import definition, store as store
from tests.test_task_controls import harness as harness


async def task(store, owner, *, mode="llm", due=1):
    task_id = await store.draft(
        task_id=None,
        guild_id="100",
        owner_id=owner,
        channel_id="200",
        proposer_id=owner,
        definition=definition(
            execution=mode,
            python={"code": "pass"} if mode != "llm" else None,
        ),
    )
    await store.activate(task_id, 1, owner, due, reset_state=False)
    return await store.get(task_id, active=True)


async def until(predicate):
    async with asyncio.timeout(5):
        # Observe background DB/state transitions without adding production test hooks.
        while not predicate():  # noqa: ASYNC110
            await asyncio.sleep(0.001)


@pytest.mark.parametrize("field", ["llm", "python", "delivery"])
def test_scheduled_capacity_defaults_and_validation(field):
    name = f"scheduled_task_{field}_max_concurrency"
    assert getattr(Settings(_env_file=None), name) == 2
    with pytest.raises(ValueError):
        Settings(_env_file=None, **{name: 0})
    assert getattr(Settings(_env_file=None, **{name: 3}), name) == 3


@pytest.mark.asyncio
async def test_candidates_do_not_truncate_other_owners_and_claim_fences_owner(store):
    backlog = [await task(store, "10", due=index + 1) for index in range(12)]
    other = await task(store, "20", due=20)
    assert [item["id"] for item in await store.due_candidates(30)] == [
        backlog[0]["id"],
        other["id"],
    ]
    first = await store.claim(backlog[0], 100)
    assert first is not None
    assert await store.claim(backlog[1], 100) is None
    assert [item["id"] for item in await store.due_candidates(30)] == [other["id"]]
    # Delivery retains the per-task fence, while freeing the owner's execution slot.
    await store.finish(first, "delivery", "", {}, [{"channel_id": "300", "content": "Result"}])
    assert await store.claim(backlog[1], 100) is not None
    assert await store.claim(backlog[0], 100) is None


@pytest.mark.asyncio
async def test_owner_rotation_preserves_oldest_eligible_task(harness, monkeypatch):
    service, _, _ = harness
    scheduler = service.scheduler
    scheduler._llm_limit = 1
    first = await task(service.r.store, "10", due=1)
    same_owner = await task(service.r.store, "10", due=2)
    other = await task(service.r.store, "20", due=3)
    started = []
    release = asyncio.Event()

    async def execute(record, run_id, ctx, definition, **kwargs):
        started.append(record["id"])
        await release.wait()
        await service.publisher.finish(record, run_id, "no_change", "Checked", {}, [])

    monkeypatch.setattr(service.authority, "validate_definition", AsyncMock())
    monkeypatch.setattr(service.executor, "execute", execute)
    await service.r.store.lease(service.authority.token, time.time())
    await scheduler.admit_due(time.time())
    await until(lambda: len(started) == 1)
    assert started == [first["id"]]
    release.set()
    await asyncio.gather(*scheduler._workers.values())
    release.clear()
    await scheduler.admit_due(time.time())
    await until(lambda: len(started) == 2)
    assert started[-1] == other["id"]
    release.set()
    await asyncio.gather(*scheduler._workers.values())
    await scheduler.admit_due(time.time())
    await until(lambda: len(started) == 3)
    assert started[-1] == same_owner["id"]


@pytest.mark.asyncio
async def test_gates_release_python_capacity_and_bound_handoffs(harness, monkeypatch):
    service, _, _ = harness
    scheduler = service.scheduler
    scheduler._llm_limit = scheduler._python_limit = 1
    llm = await task(service.r.store, "10", due=1)
    gate = await task(service.r.store, "20", mode="python_gate", due=2)
    queued_gate = await task(service.r.store, "21", mode="python_gate", due=3)
    python = await task(service.r.store, "30", mode="python_only", due=4)
    started, handoffs = [], []
    release_llm = asyncio.Event()
    gate_in_llm = asyncio.Event()

    async def execute(record, run_id, ctx, definition, *, before_handoff):
        started.append(record["id"])
        if definition.execution == "llm":
            await release_llm.wait()
        elif definition.execution == "python_gate":
            await before_handoff()
            handoffs.append(record["id"])
            gate_in_llm.set()
            await asyncio.Event().wait()
        await service.publisher.finish(record, run_id, "no_change", "Checked", {}, [])

    monkeypatch.setattr(service.authority, "validate_definition", AsyncMock())
    monkeypatch.setattr(service.executor, "execute", execute)
    await scheduler.start()
    await until(lambda: python["id"] in started)
    assert started == [llm["id"], gate["id"], python["id"]]
    assert queued_gate["id"] not in started
    assert not handoffs
    assert (await service.r.store.history(gate["id"]))[0]["status"] == "running"
    release_llm.set()
    await asyncio.wait_for(gate_in_llm.wait(), 5)
    await until(lambda: queued_gate["id"] in started)
    assert handoffs == [gate["id"]]
    assert started.count(gate["id"]) == 1
    await scheduler.cancel(queued_gate["id"])
    assert queued_gate["id"] not in scheduler._admitted
    assert (await service.r.store.get(queued_gate["id"]))["status"] == "attention"


@pytest.mark.asyncio
async def test_recovery_does_not_replay_waiting_gate(store):
    gate = await task(store, "10", mode="python_gate")
    run = await store.claim(gate, 200)
    await store.recover()
    assert (await store.get(gate["id"]))["status"] == "attention"
    assert (await store.history(gate["id"]))[0]["id"] == run
    assert (await store.history(gate["id"]))[0]["status"] == "interrupted"
    assert await store.due_candidates(300) == []


async def output_run(store, owner, contents):
    record = await task(store, owner)
    run_id = await store.claim(record, 100)
    await store.finish(
        run_id,
        "delivery",
        "Ready",
        {"cursor": "new"},
        [{"channel_id": "300", "content": content} for content in contents],
    )
    return record, run_id


@pytest.mark.asyncio
async def test_concurrent_publication_refills_pool_and_preserves_chunk_order(harness, monkeypatch):
    service, _, destination = harness
    store = service.r.store
    slow, _ = await output_run(store, "10", [f"slow-{i}" for i in range(25)])
    fast, _ = await output_run(store, "20", ["fast-0", "fast-1"])
    third, _ = await output_run(store, "30", ["third"])
    started = []
    release = asyncio.Event()

    async def send(content, **kwargs):
        started.append(content)
        if content == "slow-0":
            await release.wait()
        return SimpleNamespace(
            id=len(started), content=content, channel=destination, created_at=datetime.now(UTC)
        )

    destination.send.side_effect = send
    monkeypatch.setattr(service.publisher, "record_message", AsyncMock())
    await store.lease(service.authority.token, time.time())
    worker = asyncio.create_task(service.publisher.deliver_pending())
    try:
        await until(lambda: "third" in started)
        assert [item for item in started if item.startswith("slow")] == ["slow-0"]
        assert [item for item in started if item.startswith("fast")] == ["fast-0", "fast-1"]
        assert (await store.get(slow["id"]))["state"] == {}
        await until(lambda: worker.done() or "third" in started)
        release.set()
        await asyncio.wait_for(worker, 5)
        assert [item for item in started if item.startswith("slow")] == [
            f"slow-{i}" for i in range(25)
        ]
        for record in (slow, fast, third):
            assert (await store.get(record["id"]))["state"] == {"cursor": "new"}
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


@pytest.mark.asyncio
async def test_rate_limited_first_chunk_blocks_later_chunks(harness):
    service, _, destination = harness
    store = service.r.store
    record, run_id = await output_run(store, "10", ["first", "second"])
    destination.send.side_effect = discord.HTTPException(
        SimpleNamespace(status=429, reason="Too Many Requests"), "Slow down"
    )
    await store.lease(service.authority.token, time.time())
    await service.publisher.deliver_pending()
    assert destination.send.await_count == 1
    assert (await store.get(record["id"]))["state"] == {}
    assert await store.delivery_runs(2, set()) == []
    pending = await store.deliveries()
    assert len(pending) == 1  # Only the later chunk is time-eligible.
    assert not await store.begin_delivery(pending[0]["id"], service.authority.token)
    assert await store.deliveries(run_id=run_id) == []


@pytest.mark.asyncio
async def test_slow_approval_and_publication_do_not_stop_lease_renewal(harness, monkeypatch):
    service, _, destination = harness
    store = service.r.store
    await output_run(store, "10", ["Waiting"])
    approval_started = asyncio.Event()
    sending = asyncio.Event()

    async def reconcile():
        approval_started.set()
        await asyncio.Event().wait()

    async def send(*args, **kwargs):
        sending.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(service.approvals, "reconcile", reconcile)
    destination.send.side_effect = send
    lease = AsyncMock(wraps=store.lease)
    monkeypatch.setattr(store, "lease", lease)
    await service.scheduler.start()
    await asyncio.wait_for(approval_started.wait(), 5)
    await asyncio.wait_for(sending.wait(), 5)
    service.scheduler._wakeup.set()
    await until(lambda: lease.await_count >= 2)
    await service.close()
    # Cancellation during a send is left ambiguous for restart recovery.
    await store.recover()
    assert (await store.deliveries()) == []
