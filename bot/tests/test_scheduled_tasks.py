from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from pydantic import ValidationError

from app.scheduled_tasks import ScheduledTaskService
from app.task_access import TaskAccess, TaskPolicy, may_manage
from storage.db import Database
from storage.scheduled_tasks import ScheduledTaskStore
from tools.registry import MessageContext, ToolRegistry, TurnOutbox
from tools.workspace.common import UserLocks
from storage.conversations import ConversationStore
from discord_adapter.gateway import DiscordGateway
import app.scheduled_tasks as scheduled_module
from tools.scheduled_tasks import TaskDefinition, init_task_tools
from trust.tiers import TrustTier
from utils.schedules import Schedule


@pytest_asyncio.fixture
async def store(tmp_path):
    database = Database(tmp_path / "tasks.db")
    await database.connect()
    try:
        yield ScheduledTaskStore(database)
    finally:
        await database.close()


def definition(**changes):
    return TaskDefinition.model_validate(
        {
            "name": "Development digest",
            "objective": "Summarize changes",
            "sources": ["200"],
            "skill": "Read channel 200 since the saved cursor. Post only when changes exist.",
            "schedule": {
                "kind": "daily",
                "start": "2030-01-01T09:00:00+01:00",
                "timezone": "Europe/Berlin",
            },
            "destinations": ["300"],
            **changes,
        }
    ).model_dump(mode="json")


async def active_task(store, **changes):
    task_id = await store.draft(
        task_id=None,
        guild_id="100",
        owner_id="10",
        channel_id="200",
        proposer_id="10",
        definition=definition(**changes),
    )
    await store.activate(task_id, 1, "10", 1.0, reset_state=False)
    return await store.get(task_id, active=True)


def context(**changes):
    return MessageContext(
        **{
            "user_id": "10",
            "user_name": "Owner",
            "guild_id": "100",
            "channel_id": "200",
            "thread_id": None,
            "trust_tier": TrustTier.MEMBER,
            **changes,
        }
    )


def test_calendar_skips_dst_gap_and_fires_fold_once():
    schedule = Schedule(
        kind="daily",
        start=datetime.fromisoformat("2026-03-28T02:30:00+01:00"),
        timezone="Europe/Berlin",
    )
    assert schedule.preview(datetime.fromisoformat("2026-03-28T03:00:00+01:00").timestamp(), 1) == [
        "2026-03-30T02:30:00+02:00"
    ]
    after_first_fold = datetime.fromisoformat("2026-10-25T02:30:00+02:00").timestamp()
    assert schedule.preview(after_first_fold, 1) == ["2026-10-26T02:30:00+01:00"]


def test_monthly_skips_missing_dates_and_intervals_do_not_drift():
    monthly = Schedule(kind="monthly", start=datetime(2030, 1, 31, 9, tzinfo=UTC), month_day=31)
    assert monthly.preview(datetime(2030, 2, 1, tzinfo=UTC).timestamp(), 1) == [
        "2030-03-31T09:00:00+00:00"
    ]
    interval = Schedule(
        kind="interval", start=datetime(2030, 1, 1, tzinfo=UTC), interval_seconds=3600
    )
    start = interval.start.timestamp()
    assert interval.next_after(start + 3601) == start + 7200


def test_task_skill_and_policy_validation():
    with pytest.raises(ValidationError, match="Executable"):
        definition(skill="---\ntools: []\n---\nExecute arbitrary tools")
    with pytest.raises(ValidationError):
        definition(skill="   ")
    with pytest.raises(ValidationError):
        TaskPolicy(enabled="yes")
    with pytest.raises(ValidationError):
        definition(mention_roles=["everyone"])


@pytest.mark.asyncio
async def test_confirm_is_revision_bound_and_cannot_be_replayed(store):
    task = await active_task(store)
    with pytest.raises(ValueError, match="stale"):
        await store.activate(task["id"], 1, "10", 100.0, reset_state=True)
    await store.draft(
        task_id=task["id"],
        guild_id="100",
        owner_id="10",
        channel_id="200",
        proposer_id="20",
        definition=definition(name="Updated"),
        expected_revision=1,
    )
    with pytest.raises(ValueError):
        await store.activate(task["id"], 2, "10", 100.0, reset_state=False)
    assert (await store.get(task["id"], active=True))["definition"]["name"] == "Development digest"
    await store.activate(task["id"], 2, "20", 100.0, reset_state=False)
    assert (await store.get(task["id"], active=True))["definition"]["name"] == "Updated"


@pytest.mark.asyncio
async def test_only_one_occurrence_can_be_claimed(store):
    task = await active_task(store)
    claims = await asyncio.gather(store.claim(task, 100.0), store.claim(task, 100.0))
    assert sum(claim is not None for claim in claims) == 1


@pytest.mark.asyncio
async def test_delivery_commits_state_after_all_output_but_not_logs(store):
    task = await active_task(store)
    run_id = await store.claim(task, 100.0)
    await store.finish(
        run_id,
        "delivery",
        "Changed",
        {"cursor": "900"},
        [
            {"channel_id": "300", "content": "Update"},
            {"channel_id": "400", "content": "Check log", "is_log": True},
        ],
    )
    assert (await store.get(task["id"]))["state"] == {}
    pending = await store.deliveries()
    assert len(pending) == 1
    assert await store.lease("worker", time.time())
    assert await store.begin_delivery(pending[0]["id"], "worker")
    await store.delivery_status(pending[0]["id"], "sent", message_id="500")
    assert (await store.get(task["id"]))["state"] == {"cursor": "900"}
    logs = await store.deliveries()
    assert len(logs) == 1 and logs[0]["is_log"]
    await store.delivery_status(logs[0]["id"], "failed", error="Log channel unavailable")
    assert (await store.get(task["id"]))["state"] == {"cursor": "900"}


@pytest.mark.asyncio
async def test_silent_check_has_history_but_no_deliveries(store):
    task = await active_task(store)
    run_id = await store.claim(task, 100.0)
    await store.finish(run_id, "no_change", "No changes", {"cursor": "800"}, [])
    assert await store.deliveries() == []
    assert (await store.history(task["id"]))[0]["status"] == "no_change"
    assert (await store.get(task["id"]))["state"] == {"cursor": "800"}


@pytest.mark.asyncio
async def test_state_reset_fences_old_run(store):
    task = await active_task(store)
    run_id = await store.claim(task, 100.0)
    await store.draft(
        task_id=task["id"],
        guild_id="100",
        owner_id="10",
        channel_id="200",
        proposer_id="10",
        definition=definition(),
        expected_revision=1,
    )
    await store.activate(task["id"], 2, "10", 200.0, reset_state=True)
    await store.finish(run_id, "no_change", "Old observation", {"old": True}, [])
    assert (await store.get(task["id"]))["state"] == {}


@pytest.mark.asyncio
async def test_pausing_prevents_delivery_claim_and_late_state_writes(store):
    task = await active_task(store)
    run_id = await store.claim(task, 100.0)
    await store.finish(
        run_id,
        "delivery",
        "Changed",
        {"cursor": "900"},
        [{"channel_id": "300", "content": "Update"}],
    )
    delivery = (await store.deliveries())[0]
    await store.lease("worker", time.time())
    await store.set_status(task["id"], "paused")
    assert not await store.begin_delivery(delivery["id"], "worker")
    assert (await store.get(task["id"]))["state"] == {}


@pytest.mark.asyncio
async def test_restart_does_not_replay_running_actions_or_uncertain_sends(store):
    task = await active_task(store)
    run_id = await store.claim(task, 100.0)
    await store.recover()
    assert (await store.get(task["id"]))["status"] == "attention"
    assert (await store.history(task["id"]))[0]["status"] == "interrupted"
    assert await store.due(time.time()) == []
    assert run_id


@pytest.mark.asyncio
async def test_owner_deletion_cascades_revisions_runs_and_output(store):
    task = await active_task(store)
    run_id = await store.claim(task, 100.0)
    await store.finish(
        run_id, "delivery", "Changed", {}, [{"channel_id": "300", "content": "Update"}]
    )
    await store.delete_owner("10")
    for table in (
        "scheduled_tasks",
        "scheduled_task_revisions",
        "scheduled_task_runs",
        "scheduled_task_deliveries",
    ):
        async with store.db.conn.execute(f"SELECT count(*) FROM {table}") as cursor:
            assert (await cursor.fetchone())[0] == 0


@pytest.mark.asyncio
async def test_completion_is_scheduled_only_and_stops_further_tools():
    registry = ToolRegistry()
    manage = AsyncMock(return_value="{}")
    init_task_tools(registry, manage, manage, manage)
    args = {"outcome": "no_change", "detail": "Unchanged", "state": {"last": "123"}}
    ordinary = context(activated_tools={"task_complete"})
    assert "error" in json.loads(await registry.dispatch("task_complete", args, ordinary))
    scheduled = context(
        scheduled_run_id="run", scheduled_result={}, activated_tools={"task_complete"}
    )
    assert (
        json.loads(await registry.dispatch("task_complete", args, scheduled))["outcome"]
        == "no_change"
    )
    assert "already completed" in await registry.dispatch(
        "task_manage", {"action": "list"}, scheduled
    )
    manage.assert_not_called()


def test_management_scope_and_notification_prefix():
    task = {"guild_id": "100", "owner_id": "10"}
    assert may_manage(context(), task)
    assert not may_manage(context(user_id="11"), task)
    assert not may_manage(context(guild_id="101", trust_tier=TrustTier.STAFF), task)
    assert ScheduledTaskService._notify_content("Update", ["10"], ["20"]) == "<@10> <@&20>\nUpdate"


@pytest.mark.asyncio
async def test_everyone_role_is_rejected_even_with_permissions():
    access = object.__new__(TaskAccess)
    everyone = SimpleNamespace(is_default=lambda: True)
    channel = SimpleNamespace(guild=SimpleNamespace(get_role=lambda _: everyone))
    with pytest.raises(ValueError, match="forbidden"):
        await access.mentions(context(), channel, [], ["100"])
    mentions = await access.mentions(context(), channel, [], [])
    assert mentions.everyone is False and mentions.replied_user is False


@pytest.mark.asyncio
async def test_first_silent_baseline_loads_skill_and_discards_queued_posts(store, monkeypatch):
    task = await active_task(store, condition="Report new releases", first_check="silent")
    run_id = await store.claim(task, time.time() + 3600)
    await store.lease("worker", time.time())
    registry = ToolRegistry()
    home = SimpleNamespace(id=200, name="development")
    service = object.__new__(ScheduledTaskService)
    service._token = "worker"
    service._posts = {run_id: [{"channel_id": "300", "content": "Must not publish"}]}
    service.r = SimpleNamespace(
        store=store,
        tools=SimpleNamespace(registry=registry, workspace_locks=UserLocks()),
        conversations=ConversationStore(store.db),
        access=SimpleNamespace(channel=AsyncMock(return_value=home), owner_allowed=AsyncMock()),
        providers=SimpleNamespace(resolve=lambda *args: object(), model_config=None),
        usage=SimpleNamespace(record_turn=AsyncMock()),
        moderation=None,
        semaphore=None,
        settings=SimpleNamespace(
            bot_name="Kimi",
            react_max_iterations=20,
            react_max_tokens=4000,
            react_turn_timeout_seconds=60,
        ),
        privacy=SimpleNamespace(activity=None),
    )
    ctx = context(scheduled_run_id=run_id)
    service.fresh = AsyncMock(return_value=ctx)
    monkeypatch.setattr(scheduled_module, "load_blocked_tools", lambda *args: frozenset())
    monkeypatch.setattr(scheduled_module, "load_tool_configs", lambda *args: {})

    async def run(request):
        assert task["definition"]["skill"] in request.task_instructions
        assert "task_complete" in request.context.activated_tools
        assert request.usage_store is service.r.usage
        assert request.scheduled_run_id == run_id
        request.scheduled_result.update(
            outcome="completed",
            state={"release": "v1"},
            detail="First observation",
            content="New release",
        )
        return SimpleNamespace(
            termination_reason="completed", outbox=TurnOutbox(), generated_assets=[]
        )

    monkeypatch.setattr(scheduled_module, "run_conversation", run)
    await service._execute(task, run_id, ctx, TaskDefinition.model_validate(task["definition"]))
    assert await store.deliveries() == []
    assert (await store.history(task["id"]))[0]["status"] == "no_change"
    assert (await store.get(task["id"]))["state"] == {"release": "v1", "_task_initialized": True}


@pytest.mark.asyncio
async def test_log_send_interruption_does_not_pause_task(store):
    task = await active_task(store)
    run_id = await store.claim(task, 100.0)
    await store.finish(
        run_id,
        "no_change",
        "Unchanged",
        {"observed": True},
        [
            {"channel_id": "400", "content": "Log", "is_log": True},
        ],
    )
    await store.lease("worker", time.time())
    delivery = (await store.deliveries())[0]
    await store.begin_delivery(delivery["id"], "worker")
    await store.recover()
    assert (await store.get(task["id"]))["status"] == "active"
    assert (await store.history(task["id"]))[0]["status"] == "no_change"


@pytest.mark.asyncio
async def test_retry_known_failure_preserves_sent_chunks(store):
    task = await active_task(store)
    run_id = await store.claim(task, 100.0)
    await store.finish(
        run_id,
        "delivery",
        "Changed",
        {"cursor": "new"},
        [
            {"channel_id": "300", "content": "First"},
            {"channel_id": "300", "content": "Second"},
        ],
    )
    first, second = await store.deliveries()
    await store.delivery_status(first["id"], "sent", message_id="900")
    await store.delivery_status(second["id"], "failed", error="Forbidden")
    await store.attention(task["id"], run_id)
    await store.retry_delivery(task["id"])
    assert [row["id"] for row in await store.deliveries()] == [second["id"]]
    await store.delivery_status(second["id"], "sent", message_id="901")
    assert (await store.get(task["id"]))["state"] == {"cursor": "new"}


@pytest.mark.asyncio
@pytest.mark.parametrize("order", ["asc", "desc"])
async def test_history_traverses_more_than_100_messages_without_gaps(order):
    start = datetime(2025, 1, 1, tzinfo=UTC)
    messages = [
        SimpleNamespace(
            id=i + 1000,
            created_at=start + timedelta(seconds=i),
            content="message " + str(i),
            author=SimpleNamespace(id=10, display_name="Owner"),
            jump_url=f"https://discord.com/{i}",
            attachments=[],
        )
        for i in range(250)
    ]

    async def history(*, limit, before, after, oldest_first):
        def within(message):
            return (
                before is None
                or (
                    message.created_at < before
                    if isinstance(before, datetime)
                    else message.id < before.id
                )
            ) and (
                after is None
                or (
                    message.created_at > after
                    if isinstance(after, datetime)
                    else message.id > after.id
                )
            )

        selected = [message for message in messages if within(message)]
        if not oldest_first:
            selected.reverse()
        for message in selected[:limit]:
            yield message

    channel = SimpleNamespace(history=history)
    guild = SimpleNamespace(get_channel_or_thread=lambda _: channel)
    gateway = DiscordGateway(bot_user_provider=lambda: None)
    gateway._discord_search_actors = lambda ctx: (guild, None, None)
    gateway.resolve_discord_search_channels = AsyncMock(return_value={"200": "development"})
    args = {"channel_id": "200", "order": order, "limit": 100}
    found = []
    while True:
        page = await gateway.collect_channel_history(context(), args)
        found.extend(message["id"] for message in page["messages"])
        if page["next_cursor"] is None:
            break
        args.update(cursor=page["next_cursor"], before=page["window_end"])
    expected = [str(message.id) for message in messages]
    assert found == (expected if order == "asc" else expected[::-1])


@pytest.mark.asyncio
async def test_answer_cannot_overwrite_active_run_state(store):
    task = await active_task(store)
    run_id = await store.claim(task, 100)
    with pytest.raises(ValueError, match="Pause"):
        await store.set_status(task["id"], "active", answer="Use channel 300")
    await store.set_status(task["id"], "paused")
    await store.set_status(task["id"], "active", answer="Use channel 300")
    await store.finish(run_id, "completed", "late", {"stale": True}, [])
    assert (await store.get(task["id"]))["state"] == {"human_input": "Use channel 300"}


@pytest.mark.parametrize(
    "changes",
    [
        {"timezone": "not/a/timezone"},
        {"kind": "weekly", "weekdays": [True]},
        {"kind": "monthly", "month_day": True},
        {"kind": "interval", "interval_seconds": "60"},
    ],
)
def test_schedule_rejects_invalid_timezone_and_coerced_integers(changes):
    with pytest.raises(ValueError):
        Schedule.model_validate({"kind": "daily", "start": "2030-01-01T09:00:00Z", **changes})


@pytest.mark.parametrize("count", [1, 11])
def test_snapshot_rejects_missing_or_truncated_files(tmp_path, count):
    from app.task_output import snapshot_output
    from tools.registry import TurnOutbox

    paths = [tmp_path / f"output-{index}.txt" for index in range(count)]
    if count > 1:
        for path in paths:
            path.write_text("generated")
    outbox = TurnOutbox(output_files=[str(p) for p in paths], allowed_file_roots=[str(tmp_path)])
    with pytest.raises(ValueError, match="unavailable files"):
        snapshot_output(SimpleNamespace(), outbox, [])


@pytest.mark.asyncio
async def test_deny_is_revision_bound_and_preserves_previous_approval(store):
    task = await active_task(store)
    await store.draft(
        task_id=task["id"],
        guild_id="100",
        owner_id="10",
        channel_id="200",
        proposer_id="10",
        definition=definition(),
        expected_revision=1,
    )
    with pytest.raises(ValueError):
        await store.reject(task["id"], 2, "20")
    await store.reject(task["id"], 2, "10")
    with pytest.raises(ValueError):
        await store.activate(task["id"], 2, "10", 100, reset_state=False)
    current = await store.get(task["id"])
    assert current["status"] == "active"
    assert current["active_revision"] == 1


@pytest.mark.asyncio
async def test_approve_deny_race_has_one_winner(store):
    task_id = await store.draft(
        task_id=None,
        guild_id="100",
        owner_id="10",
        channel_id="200",
        proposer_id="10",
        definition=definition(),
    )
    results = await asyncio.gather(
        store.activate(task_id, 1, "10", 100, reset_state=False),
        store.reject(task_id, 1, "10"),
        return_exceptions=True,
    )
    assert sum(isinstance(result, ValueError) for result in results) == 1


def test_preview_uses_native_times_and_omits_verbose_details():
    from app.task_preview import render_preview

    d = TaskDefinition.model_validate(
        definition(objective="x" * 4000, condition="y" * 4000, skill="z" * 10000)
    )
    rendered = render_preview(
        {"id": "task", "revision": 1}, d, now=datetime(2029, 12, 31, tzinfo=UTC).timestamp()
    )
    assert len(rendered) < 1800
    assert "Europe/Berlin" in rendered and "your local time" in rendered
    assert rendered.count(":F>") == 3 and rendered.count(":R>") == 3
    assert "T09:" not in rendered and d.skill not in rendered and d.objective not in rendered


async def preview_fixture(store, *, can_close=True):
    from unittest.mock import MagicMock
    import discord
    from storage.task_previews import TaskPreviewStore
    from utils.privacy_barrier import UserPrivacyBarrier

    task_id = await store.draft(
        task_id=None,
        guild_id="100",
        owner_id="10",
        channel_id="200",
        proposer_id="10",
        definition=definition(),
    )
    service = object.__new__(ScheduledTaskService)
    channel = MagicMock(spec=discord.Thread)
    channel.id = 400
    channel.owner_id = 99
    member = SimpleNamespace(id=10)
    channel.guild = SimpleNamespace(
        id=100, me=SimpleNamespace(id=99), fetch_member=AsyncMock(return_value=member)
    )
    channel.permissions_for.return_value = SimpleNamespace(manage_threads=can_close)
    channel.edit = AsyncMock()
    channel.get_partial_message.return_value.edit = AsyncMock()
    service.r = SimpleNamespace(
        store=store,
        privacy=UserPrivacyBarrier(),
        access=SimpleNamespace(context=AsyncMock(return_value=context(trust_tier=TrustTier.STAFF))),
        bot=SimpleNamespace(get_channel=lambda _: channel),
        conversations=SimpleNamespace(get_thread_creator_user_id=AsyncMock(return_value="10")),
    )
    service.previews = TaskPreviewStore(store.db)
    service.fresh = AsyncMock(side_effect=lambda ctx: ctx)
    service._validate_definition = AsyncMock()
    interaction = SimpleNamespace(
        guild_id=100, channel_id=400, user=member, message=SimpleNamespace(id=500)
    )
    return service, task_id, channel, interaction


@pytest.mark.asyncio
@pytest.mark.parametrize("approve", [True, False])
@pytest.mark.parametrize("can_close", [True, False])
async def test_decision_updates_receipt_and_closes_only_with_permission(store, approve, can_close):
    service, task_id, channel, interaction = await preview_fixture(store, can_close=can_close)
    result = await service.confirm(interaction, task_id, 1, approve=approve)
    assert ("activated" if approve else "denied") in result
    edit = channel.get_partial_message.return_value.edit
    edit.assert_awaited_once()
    assert edit.call_args.kwargs["view"] is None and "attachments" not in edit.call_args.kwargs
    assert channel.edit.await_count == int(can_close)
    if can_close:
        assert channel.edit.call_args.kwargs["locked"] is True
        assert channel.edit.call_args.kwargs["archived"] is True
    assert "already" in await service.confirm(interaction, task_id, 1, approve=approve)
    assert channel.edit.await_count == int(can_close)


@pytest.mark.asyncio
@pytest.mark.parametrize("approve", [True, False])
async def test_other_staff_cannot_decide_requesters_revision(store, approve):
    service, task_id, channel, interaction = await preview_fixture(store)
    service.r.access.context.return_value = context(user_id="20", trust_tier=TrustTier.STAFF)
    interaction.user.id = 20
    with pytest.raises(ValueError, match="Only the person"):
        await service.confirm(interaction, task_id, 1, approve=approve)
    assert (await store.get(task_id))["active_revision"] is None
    channel.get_partial_message.return_value.edit.assert_not_awaited()


@pytest.mark.asyncio
async def test_activation_survives_receipt_failure_and_reconciles_after_restart(store):
    import discord
    from storage.task_previews import TaskPreviewStore

    service, task_id, channel, interaction = await preview_fixture(store)
    edit = channel.get_partial_message.return_value.edit
    edit.side_effect = discord.HTTPException(SimpleNamespace(status=503, reason="down"), "down")
    result = await service.confirm(interaction, task_id, 1)
    assert "decision succeeded" in result
    assert (await store.get(task_id))["active_revision"] == 1
    channel.edit.assert_not_awaited()
    service.previews = TaskPreviewStore(store.db)
    edit.side_effect = None
    await service.reconcile_previews(message_id="500")
    channel.edit.assert_awaited_once()
    assert await service.previews.updates(message_id="500") == []


@pytest.mark.asyncio
async def test_superseded_preview_removes_buttons_without_closing_thread(store):
    service, task_id, channel, _ = await preview_fixture(store)
    await service.previews.remember(task_id, 1, "400", "500")
    await store.draft(
        task_id=task_id,
        guild_id="100",
        owner_id="10",
        channel_id="200",
        proposer_id="10",
        definition=definition(),
        expected_revision=1,
    )
    await service.reconcile_previews()
    assert "Superseded" in channel.get_partial_message.return_value.edit.call_args.kwargs["content"]
    channel.edit.assert_not_awaited()


@pytest.mark.asyncio
async def test_draft_queues_preview_on_original_context_and_limits_llm_reply(store):
    from dataclasses import replace

    service, _, _, _ = await preview_fixture(store)
    service._moderate = AsyncMock()
    service.r.access.owner_allowed = AsyncMock()
    service.fresh = AsyncMock(side_effect=lambda ctx: replace(ctx))
    caller = context(context_key="root")
    result = json.loads(
        await service.manage({"action": "draft", "definition": definition()}, caller)
    )
    assert caller.outbox.task_preview is not None
    assert result["preview_delivery"] == "queued_separate_message"
    assert "Do not repeat" in result["instructions"] and "skill" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("location", ["new_thread", "existing_thread", "fallback", "in_channel"])
async def test_preview_delivery_uses_quiet_thread_and_separate_short_notice(store, location):
    from unittest.mock import MagicMock
    import discord
    from tools.registry import TaskPreviewRequest

    service, task_id, thread, _ = await preview_fixture(store)
    parent = MagicMock(spec=discord.TextChannel)
    parent.id = 200
    parent.guild = thread.guild
    original = thread if location == "existing_thread" else parent
    expected = original if location in {"existing_thread", "fallback", "in_channel"} else thread
    sent = SimpleNamespace(id=500, jump_url="https://discord.com/channels/100/400/500")
    expected.send = AsyncMock(return_value=sent)
    service.r.access.channel = AsyncMock(return_value=original)
    service._moderate = AsyncMock()
    boundary = SimpleNamespace(
        create_handoff_thread=AsyncMock(return_value=None if location == "fallback" else thread)
    )
    message = SimpleNamespace(guild=thread.guild, author=SimpleNamespace(id=10), channel=original)
    result = await service.deliver_preview(
        message, boundary, 1, TaskPreviewRequest(task_id, 1, location == "in_channel"), "root"
    )
    assert sent.jump_url in result and "pending approval" in result
    expected.send.assert_awaited_once()
    args = expected.send.call_args.kwargs
    assert [f.filename for f in args["files"]] == ["task.json", "SKILL.md"]
    assert [button.label for button in args["view"].children] == ["Approve", "Deny"]
    assert args["allowed_mentions"].everyone is False
    if location in {"new_thread", "fallback"}:
        request = boundary.create_handoff_thread.call_args.args[1]
        assert request.auto_respond is False
    else:
        boundary.create_handoff_thread.assert_not_awaited()
    if location == "fallback":
        assert "couldn't open" in result


@pytest.mark.asyncio
async def test_preview_references_cascade_on_task_deletion(store):
    service, task_id, _, _ = await preview_fixture(store)
    await service.previews.remember(task_id, 1, "400", "500")
    await store.delete(task_id)
    async with store.db.conn.execute("SELECT count(*) FROM scheduled_task_previews") as cursor:
        assert (await cursor.fetchone())[0] == 0


@pytest.mark.asyncio
async def test_v10_migration_preserves_approved_task_and_pending_draft(tmp_path):
    import sqlite3
    from storage.task_schema import TASK_SCHEMA

    path = tmp_path / "v10.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            'CREATE TABLE schema_version(version INTEGER PRIMARY KEY,name TEXT,applied_at TEXT); INSERT INTO schema_version VALUES(10,"scheduled_tasks","then");'
            + TASK_SCHEMA
        )
        conn.execute(
            "INSERT INTO scheduled_tasks(id,guild_id,owner_id,channel_id,revision,active_revision,status,created_at,updated_at) VALUES('t','100','10','200',2,1,'active',0,0)"
        )
        for revision in (1, 2):
            conn.execute(
                "INSERT INTO scheduled_task_revisions VALUES(?,?,?,?,?)",
                ("t", revision, json.dumps(definition()), "10", 0),
            )
    db = Database(path)
    await db.connect()
    try:
        async with db.conn.execute(
            "SELECT revision,approval_status FROM scheduled_task_revisions ORDER BY revision"
        ) as cursor:
            assert [tuple(row) for row in await cursor.fetchall()] == [
                (1, "approved"),
                (2, "pending"),
            ]
        assert (await ScheduledTaskStore(db).get("t"))["active_revision"] == 1
    finally:
        await db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("use", ["destination", "log", "management"])
@pytest.mark.parametrize("approve", [True, False])
async def test_decision_keeps_threads_needed_by_approved_tasks_open(store, use, approve):
    service, task_id, channel, interaction = await preview_fixture(store)
    config = definition(
        **(
            {"destinations": ["400"]}
            if use == "destination"
            else {"log_channel": "400"}
            if use == "log"
            else {}
        )
    )
    # A different, already approved task must also be protected from this decision.
    other_id = await store.draft(
        task_id=None,
        guild_id="100",
        owner_id="10",
        channel_id="400" if use == "management" else "200",
        proposer_id="10",
        definition=config,
    )
    await store.activate(other_id, 1, "10", 100, reset_state=False)
    await service.confirm(interaction, task_id, 1, approve=approve)
    channel.edit.assert_not_awaited()
    assert "left open" in channel.get_partial_message.return_value.edit.call_args.kwargs["content"]


@pytest.mark.asyncio
async def test_deny_edit_keeps_previous_version_destination_open(store):
    service, task_id, channel, interaction = await preview_fixture(store)
    await store.draft(
        task_id=task_id,
        guild_id="100",
        owner_id="10",
        channel_id="200",
        proposer_id="10",
        definition=definition(destinations=["400"]),
        expected_revision=1,
    )
    await store.activate(task_id, 2, "10", 100, reset_state=False)
    await store.draft(
        task_id=task_id,
        guild_id="100",
        owner_id="10",
        channel_id="200",
        proposer_id="10",
        definition=definition(),
        expected_revision=2,
    )
    await service.confirm(interaction, task_id, 3, approve=False)
    channel.edit.assert_not_awaited()
    assert (await store.get(task_id))["active_revision"] == 2


@pytest.mark.asyncio
async def test_publication_hint_reaches_model_without_private_task_context(store, monkeypatch):
    from agent.context import ConversationContext
    from agent.core import ConversationRunRequest, run_conversation
    from tests.test_core_command_template import _CapturingProvider
    import agent.core as core

    task = await active_task(
        store, skill="SECRET_PRIVATE_PROCEDURE", objective="SECRET_PRIVATE_OBJECTIVE"
    )
    run_id = await store.claim(task, 100)
    await store.finish(
        run_id,
        "delivery",
        "SECRET_RUN_DETAIL",
        {"secret": "SECRET_STATE"},
        [{"channel_id": "300", "content": "Public digest"}],
    )
    delivery = (await store.deliveries())[0]
    await store.delivery_status(delivery["id"], "sent", message_id="500")
    service = object.__new__(ScheduledTaskService)
    service.r = SimpleNamespace(store=store, conversations=ConversationStore(store.db))
    message = SimpleNamespace(
        id=500,
        channel=SimpleNamespace(id=300, name="updates"),
        content="Public digest",
        created_at=datetime(2026, 9, 9, 8, tzinfo=UTC),
    )
    await service._record_message(context(), message)
    key = "scheduled-publication:300:500"
    # A later task edit must not rename the historical run in the reply hint.
    await store.draft(
        task_id=task["id"],
        guild_id="100",
        owner_id="10",
        channel_id="200",
        proposer_id="10",
        definition=definition(name="Different task name"),
        expected_revision=1,
    )
    registry = ToolRegistry()
    registry.prompt_instructions = service.wizard_instructions
    provider = _CapturingProvider()
    monkeypatch.setattr(core, "build_system_prompt", lambda **kwargs: "System policy")
    await run_conversation(
        ConversationRunRequest(
            user_message="Why did it change?",
            context=ConversationContext(key=key),
            trust_tier=TrustTier.MEMBER,
            user_name="Reader",
            user_id="20",
            guild_id="100",
            channel_id="300",
            provider=provider,
            registry=registry,
            max_iterations=1,
        )
    )
    prompt = provider.system_prompt
    assert "Development digest" in prompt and "Different task name" not in prompt
    assert run_id in prompt and task["id"] in prompt
    assert "2026-09-09T08:00:00+00:00" in prompt
    assert "explicit request and authorization" in prompt
    for secret in (
        "SECRET_PRIVATE_PROCEDURE",
        "SECRET_PRIVATE_OBJECTIVE",
        "SECRET_RUN_DETAIL",
        "SECRET_STATE",
    ):
        assert secret not in prompt
    assert await service.wizard_instructions("20", "999", key) == ""
    assert await service.wizard_instructions("20", None, key) == ""
    assert await service.wizard_instructions("20", "100", "scheduled-publication:301:500") == ""
    await store.delete(task["id"])
    assert await service.wizard_instructions("20", "100", key) == ""


@pytest.mark.asyncio
async def test_manual_publication_has_no_scheduled_task_hint(store):
    service = object.__new__(ScheduledTaskService)
    service.r = SimpleNamespace(store=store, conversations=ConversationStore(store.db))
    message = SimpleNamespace(
        id=501,
        channel=SimpleNamespace(id=300, name="updates"),
        content="Manual post",
        created_at=datetime.now(UTC),
    )
    await service._record_message(context(), message)
    assert await service.wizard_instructions("10", "100", "scheduled-publication:300:501") == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [None, ValueError("Sources unavailable"), TimeoutError(), RuntimeError("API failed")]
)
async def test_discovery_returns_destinations_independently(failure):
    page = {"sources": {"200": "general"}, "next_cursor": "400", "has_more": True}
    discovery = AsyncMock(return_value=page, side_effect=failure)
    service = object.__new__(ScheduledTaskService)
    ctx = context()
    service.fresh = AsyncMock(return_value=ctx)
    service.r = SimpleNamespace(
        gateway=SimpleNamespace(discover_discord_channels=discovery),
        settings=SimpleNamespace(discord_search_excluded_channel_ids=frozenset()),
        access=SimpleNamespace(
            policy=AsyncMock(return_value=SimpleNamespace(destinations=["300", "400"])),
            channel=AsyncMock(side_effect=[SimpleNamespace(id=300, name="reports"), ValueError()]),
        ),
    )
    result = json.loads(await service.discover({"cursor": "200"}, ctx))
    assert result["destinations"] == {"300": "reports"}
    discovery.assert_awaited_once_with(
        ctx, excluded_channel_ids=frozenset(), cursor="200", limit=200
    )
    if failure is None:
        assert result == {**page, "guild_id": "100", "destinations": {"300": "reports"}}
    else:
        assert result["sources_error"]
        assert result["sources"] == {}
        assert result["has_more"] is None
