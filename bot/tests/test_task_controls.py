from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import discord
import pytest
import pytest_asyncio

import app.task_executor as scheduled_module
import app.task_authority as authority_module
from app.scheduled_tasks import ScheduledTaskService
from app.task_controls import TaskConfirmation
from app.task_controls import TaskManageEntry
from storage.conversations import ConversationStore
from storage.db import Database
from storage.scheduled_tasks import ScheduledTaskStore
from tests.test_scheduled_tasks import active_task, context, definition
from tools.browse import init_browse_tools
from tools.registry import MessageContext, ToolRegistry, TurnOutbox
from tools.workspace.common import UserLocks
from trust.tiers import TrustTier
from utils.privacy_barrier import UserPrivacyBarrier


@pytest_asyncio.fixture
async def harness(tmp_path, monkeypatch):
    db = Database(tmp_path / "tasks.db")
    await db.connect()
    registry = ToolRegistry()
    init_browse_tools(registry)
    home = MagicMock(spec=discord.TextChannel)
    home.id, home.name, home.parent_id = 200, "development", None
    home.guild = SimpleNamespace(id=100, filesize_limit=25 * 1024 * 1024)
    home.send = AsyncMock(
        return_value=SimpleNamespace(
            id=500,
            created_at=datetime(2030, 1, 1, tzinfo=UTC),
            jump_url="https://discord.com/channels/100/200/500",
        )
    )
    home.get_partial_message.return_value.edit = AsyncMock()
    destination = SimpleNamespace(id=300, guild=home.guild, send=AsyncMock())

    async def access_context(guild_id, owner_id, channel_id, *, run_id=""):
        return context(
            guild_id=guild_id, user_id=owner_id, channel_id=channel_id, scheduled_run_id=run_id
        )

    runtime = SimpleNamespace(
        store=ScheduledTaskStore(db),
        bot=SimpleNamespace(tree=Mock(), add_view=Mock(), get_channel=lambda _: home),
        tools=SimpleNamespace(
            registry=registry, workspace_locks=UserLocks(), plugin_privacy_callbacks=Mock()
        ),
        conversations=ConversationStore(db),
        access=SimpleNamespace(
            context=AsyncMock(side_effect=access_context),
            owner_allowed=AsyncMock(return_value=SimpleNamespace(timezone="UTC")),
            channel=AsyncMock(
                side_effect=lambda ctx, channel_id, **kwargs: (
                    home if channel_id == "200" else destination
                )
            ),
            mentions=AsyncMock(return_value=discord.AllowedMentions.none()),
        ),
        providers=SimpleNamespace(
            resolve=Mock(return_value=object()),
            model_config=SimpleNamespace(roles=SimpleNamespace(scheduled="task-model")),
        ),
        usage=SimpleNamespace(record_turn=AsyncMock()),
        moderation=None,
        semaphore=asyncio.Semaphore(2),
        settings=SimpleNamespace(
            bot_name="Kimi",
            react_max_iterations=20,
            react_max_tokens=4000,
            react_turn_timeout_seconds=60,
        ),
        privacy=UserPrivacyBarrier(),
        user_blocked=AsyncMock(return_value=False),
        gateway=Mock(),
    )
    service = ScheduledTaskService(runtime)
    monkeypatch.setattr(scheduled_module, "load_blocked_tools", lambda *args: frozenset())
    monkeypatch.setattr(authority_module, "load_blocked_tools", lambda *args: frozenset())
    monkeypatch.setattr(scheduled_module, "load_tool_configs", lambda *args: {})
    try:
        yield service, home, destination
    finally:
        await service.close()
        await db.close()


async def draft(service, *, owner_id="10", guild_id="100", **changes):
    return await service.r.store.draft(
        task_id=None,
        guild_id=guild_id,
        owner_id=owner_id,
        channel_id="200",
        proposer_id=owner_id,
        definition=definition(**changes),
    )


def interaction(*, user_id=10, guild_id=100):
    sent = []

    async def send(**kwargs):
        saved = dict(kwargs)
        if file := kwargs.get("file"):
            saved["file_text"] = file.fp.read().decode()
        sent.append(saved)

    return SimpleNamespace(
        guild_id=guild_id,
        channel_id=200,
        user=SimpleNamespace(id=user_id),
        message=None,
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock(side_effect=send)),
        sent=sent,
    )


def tool_context(request):
    return MessageContext(
        user_id=request.user_id,
        user_name=request.user_name,
        guild_id=request.guild_id,
        channel_id=request.channel_id,
        thread_id=None,
        trust_tier=request.trust_tier,
        scheduled_run_id=request.scheduled_run_id,
        scheduled_result=request.scheduled_result,
        before_tool=request.before_tool,
        blocked_tools=request.context.blocked_tools,
        activated_tools={entry.name for entry in request.registry.get_all_tools()},
    )


def model_result():
    return SimpleNamespace(termination_reason="completed", outbox=TurnOutbox(), generated_assets=[])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "condition,first_check,expected",
    [
        ("", "silent", "completed"),
        ("Watch releases", "silent", "no_change"),
        ("Watch releases", "publish", "completed"),
    ],
)
async def test_preview_reads_and_captures_posts_without_publishing_or_saving_state(
    harness, monkeypatch, condition, first_check, expected
):
    service, home, destination = harness
    task_id = await draft(service, condition=condition, first_check=first_check)
    before = await service.r.store.get(task_id)
    registry = service.r.tools.registry
    read = AsyncMock(return_value='{"messages":[{"content":"Release v2"}]}')
    write = AsyncMock(return_value="changed")
    for name, handler in (("get_channel_context", read), ("browser", write)):
        registry.register(name=name, description=name, parameters={}, handler=handler)

    async def run(request):
        assert "TEST PREVIEW" in request.task_instructions
        ctx = tool_context(request)
        assert "Unknown tool" in await registry.dispatch("browser", {}, ctx)
        # Loading a new plugin during the test cannot bypass its restricted tools.
        registry.register(name="new_write_tool", description="write", parameters={}, handler=write)
        assert "Unknown tool" in await registry.dispatch("new_write_tool", {}, ctx)
        assert "Release v2" in await registry.dispatch("get_channel_context", {}, ctx)
        assert "queued" in await registry.dispatch(
            "discord_post", {"channel_id": "300", "content": "Release v2"}, ctx
        )
        await registry.dispatch(
            "task_complete",
            {
                "outcome": "completed",
                "state": {"release": "v2"},
                "detail": "A new release was found",
                "content": "",
            },
            ctx,
        )
        return model_result()

    monkeypatch.setattr(scheduled_module, "run_conversation", run)
    result = await service.executor.test_preview(context(), task_id, 1)
    assert result["outcome"] == expected
    assert len(result["posts"]) == int(expected == "completed")
    if expected == "no_change":
        assert "baseline" in result["detail"]
    assert await service.r.store.get(task_id) == before
    assert await service.r.store.history(task_id) == []
    assert await service.r.store.deliveries() == []
    read.assert_awaited_once()
    write.assert_not_awaited()
    home.send.assert_not_awaited()
    destination.send.assert_not_awaited()
    assert service.executor.tests == service.runs == {}
    service.r.usage.record_turn.assert_awaited()
    assert service.r.providers.resolve.call_args_list[0].args[0] == "scheduled"
    async with service.r.store.db.conn.execute(
        "SELECT access_scope,owner_user_id FROM conversations"
    ) as cursor:
        assert [tuple(row) for row in await cursor.fetchall()] == [("owner_only", "10")]


@pytest.mark.asyncio
async def test_preview_returns_needs_input_privately_and_leaves_approval_pending(
    harness, monkeypatch
):
    service, home, destination = harness
    task_id = await draft(service)

    async def run(request):
        request.scheduled_result.update(
            outcome="needs_input",
            state={},
            content="",
            detail="This procedure requires a browser action unavailable in preview.",
        )
        return model_result()

    monkeypatch.setattr(scheduled_module, "run_conversation", run)
    event = interaction()
    card = TaskConfirmation(service.controls, task_id, 1)
    assert card.is_persistent()
    await card.children[0].callback(event)
    assert event.sent[0]["ephemeral"] is True
    assert "Needs input" in event.sent[0]["embed"].description
    assert "browser action" in event.sent[0]["file_text"]
    assert event.sent[0]["allowed_mentions"].everyone is False
    assert (await service.r.store.get(task_id))["approval_status"] == "pending"
    home.send.assert_not_awaited()
    destination.send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["approve", "replace", "blocked"])
async def test_preview_stops_if_draft_or_authority_changes(harness, monkeypatch, change):
    service, _, destination = harness
    task_id = await draft(service)

    async def run(request):
        if change == "approve":
            await service.r.store.activate(task_id, 1, "10", 100, reset_state=False)
        elif change == "replace":
            await service.r.store.draft(
                task_id=task_id,
                guild_id="100",
                owner_id="10",
                channel_id="200",
                proposer_id="10",
                definition=definition(),
                expected_revision=1,
            )
        else:
            service.r.user_blocked.return_value = True
        await request.before_tool(tool_context(request))
        pytest.fail("stale preview reached a tool")

    monkeypatch.setattr(scheduled_module, "run_conversation", run)
    with pytest.raises(ValueError):
        await service.executor.test_preview(context(), task_id, 1)
    assert await service.r.store.history(task_id) == []
    assert service.executor.tests == service.runs == {}
    destination.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_only_requester_can_test_pending_revision(harness):
    service, _, _ = harness
    task_id = await draft(service)
    # Staff may manage the task, but cannot test someone else's proposal.
    service.authority.fresh = AsyncMock(side_effect=lambda ctx: ctx)
    with pytest.raises(ValueError, match="Only the person"):
        await service.executor.test_preview(
            context(user_id="11", trust_tier=TrustTier.STAFF), task_id, 1
        )
    await service.r.store.reject(task_id, 1, "10")
    with pytest.raises(ValueError, match="decided or replaced"):
        await service.executor.test_preview(context(), task_id, 1)


@pytest.mark.asyncio
async def test_duplicate_preview_is_rejected_and_cancellation_cleans_up(harness, monkeypatch):
    service, _, _ = harness
    task_id = await draft(service)
    started = asyncio.Event()

    async def run(request):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(scheduled_module, "run_conversation", run)
    test = asyncio.create_task(service.executor.test_preview(context(), task_id, 1))
    await asyncio.wait_for(started.wait(), 2)
    with pytest.raises(ValueError, match="already running"):
        await service.executor.test_preview(context(), task_id, 1)
    await service._cancel(task_id)
    assert test.cancelled()
    assert service.executor.tests == service.runs == {}
    assert await service.r.store.history(task_id) == []


@pytest.mark.asyncio
async def test_tasks_panel_pages_and_rechecks_permissions(harness):
    service, _, _ = harness
    ids = [await draft(service, name=f"Task {i}") for i in range(12)]
    await draft(service, owner_id="11", name="Someone else's task")
    await draft(service, guild_id="101", name="Another server")
    event = interaction()
    controls = service.controls
    await controls.handle(event, "list")
    panel = event.sent[-1]
    assert panel["ephemeral"] is True
    select = panel["view"].children[0]
    assert [option.value for option in select.options] == list(reversed(ids))[:10]
    assert "Someone else's" not in panel["embed"].description
    next_button = next(
        item for item in panel["view"].children if getattr(item, "label", "") == "Next"
    )
    second = interaction()
    await next_button.callback(second)
    assert [option.value for option in second.sent[-1]["view"].children[0].options] == [
        ids[1],
        ids[0],
    ]
    forbidden = interaction(user_id=11)
    await TaskManageEntry(service.controls, ids[0]).children[0].callback(forbidden)
    assert forbidden.sent[-1]["content"] == "Task not found"
    service.r.user_blocked.return_value = True
    revoked = interaction()
    await controls.handle(revoked, "inspect", task_id=ids[0])
    assert revoked.sent[-1]["content"] == "Task access is unavailable"


@pytest.mark.asyncio
async def test_manage_buttons_pause_resume_and_confirm_deletion(harness):
    service, _, _ = harness
    task = await active_task(service.r.store)
    controls = service.controls
    event = interaction()
    await controls.handle(event, "inspect", task_id=task["id"])
    pause = next(item for item in event.sent[-1]["view"].children if item.label == "Pause")
    paused = interaction()
    await pause.callback(paused)
    assert (await service.r.store.get(task["id"]))["status"] == "paused"
    resume = next(item for item in paused.sent[-1]["view"].children if item.label == "Resume")
    await resume.callback(interaction())
    assert (await service.r.store.get(task["id"]))["status"] == "active"
    confirm = interaction()
    await controls.handle(confirm, "delete", task_id=task["id"])
    assert await service.r.store.get(task["id"])
    await confirm.sent[-1]["view"].children[0].callback(interaction())
    with pytest.raises(ValueError, match="Task not found"):
        await service.r.store.get(task["id"])


@pytest.mark.asyncio
async def test_active_cards_refresh_status_and_survive_restart(harness, monkeypatch):
    service, home, _ = harness
    task = await active_task(service.r.store)
    await service.approvals.previews.remember(task["id"], 1, "200", "500")
    await service.approvals.reconcile()
    edit = home.get_partial_message.return_value.edit
    assert "Task active" in edit.call_args.kwargs["content"]
    assert edit.call_args.kwargs["view"].is_persistent()
    assert await service.approvals.previews.updates() == []
    await service.r.store.set_status(task["id"], "paused")
    await service.approvals.reconcile()
    assert "Task paused" in edit.call_args.kwargs["content"]
    assert "None scheduled" in edit.call_args.kwargs["content"]
    monkeypatch.setattr(service.scheduler, "loop", AsyncMock())
    await service.start()
    ids = {
        item.custom_id
        for call in service.r.bot.add_view.call_args_list
        for item in call.args[0].children
    }
    assert f"task-manage:{task['id']}" in ids
    assert f"task-test:{task['id']}:1" in ids


@pytest.mark.asyncio
async def test_existing_pending_card_gains_preview_button_once(harness):
    service, home, _ = harness
    task_id = await draft(service)
    await service.approvals.previews.remember(task_id, 1, "200", "500")
    await service.approvals.reconcile()
    edit = home.get_partial_message.return_value.edit
    assert [item.label for item in edit.call_args.kwargs["view"].children] == [
        "Test preview",
        "Approve",
        "Reject",
    ]
    assert await service.approvals.previews.updates() == []


@pytest.mark.asyncio
async def test_private_panel_navigation_updates_original_response(harness):
    service, _, _ = harness
    await draft(service)
    event = interaction()
    event.message = SimpleNamespace(flags=SimpleNamespace(ephemeral=True))
    event.edit_original_response = AsyncMock()

    async def defer(**kwargs):
        assert kwargs == {"ephemeral": True, "thinking": False}
        event.response.type = discord.InteractionResponseType.deferred_message_update

    event.response.defer.side_effect = defer
    await service.controls.handle(event, "list")
    event.followup.send.assert_not_awaited()
    payload = event.edit_original_response.call_args.kwargs
    assert payload["attachments"] == []
    assert payload["embed"].title == "Scheduled tasks"
    components = payload["view"].to_components()
    assert components[0]["components"][0]["type"] == 3  # Discord select menu.
    assert payload["allowed_mentions"].everyone is False


@pytest.mark.asyncio
async def test_edit_starts_owner_scoped_conversation_for_selected_task(harness):
    service, home, _ = harness
    task = await active_task(service.r.store)
    event = interaction()
    await service.controls.handle(event, "edit", task_id=task["id"])
    home.send.assert_awaited_once()
    assert "Reply to this message" in home.send.call_args.args[0]
    conv = await service.r.conversations.get_continuation_conversation_for_reply(
        "500",
        channel_id="200",
        requester_user_id="10",
    )
    assert conv is not None and conv.access_scope == "owner_only"
    assert task["id"] in await service.manager.wizard_instructions("10", "100", conv.key)
    assert await service.manager.wizard_instructions("11", "100", conv.key) == ""
    setup = json.loads(await service.manage({"action": "setup"}, context(context_key=conv.key)))
    assert setup["task_id"] == task["id"]
    assert task["id"] in await service.manager.wizard_instructions("10", "100", conv.key)
    await service.r.store.draft(
        task_id=task["id"],
        guild_id="100",
        owner_id="10",
        channel_id="200",
        proposer_id="10",
        definition=definition(),
        expected_revision=1,
    )
    await service.r.store.activate(task["id"], 2, "10", 100, reset_state=False)
    assert await service.manager.wizard_instructions("10", "100", conv.key) == ""


@pytest.mark.asyncio
async def test_cancelled_delivery_is_not_offered_for_retry(harness):
    service, _, _ = harness
    store = service.r.store
    task = await active_task(store)
    run_id = await store.claim(task, 100)
    await store.finish(
        run_id,
        "delivery",
        "",
        {"cursor": "new"},
        [
            {"channel_id": "300", "content": "First"},
            {"channel_id": "300", "content": "Second"},
        ],
    )
    first, _ = await store.deliveries()
    await store.delivery_status(first["id"], "failed", error="Forbidden")
    await store.attention(task["id"], run_id)
    assert await store.can_retry_delivery(task["id"])
    await store.set_status(task["id"], "paused")
    assert not await store.can_retry_delivery(task["id"])
    with pytest.raises(ValueError, match="cancelled"):
        await store.retry_delivery(task["id"])


@pytest.mark.asyncio
@pytest.mark.parametrize("reset", [False, True])
async def test_preview_copies_saved_state_and_honors_draft_reset(harness, monkeypatch, reset):
    service, _, _ = harness
    store = service.r.store
    task = await active_task(store)
    run_id = await store.claim(task, 100)
    await store.finish(run_id, "no_change", "", {"cursor": "saved"}, [])
    await store.draft(
        task_id=task["id"],
        guild_id="100",
        owner_id="10",
        channel_id="200",
        proposer_id="10",
        definition=definition(reset_state=reset),
        expected_revision=1,
    )
    before = await store.get(task["id"])
    history = await store.history(task["id"])

    async def run(request):
        assert ('"cursor": "saved"' in request.user_message) == (not reset)
        request.scheduled_result.update(
            outcome="no_change", state={"cursor": "test"}, content="", detail="No change"
        )
        return model_result()

    monkeypatch.setattr(scheduled_module, "run_conversation", run)
    await service.executor.test_preview(context(), task["id"], 2)
    assert await store.get(task["id"]) == before
    assert await store.history(task["id"]) == history
