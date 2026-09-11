"""Published-result outbox boundaries, recovery, permissions, and retained-file access."""

from __future__ import annotations

import asyncio
import dataclasses
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from app.scheduled_tasks import ScheduledTaskService
from kimi_agent_module_api import ModulePermissions, ScheduledResultAccessError
from modules.health import HealthRegistry
from modules.scheduled_results import ScheduledResultRuntime
from storage.db import Database
from storage.scheduled_tasks import ScheduledTaskStore
from storage.task_results import TaskResultStore
from tests.test_scheduled_tasks import active_task, complete_runtime, context
from utils.privacy_barrier import UserPrivacyBarrier


@pytest_asyncio.fixture
async def store(tmp_path):
    db = Database(tmp_path / "results.db")
    await db.connect()
    try:
        yield ScheduledTaskStore(db)
    finally:
        await db.close()


def runtime(store, **kwargs):
    return ScheduledResultRuntime(
        TaskResultStore(store.db),
        check_access=kwargs.pop("check_access", AsyncMock()),
        privacy=kwargs.pop("privacy", UserPrivacyBarrier()),
        health=HealthRegistry(),
        **kwargs,
    )


async def subscribe(worker, handler, *, module="report", guild_id=100, active=lambda _: True):
    view = worker.view_for(module, ModulePermissions(scheduled_results=("digest",)), active)
    await view.subscribe("digest", guild_id=guild_id, handler=handler)
    return view


async def publish(store, *, posts=None):
    task = await active_task(store)
    run_id = await store.claim(task, 100.0)
    await store.finish(
        run_id,
        "delivery",
        "PRIVATE DETAIL",
        {"secret": "PRIVATE STATE"},
        posts or [{"channel_id": "300", "content": "Published"}],
    )
    for delivery in await store.deliveries():
        await store.delivery_status(delivery["id"], "sent", message_id=str(900 + delivery["id"]))
    return task, run_id


async def retry_now(store):
    async with store.db.write_transaction() as conn:
        await conn.execute("UPDATE scheduled_result_notifications SET retry_at=0,lease_until=0")


@pytest.mark.asyncio
async def test_complete_publication_only_and_public_payload(store):
    worker = runtime(store)
    received, readers = [], []

    async def handler(result, files):
        received.append(result)
        readers.append(files)
        assert await files.read(result.messages[0].attachments[0].id) == b"published file"
        with pytest.raises(ScheduledResultAccessError):
            await files.read(str(hidden_id))

    await subscribe(worker, handler)
    other = AsyncMock()
    await subscribe(worker, other, module="other_guild", guild_id=200)
    task = await active_task(store)
    run_id = await store.claim(task, 100.0)
    file_id, hidden_id = await store.save_files(
        run_id,
        [
            ("result.txt", "Published attachment", b"published file"),
            ("private.txt", None, b"never published"),
        ],
    )
    await store.finish(
        run_id,
        "delivery",
        "PRIVATE DETAIL",
        {"secret": "PRIVATE STATE"},
        [
            {"channel_id": "300", "content": "First", "file_ids": [file_id]},
            {"channel_id": "301", "content": "Second"},
            {"channel_id": "400", "content": "Log only", "is_log": True},
        ],
    )
    first, second = await store.deliveries()
    await store.delivery_status(
        first["id"], "sent", message_id="900", published_embed={"title": "Published embed"}
    )
    await worker.dispatch_once()
    assert received == []
    await store.delivery_status(second["id"], "failed")
    await worker.dispatch_once()
    assert received == []
    await store.delivery_status(second["id"], "sent", message_id="901")
    await worker.dispatch_once()
    assert len(received) == 1
    result = received[0]
    assert (result.task_id, result.run_id, result.revision) == (task["id"], run_id, 1)
    assert (result.guild_id, result.owner_id, result.outcome) == (100, 10, "completed")
    assert [m.content for m in result.messages] == ["First", "Second"]
    assert [m.message.message_id for m in result.messages] == [900, 901]
    assert json.loads(result.messages[0].embed_json) == {"title": "Published embed"}
    assert result.messages[0].attachments[0].size_bytes == len(b"published file")
    assert "PRIVATE" not in repr(dataclasses.asdict(result))
    assert "Log only" not in repr(result)
    assert (await store.history(task["id"]))[0]["status"] == "completed"
    # A lost acknowledgement of a Discord delivery must not enqueue a second notification.
    await store.delivery_status(second["id"], "sent", message_id="901")
    await worker.dispatch_once()
    assert len(received) == 1
    other.assert_not_awaited()
    with pytest.raises(ScheduledResultAccessError):
        await readers[0].read(str(file_id))


@pytest.mark.asyncio
async def test_subscriber_failure_retries_after_connection_restart_without_republishing(store):
    first = runtime(store)
    failed = AsyncMock(side_effect=RuntimeError("module secret must not enter history"))
    await subscribe(first, failed)
    task, _ = await publish(store)
    before = await store.history(task["id"])
    await first.dispatch_once()
    history = await first.store.history(task["id"])
    assert history[0]["status"] == "retry"
    assert "module secret" not in repr(history)
    notification_id = history[0]["id"]
    path = store.db._path
    await store.db.close()
    store.db = Database(path)
    await store.db.connect()
    try:
        recovered = runtime(store)
        success = AsyncMock()
        await subscribe(recovered, success)
        await retry_now(store)
        await recovered.dispatch_once()
        assert success.call_args.args[0].notification_id == notification_id
        assert failed.call_args.args[0].notification_id == notification_id
        assert (await recovered.store.history(task["id"]))[0]["status"] == "acknowledged"
        assert await store.history(task["id"]) == before
        assert await store.deliveries() == []
        await recovered.dispatch_once()
        success.assert_awaited_once()
    finally:
        await store.db.close()


@pytest.mark.asyncio
async def test_lease_recovery_fences_stale_acknowledgement(store):
    worker = runtime(store)
    await subscribe(worker, AsyncMock())
    task, _ = await publish(store)
    old = (await worker.store.claim())[0]
    assert await worker.store.claim() == []
    await retry_now(store)
    new = (await worker.store.claim())[0]
    assert old["id"] == new["id"] and old["lease_token"] != new["lease_token"]
    await worker.store.settle(old, "acknowledged")
    assert (await worker.store.history(task["id"]))[0]["status"] == "delivering"
    await worker.store.settle(new, "acknowledged")
    assert (await worker.store.history(task["id"]))[0]["status"] == "acknowledged"


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["inactive", "unavailable", "permission", "access"])
async def test_current_availability_and_access_rechecked_on_retry(store, reason):
    check = AsyncMock()
    worker = runtime(store, check_access=check)
    handler = AsyncMock()
    view = await subscribe(worker, handler)
    task, _ = await publish(store)
    if reason == "inactive":
        view.is_guild_active = lambda _: False
    elif reason == "unavailable":
        worker.unregister_module("report")
    elif reason == "permission":
        view.permissions = ModulePermissions()
    else:
        check.side_effect = ValueError("no longer permitted")
    await worker.dispatch_once()
    handler.assert_not_awaited()
    assert (await worker.store.history(task["id"]))[0]["status"] == "blocked"
    check.side_effect = None
    await subscribe(worker, handler)
    await retry_now(store)
    await worker.dispatch_once()
    handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_new_subscription_does_not_replay_history_and_requires_declaration(store):
    task, _ = await publish(store)
    worker = runtime(store)
    handler = AsyncMock()
    view = await subscribe(worker, handler)
    await worker.dispatch_once()
    handler.assert_not_awaited()
    assert await worker.store.history(task["id"]) == []
    with pytest.raises(ScheduledResultAccessError):
        await view.subscribe("undeclared", guild_id=100, handler=handler)
    for guild_id in (0, -1, True):
        with pytest.raises(ValueError):
            await view.subscribe("digest", guild_id=guild_id, handler=handler)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["task", "owner", "unsubscribe", "retention"])
async def test_deletion_and_retention_remove_notifications_and_revoke_files(store, action):
    worker = runtime(store)
    view = await subscribe(worker, AsyncMock())
    task = await active_task(store)
    run_id = await store.claim(task, 100)
    file_id = (await store.save_files(run_id, [("result.txt", None, b"retained")]))[0]
    await store.finish(
        run_id,
        "delivery",
        "",
        {},
        [
            {"channel_id": "300", "content": "Report", "file_ids": [file_id]},
        ],
    )
    delivery = (await store.deliveries())[0]
    await store.delivery_status(delivery["id"], "sent", message_id="900")
    row = (await worker.store.claim())[0]
    assert await worker.store.read_file(row, str(file_id), 100) == b"retained"
    if action == "task":
        await store.delete(task["id"])
    elif action == "owner":
        await store.delete_owner("10")
    elif action == "unsubscribe":
        await view.unsubscribe("digest", guild_id=100)
    else:
        async with store.db.write_transaction() as conn:
            await conn.execute("UPDATE scheduled_result_notifications SET expires_at=0")
        await worker.store.prune()
    assert not await worker.store.live(row)
    assert await worker.store.history(task["id"]) == []
    with pytest.raises(ValueError):
        await worker.store.read_file(row, str(file_id), 100)
    assert await worker.store.claim() == []


@pytest.mark.asyncio
async def test_attachment_reads_recheck_access_and_enforce_size_budget(store):
    worker = runtime(store)
    task = await active_task(store)
    run_id = await store.claim(task, 100)
    file_id = (await store.save_files(run_id, [("report.txt", None, b"abc")]))[0]

    async def handler(result, files):
        with pytest.raises(ScheduledResultAccessError):
            await files.read(str(file_id), max_bytes=2)
        assert await files.read(str(file_id), max_bytes=3) == b"abc"
        files.remaining = 2
        with pytest.raises(ScheduledResultAccessError):
            await files.read(str(file_id))
        files.remaining = 100
        worker.check_access.side_effect = ValueError("visibility revoked")
        with pytest.raises(ScheduledResultAccessError):
            await files.read(str(file_id))

    await subscribe(worker, handler)
    await store.finish(
        run_id,
        "delivery",
        "",
        {},
        [
            {"channel_id": "300", "content": "Report", "file_ids": [file_id]},
        ],
    )
    delivery = (await store.deliveries())[0]
    await store.delivery_status(delivery["id"], "sent", message_id="900")
    await worker.dispatch_once()
    assert (await worker.store.history(task["id"]))[0]["status"] == "acknowledged"


@pytest.mark.asyncio
async def test_timeout_retries_and_closes_retained_reader(store):
    worker = runtime(store, timeout=0.01, cancel_grace=0.01)
    readers = []

    async def handler(_result, files):
        readers.append(files)
        await asyncio.Event().wait()

    await subscribe(worker, handler)
    task, _ = await publish(store)
    await worker.dispatch_once()
    assert (await worker.store.history(task["id"]))[0]["status"] == "retry"
    assert readers and not readers[0].open


@pytest.mark.asyncio
async def test_ignored_cancellation_pauses_module_without_accumulating_handlers(store):
    worker = runtime(store, timeout=0.01, cancel_grace=0.01)
    release = asyncio.Event()
    invocations, readers = [], []

    async def stubborn(result, files):
        invocations.append(result.notification_id)
        readers.append(files)
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue

    await subscribe(worker, stubborn)
    task, _ = await publish(store)
    try:
        await worker.dispatch_once()
        assert worker.health.get("report").state == "failed"
        assert (await worker.store.history(task["id"]))[0]["status"] == "blocked"
        assert len(invocations) == 1 and not readers[0].open
        healthy = AsyncMock()
        await subscribe(worker, healthy, module="healthy")
        await publish(store)
        for _ in range(2):
            await retry_now(store)
            await worker.dispatch_once()
        assert len(invocations) == 1
        healthy.assert_awaited_once()
        assert sum(map(len, worker._invocations.values())) == 1
    finally:
        release.set()
        await asyncio.gather(*(t for tasks in worker._invocations.values() for t in tasks))
        await worker.close()


@pytest.mark.asyncio
async def test_healthy_notification_does_not_clear_another_failure(store):
    worker = runtime(store)
    calls = 0

    async def handler(_result, _files):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("first result failed")

    await subscribe(worker, handler)
    await publish(store)
    await worker.dispatch_once()
    await publish(store)
    await worker.dispatch_once()
    assert calls == 2
    state = worker.health.get("report")
    assert state.state == "degraded"
    assert state.metrics["results_retry"] == 1
    assert state.metrics["results_acknowledged"] == 1


@pytest.mark.asyncio
async def test_pending_privacy_deletion_prevents_callback(store):
    privacy = UserPrivacyBarrier()
    worker = runtime(store, privacy=privacy)
    handler = AsyncMock()
    await subscribe(worker, handler)
    task, _ = await publish(store)
    await privacy.mark_deletion_pending("10")
    await worker.dispatch_once()
    handler.assert_not_awaited()
    assert (await worker.store.history(task["id"]))[0]["status"] != "acknowledged"


@pytest.mark.asyncio
async def test_application_access_rechecks_owner_and_every_destination(store):
    worker = runtime(store)
    await subscribe(worker, AsyncMock())
    await publish(
        store,
        posts=[
            {"channel_id": "300", "content": "A"},
            {"channel_id": "301", "content": "B"},
        ],
    )
    row = (await worker.store.claim())[0]
    access = SimpleNamespace(
        context=AsyncMock(return_value=context()), owner_allowed=AsyncMock(), channel=AsyncMock()
    )
    service = ScheduledTaskService(complete_runtime(SimpleNamespace(store=store, access=access)))
    await service.check_result_access(row)
    access.owner_allowed.assert_awaited_once()
    assert {c.args[1] for c in access.channel.call_args_list} == {"300", "301"}
    assert all(c.kwargs == {"posting": False} for c in access.channel.call_args_list)
    service.r.user_blocked.return_value = True
    with pytest.raises(ValueError):
        await service.check_result_access(row)


@pytest.mark.asyncio
async def test_large_published_embed_does_not_poison_notification(store):
    worker = runtime(store)
    handler = AsyncMock()
    await subscribe(worker, handler)
    task = await active_task(store)
    run_id = await store.claim(task, 100)
    await store.finish(run_id, "delivery", "", {}, [{"channel_id": "300", "content": "Report"}])
    delivery = (await store.deliveries())[0]
    url = "https://example.com/" + "x" * (2048 - len("https://example.com/"))
    embed = {
        "title": "x",
        "description": "😀" * 4096,
        "url": url,
        "footer": {"text": "😀" * 1902, "icon_url": url},
        "author": {"name": "x", "url": url, "icon_url": url},
        "image": {"url": url},
        "thumbnail": {"url": url},
    }
    assert len(json.dumps(embed, ensure_ascii=False).encode()) > 32768
    await store.delivery_status(delivery["id"], "sent", message_id="900", published_embed=embed)
    await worker.dispatch_once()
    handler.assert_awaited_once()
    result = handler.call_args.args[0]
    assert result.messages[0].content == "Report"
    assert result.messages[0].message.message_id == 900
    assert result.messages[0].embed_json is None
    assert result.messages[0].embed_unavailable_reason
    assert (await worker.store.history(task["id"]))[0]["status"] == "acknowledged"


@pytest.mark.asyncio
async def test_privacy_deletion_waits_for_active_callback_then_removes_its_data(store):
    privacy = UserPrivacyBarrier()
    worker = runtime(store, privacy=privacy)
    entered, release, deleting = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def handler(_result, _files):
        entered.set()
        await release.wait()

    async def delete_owner():
        async with privacy.deletion("10"):
            deleting.set()
            await store.delete_owner("10")

    await subscribe(worker, handler)
    task, _ = await publish(store)
    dispatch = asyncio.create_task(worker.dispatch_once())
    await asyncio.wait_for(entered.wait(), 1)
    deletion = asyncio.create_task(delete_owner())
    try:
        await asyncio.sleep(0)
        assert not deleting.is_set()
        release.set()
        await asyncio.wait_for(asyncio.gather(dispatch, deletion), 1)
        assert deleting.is_set()
        assert await worker.store.history(task["id"]) == []
    finally:
        release.set()
        await asyncio.gather(dispatch, deletion)


@pytest.mark.asyncio
async def test_publication_confirmation_and_notification_rollback_together(store, monkeypatch):
    import storage.scheduled_tasks as task_store_module

    worker = runtime(store)
    await subscribe(worker, AsyncMock())
    task = await active_task(store)
    run_id = await store.claim(task, 100)
    await store.finish(
        run_id, "delivery", "", {"cursor": "next"}, [{"channel_id": "300", "content": "Report"}]
    )
    delivery = (await store.deliveries())[0]
    await store.delivery_status(delivery["id"], "sending")
    enqueue = task_store_module.enqueue_results

    async def interrupted(conn, run, now):
        await enqueue(conn, run, now)
        raise RuntimeError("process interrupted before commit")

    monkeypatch.setattr(task_store_module, "enqueue_results", interrupted)
    with pytest.raises(RuntimeError, match="before commit"):
        await store.delivery_status(delivery["id"], "sent", message_id="900")
    history = await store.task_history(task["id"])
    assert history["notifications"] == []
    assert history["runs"][0]["status"] == "delivery"
    assert history["deliveries"][0]["status"] == "sending"
    assert (await store.get(task["id"]))["state"] == {}


@pytest.mark.asyncio
async def test_v15_upgrade_preserves_completed_history_without_replay(store):
    task, run_id = await publish(store)
    before = await store.history(task["id"])
    # Remove exactly the v16 additions to construct the preceding schema.
    async with store.db.write_transaction() as conn:
        await conn.execute("DROP TABLE scheduled_result_notifications")
        await conn.execute("DROP TABLE scheduled_result_subscriptions")
        await conn.execute("DELETE FROM schema_version WHERE version=16")
    await store.db.close()
    await store.db.connect()
    assert await store.history(task["id"]) == before
    assert (await store.history(task["id"]))[0]["id"] == run_id
    worker = runtime(store)
    handler = AsyncMock()
    await subscribe(worker, handler)
    await worker.dispatch_once()
    handler.assert_not_awaited()
    assert await worker.store.history(task["id"]) == []
    async with store.db.conn.execute("SELECT name FROM schema_version WHERE version=16") as cursor:
        assert (await cursor.fetchone())[0] == "scheduled_results"
