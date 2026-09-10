from __future__ import annotations

import asyncio
import json
import socket
import sqlite3
import time
from contextlib import closing
from types import SimpleNamespace
from datetime import timedelta
from unittest.mock import AsyncMock

import aiohttp
import discord
import pytest

import app.task_reads as reads
from discord_adapter.gateway import _search_channel_accessible
from storage.db import Database
from storage.scheduled_tasks import ScheduledTaskStore
from tests.test_scheduled_tasks import active_task, definition, store as store
from tests.test_task_controls import harness as harness
from tests.test_task_python import (
    DISCORD_INPUT,
    NOW,
    python_harness as python_harness,
    run_task,
)
from tools.downloads import FetchUrlError


def http_error(status, retry_after=None):
    headers = {} if retry_after is None else {"Retry-After": retry_after}
    return discord.HTTPException(
        SimpleNamespace(status=status, reason="unavailable", headers=headers), "unavailable"
    )


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504, 400, 401, 403, 404, 501])
def test_http_read_classification(status):
    for exc in (
        http_error(status, "7"),
        FetchUrlError("not inspected", status=status, retry_after="7"),
    ):
        failure = reads.read_error(exc)
        assert failure.status == status
        assert failure.retryable is (status in reads.RETRY_STATUSES)
        assert failure.retry_after == 7


@pytest.mark.parametrize(
    "exc,retryable",
    [
        (TimeoutError(), True),
        (ConnectionResetError(), True),
        (aiohttp.ServerDisconnectedError(), True),
        (socket.gaierror(socket.EAI_AGAIN, "unrelated text"), True),
        (socket.gaierror(socket.EAI_NONAME, "timed out"), False),
        (FetchUrlError("HTTP 503 timed out"), False),
        (ValueError("temporary network error"), False),
        (PermissionError("denied"), False),
    ],
)
def test_read_classification_preserves_typed_causes(exc, retryable):
    wrapper = ValueError("Could not read channel")
    wrapper.__cause__ = exc
    failure = reads.read_error(wrapper)
    assert failure.retryable is retryable
    assert failure.cause is exc


@pytest.mark.asyncio
async def test_retry_budget_and_retry_after(monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr(reads.asyncio, "sleep", sleep)
    read = AsyncMock(side_effect=[http_error(503), http_error(429, "6"), "result"])
    assert await reads.retry_read(read) == "result"
    assert [call.args[0] for call in sleep.await_args_list] == [1, 6]
    read = AsyncMock(side_effect=http_error(503))
    with pytest.raises(reads.TaskReadError) as failure:
        await reads.retry_read(read)
    assert failure.value.retryable
    assert read.await_count == 3
    sleep.reset_mock()
    read = AsyncMock(side_effect=http_error(429, "61"))
    with pytest.raises(reads.TaskReadError):
        await reads.retry_read(read)
    assert read.await_count == 1
    sleep.assert_not_awaited()
    read = AsyncMock(side_effect=http_error(403))
    with pytest.raises(reads.TaskReadError) as failure:
        await reads.retry_read(read)
    assert not failure.value.retryable
    assert read.await_count == 1


@pytest.mark.asyncio
async def test_read_deadline_and_cancellation():
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def read():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    with pytest.raises(reads.TaskReadError) as failure:
        await reads.retry_read(read, timeout_seconds=0.01)
    assert failure.value.retryable and stopped.is_set()
    started.clear()
    operation = asyncio.create_task(reads.retry_read(read))
    await started.wait()
    operation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await operation


@pytest.mark.asyncio
async def test_private_thread_transient_membership_failure_is_not_denial():
    permissions = SimpleNamespace(
        view_channel=True, read_message_history=True, manage_threads=False
    )
    channel = SimpleNamespace(
        type=discord.ChannelType.private_thread,
        permissions_for=lambda actor: permissions,
        fetch_member=AsyncMock(side_effect=http_error(503)),
    )
    member = SimpleNamespace(id=1)
    with pytest.raises(discord.HTTPException):
        await _search_channel_accessible(channel, member, member, propagate_http_errors=True)
    channel.fetch_member.side_effect = discord.Forbidden(
        SimpleNamespace(status=403, reason="denied"), "denied"
    )
    assert not await _search_channel_accessible(channel, member, member, propagate_http_errors=True)


async def failure_occurrence(store, task, next_run):
    run_id = await store.claim(task, next_run)
    assert run_id is not None
    await store.finish(
        run_id,
        "read_failed",
        "Server unavailable",
        task["state"],
        [{"channel_id": "301", "is_log": True, "content": "Occurrence failed"}],
    )
    return run_id


async def outbox(store):
    async with store.db.conn.execute(
        "SELECT payload_json FROM scheduled_task_deliveries ORDER BY id"
    ) as cursor:
        return [json.loads(row[0]) for row in await cursor.fetchall()]


@pytest.mark.asyncio
async def test_outage_survives_restart_and_recovers_only_after_all_posts(store):
    task = await active_task(store)
    run_id = await store.claim(task, 200)
    state = {"_task_inputs": {"discussion": "previous"}, "release": 1}
    await store.finish(run_id, "no_change", "baseline", state, [])
    task = await store.get(task["id"], active=True)
    await failure_occurrence(store, task, 300)
    await store.db.close()
    await store.db.connect()
    task = await store.get(task["id"], active=True)
    await failure_occurrence(store, task, 400)
    task = await store.get(task["id"], active=True)
    assert task["status"] == "active" and task["next_run"] == 400
    assert task["read_failure_streak"] == 2 and task["state"] == state
    assert len(await outbox(store)) == 3  # One home notice plus a log for each failure.
    run_id = await store.claim(task, 500)
    await store.finish(
        run_id,
        "delivery",
        "Read recovered",
        {"release": 2},
        [{"channel_id": "300", "content": "first"}, {"channel_id": "300", "content": "second"}],
    )
    pending = [row for row in await store.deliveries() if not row["is_log"]]
    await store.delivery_status(pending[0]["id"], "sent", message_id="900")
    assert (await store.get(task["id"]))["read_failure_streak"] == 2
    await store.delivery_status(pending[1]["id"], "sent", message_id="901")
    task = await store.get(task["id"], active=True)
    assert task["read_failure_streak"] == 0 and task["state"] == {"release": 2}
    notices = [row for row in await outbox(store) if row.get("management")]
    assert len(notices) == 2 and "recovered" in notices[1]["content"]
    # Replayed delivery receipts cannot duplicate recovery notifications.
    await store.delivery_status(pending[1]["id"], "sent", message_id="901")
    assert len(await outbox(store)) == 6
    await failure_occurrence(store, task, 600)
    assert len([row for row in await outbox(store) if row.get("management")]) == 3


@pytest.mark.asyncio
async def test_one_off_read_failure_requires_attention(store):
    task = await active_task(
        store, schedule={"kind": "once", "start": "2030-01-01T09:00:00Z", "timezone": "UTC"}
    )
    await failure_occurrence(store, task, None)
    current = await store.get(task["id"])
    assert current["status"] == "attention" and current["next_run"] is None
    assert "needs attention" in (await outbox(store))[-1]["content"]


@pytest.mark.asyncio
async def test_skipped_occurrence_does_not_report_recovery_and_reapproval_clears_outage(store):
    task = await active_task(store)
    await failure_occurrence(store, task, 200)
    task = await store.get(task["id"], active=True)
    run_id = await store.claim(task, 300)
    await store.finish(run_id, "no_change", "Skipped", task["state"], [], recover_reads=False)
    task = await store.get(task["id"], active=True)
    assert task["read_failure_streak"] == 1
    run_id = await store.claim(task, 400)
    await store.draft(
        task_id=task["id"],
        guild_id="100",
        owner_id=task["owner_id"],
        channel_id="200",
        proposer_id=task["proposer_id"],
        expected_revision=1,
        definition=definition(name="New name"),
    )
    await store.activate(task["id"], 2, task["proposer_id"], 500, reset_state=False)
    await store.finish(run_id, "read_failed", "Old definition", {}, [])
    task = await store.get(task["id"], active=True)
    assert task["read_failure_streak"] == 0 and task["next_run"] == 500
    assert len(await outbox(store)) == 2


@pytest.mark.asyncio
async def test_v12_upgrade_adds_empty_streak_without_changing_approved_task(tmp_path):
    path = tmp_path / "upgrade.db"
    db = Database(path)
    await db.connect()
    store = ScheduledTaskStore(db)
    task = await active_task(store)
    await db.close()
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("ALTER TABLE scheduled_tasks DROP COLUMN read_failure_streak")
        connection.execute("DELETE FROM schema_version WHERE version=13")
    await db.connect()
    try:
        assert await store.get(task["id"]) == task
        await db.close()
        await db.connect()
        assert (await store.get(task["id"]))["read_failure_streak"] == 0
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_input_retry_restarts_partial_history_with_fixed_window(python_harness, monkeypatch):
    monkeypatch.setattr(reads, "RETRY_DELAYS", (0, 0))
    box = python_harness
    message = {"id": "42", "timestamp": NOW.isoformat(), "content": "outside exclusive upper bound"}
    history = box.service.r.gateway.collect_channel_history
    history.side_effect = [
        {"messages": [message], "has_more": True, "next_cursor": "42"},
        http_error(503),
        {"messages": [], "has_more": False, "next_cursor": None},
    ]
    task = await run_task(box, inputs=[DISCORD_INPUT])
    assert task["status"] == "active" and task["read_failure_streak"] == 0
    assert box.process.await_count == 1
    calls = [call.args[1] for call in history.await_args_list]
    assert calls[0] == calls[2] and calls[1]["cursor"] == "42"
    assert len({call["before"] for call in calls}) == 1
    assert box.requests[0].input["inputs"]["discussion"]["count"] == 0


@pytest.mark.asyncio
async def test_preflight_retries_before_model_execution(harness, monkeypatch):
    monkeypatch.setattr(reads, "RETRY_DELAYS", (0, 0))
    service, _, _ = harness
    preflight = AsyncMock(side_effect=[http_error(503), http_error(503), None])
    monkeypatch.setattr(service.authority, "validate_definition", preflight)
    task = await active_task(service.r.store)
    execute = AsyncMock(side_effect=TimeoutError("Model has already started"))
    monkeypatch.setattr(service.executor, "execute", execute)
    await service.r.store.lease(service.authority.token, time.time())
    await service.scheduler.run(task["id"])
    assert preflight.await_count == 3 and execute.await_count == 1
    assert (await service.r.store.get(task["id"]))["status"] == "attention"


@pytest.mark.asyncio
async def test_failure_after_python_started_does_not_retry_or_keep_active(
    python_harness, monkeypatch
):
    monkeypatch.setattr(reads, "RETRY_DELAYS", (0, 0))
    box = python_harness
    box.process.side_effect = TimeoutError("Sandbox timed out")
    task = await run_task(box)
    assert box.process.await_count == 1
    assert task["status"] == "attention" and task["read_failure_streak"] == 0


@pytest.mark.asyncio
async def test_exhausted_inputs_keep_state_and_each_run_log(python_harness, monkeypatch):
    monkeypatch.setattr(reads, "RETRY_DELAYS", (0, 0))
    box = python_harness
    history = box.service.r.gateway.collect_channel_history
    task = await run_task(box, inputs=[DISCORD_INPUT], log_channel="300")
    original_state = task["state"]
    monkeypatch.setattr("tests.test_task_python.NOW", NOW + timedelta(hours=1))
    history.reset_mock()
    history.side_effect = http_error(503)
    for streak in (1, 2):
        await box.service.r.store.set_status(task["id"], "active", next_run=1000 + streak)
        await box.service.scheduler.run(task["id"])
        task = await box.service.r.store.get(task["id"], active=True)
        assert task["status"] == "active" and task["read_failure_streak"] == streak
        assert task["state"] == original_state
    assert history.await_count == 6 and box.process.await_count == 1
    records = await outbox(box.service.r.store)
    assert len([row for row in records if row.get("management")]) == 1
    assert len([row for row in records if not row.get("management")]) == 3
    history.side_effect = None
    await box.service.r.store.set_status(task["id"], "active", next_run=2000)
    await box.service.scheduler.run(task["id"])
    assert (await box.service.r.store.get(task["id"]))["read_failure_streak"] == 0
    assert box.process.await_count == 2


@pytest.mark.asyncio
async def test_private_thread_recipient_preflight_retries_membership(harness, monkeypatch):
    from app.task_access import TaskAccess

    monkeypatch.setattr(reads, "RETRY_DELAYS", (0, 0))
    service, _, destination = harness
    access = TaskAccess(service.r.bot, service.r.settings, None, lambda: {100})
    member = SimpleNamespace(id=11)
    bot_member = SimpleNamespace(id=99)
    destination.guild.me = bot_member
    destination.guild.fetch_member = AsyncMock(return_value=member)
    destination.type = discord.ChannelType.private_thread
    destination.permissions_for = lambda actor: SimpleNamespace(
        view_channel=True, read_message_history=True, manage_threads=False
    )
    destination.fetch_member = AsyncMock(side_effect=http_error(503))
    monkeypatch.setattr(service.r.access, "mentions", access.mentions)
    task = await active_task(service.r.store, mention_users=["11"])
    execute = AsyncMock()
    monkeypatch.setattr(service.executor, "execute", execute)
    await service.r.store.lease(service.authority.token, time.time())
    await service.scheduler.run(task["id"])
    current = await service.r.store.get(task["id"])
    assert current["status"] == "active" and current["read_failure_streak"] == 1
    assert destination.fetch_member.await_count == 3
    assert (await service.r.store.history(task["id"]))[0]["status"] == "read_failed"
    execute.assert_not_awaited()
