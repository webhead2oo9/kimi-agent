from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import discord
import pytest

from app import runtime as app_runtime
from config.fragments.tool_policy import ToolPolicyLoadError
from config.settings import Settings
from discord_adapter.io import (
    attachment_delivery_notice,
    chunk_message,
    prepare_attachment_delivery,
    suppress_link_previews,
)
from kimi_agent_module_api.contracts import GuildSettingField, GuildSettingsSchema
from modules.guild_settings import GUILD_MODULES_DIR, GuildSettingsService
from storage.db import Database
from storage.coding_tasks import CodingTask, CodingTaskStatus
from storage.model_selection import ModelSelectionStore
from tests.helpers import StubProviderManager
from tools.workspace.common import UserLocks
from trust.tiers import TrustTier
from workspace import WorkspaceKey


def _settings(**kwargs: object) -> Settings:
    values: dict[str, object] = {
        "discord_bot_token": "discord-token",
        "model_api_key": "main-key",
        "allowed_guild_ids": "",
        "moderation_enabled": False,
        **kwargs,
    }
    return Settings(_env_file=None, **values)  # type: ignore[call-arg, arg-type]


@pytest.mark.asyncio
async def test_text_response_does_not_wait_for_workspace_writer() -> None:
    locks = UserLocks()
    gateway = SimpleNamespace(send_response=AsyncMock(return_value=[]))
    app = cast(
        app_runtime.KimiApplication,
        SimpleNamespace(
            discord_gateway=gateway,
            tools=SimpleNamespace(workspace_locks=locks),
        ),
    )
    workspace_key = WorkspaceKey("u1__g1")

    async with locks.writer(workspace_key):
        await asyncio.wait_for(
            app_runtime.KimiApplication.send_response(
                app,
                cast(discord.abc.Messageable, SimpleNamespace()),
                "hello while coding",
                workspace_key=workspace_key,
            ),
            timeout=0.5,
        )

    gateway.send_response.assert_awaited_once()


@pytest.mark.asyncio
async def test_file_response_waits_for_workspace_writer() -> None:
    locks = UserLocks()
    gateway = SimpleNamespace(send_response=AsyncMock(return_value=[]))
    app = cast(
        app_runtime.KimiApplication,
        SimpleNamespace(
            discord_gateway=gateway,
            tools=SimpleNamespace(workspace_locks=locks),
        ),
    )
    workspace_key = WorkspaceKey("u1__g1")

    async with locks.writer(workspace_key):
        delivery = asyncio.create_task(
            app_runtime.KimiApplication.send_response(
                app,
                cast(discord.abc.Messageable, SimpleNamespace()),
                "attached result",
                output_files=["artifact.txt"],
                allowed_file_roots=["."],
                workspace_key=workspace_key,
            )
        )
        await asyncio.sleep(0)
        assert not delivery.done()
        gateway.send_response.assert_not_awaited()

    await asyncio.wait_for(delivery, timeout=0.5)
    gateway.send_response.assert_awaited_once()


def test_settings_secret_values_collects_nonempty_secret_fields() -> None:
    settings = _settings(compaction_api_key="compact-key", brave_api_key="")

    values = app_runtime._settings_secret_values(settings)

    assert "discord-token" in values
    assert "main-key" in values
    assert "compact-key" in values
    assert "" not in values


def test_coding_delivery_text_uses_readable_short_task_reference() -> None:
    task_id = "3ff8bac7f9e24ed19a65d267c188d7ea"
    task = SimpleNamespace(
        id=task_id,
        status=CodingTaskStatus.QUEUED,
        objective="Review the entire workspace and produce a detailed report",
        display_summary="Review the workspace",
        milestone="",
        plan=[],
    )

    status_text = app_runtime.KimiApplication._coding_status_text(cast(CodingTask, task))
    result_text = app_runtime.KimiApplication._coding_result_delivery_text(
        task_id,
        "Implemented the requested change.",
    )

    assert "coding-status:" not in status_text
    assert "coding-result:" not in result_text
    assert "Coding task `3ff8bac7`: queued" in status_text
    assert "Review the workspace" in status_text
    assert "produce a detailed report" not in status_text
    assert result_text.startswith("**Coding result `3ff8bac7`**\n")
    assert (
        app_runtime.KimiApplication._strip_coding_delivery_marker(
            result_text,
            task_ref=task_id[:8],
        )
        == "Implemented the requested change."
    )


def test_coding_status_replaces_summary_with_worker_plan() -> None:
    task = SimpleNamespace(
        id="3ff8bac7f9e24ed19a65d267c188d7ea",
        status=CodingTaskStatus.RUNNING,
        objective="Raw durable objective",
        display_summary="Queued summary",
        milestone="Repository inspected",
        plan=[{"content": "Update the parser", "status": "in_progress"}],
    )

    status = app_runtime.KimiApplication._coding_status_text(cast(CodingTask, task))

    assert "Update the parser" in status
    assert "Repository inspected" in status
    assert "Raw durable objective" not in status
    assert "Queued summary" not in status


def test_coding_status_wire_text_suppresses_link_previews() -> None:
    status = "Working on https://example.com/repo"

    assert app_runtime.KimiApplication._coding_status_wire_text(status) == (
        "Working on <https://example.com/repo>"
    )


def test_strip_coding_delivery_marker_supports_legacy_messages() -> None:
    text = "-# coding-result:3ff8bac7f9e24ed19a65d267c188d7ea\nDone."

    assert app_runtime.KimiApplication._strip_coding_delivery_marker(text) == "Done."


def test_coding_result_delivery_uses_normal_discord_chunking_without_truncation() -> None:
    task_id = "3ff8bac7f9e24ed19a65d267c188d7ea"
    report = "start\n" + ("detail line\n" * 500) + "end-of-report"

    delivery_text = app_runtime.KimiApplication._coding_result_delivery_text(task_id, report)
    chunks = app_runtime.chunk_message(delivery_text)

    assert len(chunks) > 1
    assert "[Report truncated for delivery.]" not in delivery_text
    assert "end-of-report" in chunks[-1]


@pytest.mark.asyncio
async def test_coding_result_recovery_matches_link_suppressed_wire_chunks() -> None:
    bot_user = SimpleNamespace(id=99)
    expected_text = "first https://example.com\n" + ("detail\n" * 500) + "last"
    expected = chunk_message(suppress_link_previews(expected_text))

    class HistoryChannel:
        def __init__(self, contents: list[str]) -> None:
            self.messages = [
                SimpleNamespace(content=content, author=bot_user, id=index)
                for index, content in enumerate(contents, start=1)
            ]

        async def history(self, *, limit: int):
            del limit
            for message in reversed(self.messages):
                yield message

    app = cast(
        app_runtime.KimiApplication,
        SimpleNamespace(bot=SimpleNamespace(user=bot_user)),
    )
    complete = cast(discord.TextChannel | discord.Thread, HistoryChannel(expected))
    partial = cast(discord.TextChannel | discord.Thread, HistoryChannel(expected[:-1]))

    recovered = await app_runtime.KimiApplication._find_coding_result_delivery(
        app,
        complete,
        expected_text,
        legacy_marker="coding-result:legacy",
    )
    incomplete = await app_runtime.KimiApplication._find_coding_result_delivery(
        app,
        partial,
        expected_text,
        legacy_marker="coding-result:legacy",
    )

    assert [message.content for message in recovered] == expected
    assert incomplete == []


@pytest.mark.asyncio
async def test_coding_result_channel_keeps_originating_thread() -> None:
    fallback = cast(discord.TextChannel | discord.Thread, SimpleNamespace(id=22))
    task = cast(CodingTask, SimpleNamespace(thread_id="22"))

    result = await app_runtime.KimiApplication._coding_result_channel(
        cast(app_runtime.KimiApplication, SimpleNamespace()),
        task,
        fallback,
        "result",
    )

    assert result is fallback


@pytest.mark.asyncio
async def test_coding_result_channel_adopts_foreground_handoff_thread(monkeypatch) -> None:
    class FakeTextChannel:
        async def fetch_message(self, message_id: int):
            assert message_id == 123
            return trigger

    class FakeThread:
        id = 20

    trigger = SimpleNamespace(content="build the CLI")
    fallback = FakeTextChannel()
    thread = FakeThread()
    adopt = AsyncMock(return_value=thread)
    monkeypatch.setattr(app_runtime.discord, "TextChannel", FakeTextChannel)
    monkeypatch.setattr(app_runtime.discord, "Thread", FakeThread)
    app = object.__new__(app_runtime.KimiApplication)
    app.settings = cast(
        Any,
        SimpleNamespace(
            thread_auto_handoff_enabled=False,
            thread_handoff_enabled=True,
        ),
    )
    app.thread_handoff = cast(Any, object())
    app.threads = cast(Any, SimpleNamespace(_adopt_managed_handoff_thread=adopt))
    app.coding_task_store = None
    task = cast(
        CodingTask,
        SimpleNamespace(
            id="3ff8bac7f9e24ed19a65d267c188d7ea",
            thread_id=None,
            checkpoint={},
            trigger_discord_message_id="123",
        ),
    )

    result = await app._coding_result_channel(
        task,
        cast(discord.TextChannel | discord.Thread, fallback),
        "result",
    )

    assert result is thread
    adopt.assert_awaited_once_with(trigger)


@pytest.mark.asyncio
async def test_coding_result_channel_applies_forced_auto_thread_policy(monkeypatch) -> None:
    class FakeTextChannel:
        id = 10

        async def fetch_message(self, message_id: int):
            assert message_id == 123
            return trigger

    class FakeThread:
        id = 20

    trigger = SimpleNamespace(content="build the CLI")
    fallback = FakeTextChannel()
    thread = FakeThread()
    create_thread = AsyncMock(return_value=thread)
    add_reaction = AsyncMock()
    monkeypatch.setattr(app_runtime.discord, "TextChannel", FakeTextChannel)
    monkeypatch.setattr(app_runtime.discord, "Thread", FakeThread)
    monkeypatch.setattr(
        app_runtime,
        "load_channel_auto_thread",
        lambda _channel_id: SimpleNamespace(
            min_lines=None,
            min_chars=None,
            always=True,
        ),
    )
    app = cast(
        app_runtime.KimiApplication,
        SimpleNamespace(
            settings=SimpleNamespace(
                thread_auto_handoff_enabled=True,
                thread_handoff_enabled=True,
                bot_name="Kimi",
            ),
            thread_handoff=object(),
            threads=SimpleNamespace(
                _thread_handoff_creation_allowed=lambda _message: True,
                _create_handoff_thread=create_thread,
                _adopt_managed_handoff_thread=AsyncMock(return_value=None),
            ),
            bot=SimpleNamespace(user=SimpleNamespace(id=99)),
            coding_task_store=None,
            discord_gateway=SimpleNamespace(add_status_reaction=add_reaction),
            _strip_message_invocation=lambda content, *, bot_user: content,
            _save_coding_delivery_thread=AsyncMock(),
        ),
    )
    task = cast(
        CodingTask,
        SimpleNamespace(
            id="3ff8bac7f9e24ed19a65d267c188d7ea",
            thread_id=None,
            checkpoint={},
            trigger_discord_message_id="123",
            channel_id="10",
            conversation_id=7,
            user_id="42",
        ),
    )

    result = await app_runtime.KimiApplication._coding_result_channel(
        app,
        task,
        cast(discord.TextChannel | discord.Thread, fallback),
        "short result",
    )

    assert result is thread
    create_thread.assert_awaited_once()
    add_reaction.assert_awaited_once_with(trigger, app_runtime.THREAD_HANDOFF_REACTION)


@pytest.mark.asyncio
async def test_delete_coding_status_uses_recorded_message() -> None:
    message = SimpleNamespace(delete=AsyncMock())
    channel = SimpleNamespace(fetch_message=AsyncMock(return_value=message))
    task = cast(
        CodingTask,
        SimpleNamespace(
            id="3ff8bac7f9e24ed19a65d267c188d7ea",
            status_discord_message_id="456",
        ),
    )
    app = cast(app_runtime.KimiApplication, SimpleNamespace())

    await app_runtime.KimiApplication._delete_coding_status_message(
        app,
        cast(discord.TextChannel | discord.Thread, channel),
        task,
        "Coding task `3ff8bac7`",
    )

    channel.fetch_message.assert_awaited_once_with(456)
    message.delete.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_coding_output_moderation_honors_exempt_task_tier() -> None:
    check = AsyncMock()
    service = SimpleNamespace(
        enabled=True,
        output_exempt_tier=TrustTier.REGULAR,
        check=check,
    )
    app = object.__new__(app_runtime.KimiApplication)
    app.moderation_service = cast(Any, service)
    task = cast(
        CodingTask,
        SimpleNamespace(checkpoint={"trust_tier": TrustTier.STAFF.value}),
    )

    result = await app_runtime.KimiApplication._moderate_coding_text(
        app,
        task,
        "full coding report",
        status=False,
    )

    assert result.text == "full coding report"
    assert result.blocked is False
    assert app_runtime.KimiApplication._should_moderate_coding_output(app, task) is False
    check.assert_not_awaited()


@pytest.mark.asyncio
async def test_coding_output_moderation_uses_checkpoint_task_tier() -> None:
    check = AsyncMock(return_value=SimpleNamespace(blocked=False, error=False))
    service = SimpleNamespace(
        enabled=True,
        output_exempt_tier=TrustTier.REGULAR,
        check=check,
    )
    app = object.__new__(app_runtime.KimiApplication)
    app.moderation_service = cast(Any, service)
    task = cast(
        CodingTask,
        SimpleNamespace(
            checkpoint={"trust_tier": TrustTier.MEMBER.value},
            user_id="42",
            channel_id="10",
            thread_id=None,
        ),
    )

    result = await app_runtime.KimiApplication._moderate_coding_text(
        app,
        task,
        "full coding report",
        status=False,
    )

    assert result.text == "full coding report"
    assert result.blocked is False
    check.assert_awaited_once_with(
        text="full coding report",
        direction=app_runtime.Direction.OUTPUT,
        user_id="42",
        channel_id="10",
        thread_id=None,
        trust_tier=TrustTier.MEMBER.value,
    )


@pytest.mark.asyncio
async def test_coding_output_moderation_marks_blocked_result() -> None:
    check = AsyncMock(return_value=SimpleNamespace(blocked=True, error=False))
    service = SimpleNamespace(
        enabled=True,
        output_exempt_tier=None,
        check=check,
        refusal_for=lambda _direction, *, error: f"refused:{error}",
    )
    app = object.__new__(app_runtime.KimiApplication)
    app.moderation_service = cast(Any, service)
    task = cast(
        CodingTask,
        SimpleNamespace(
            checkpoint={"trust_tier": TrustTier.MEMBER.value},
            user_id="42",
            channel_id="10",
            thread_id=None,
        ),
    )

    result = await app._moderate_coding_text(task, "blocked report", status=False)

    assert result.text == "refused:False"
    assert result.blocked is True


@pytest.mark.asyncio
async def test_durable_attachment_plan_freezes_limit_and_plain_notice(tmp_path: Path) -> None:
    output = tmp_path / "large.zip"
    output.write_bytes(b"12345")
    guild = SimpleNamespace(filesize_limit=4)
    channel = cast(
        discord.TextChannel | discord.Thread,
        SimpleNamespace(guild=guild),
    )
    save_plan = AsyncMock(side_effect=lambda _task_id, plan: plan)
    gateway = SimpleNamespace(
        prepare_attachment_delivery=lambda target, **kwargs: prepare_attachment_delivery(
            target,
            **kwargs,
        )
    )
    app = object.__new__(app_runtime.KimiApplication)
    app.discord_gateway = cast(Any, gateway)
    app.coding_task_store = cast(
        Any,
        SimpleNamespace(
            set_delivery_attachment_plan_if_absent=save_plan,
        ),
    )
    task = cast(CodingTask, SimpleNamespace(id="task-1", checkpoint={}))

    plan = await app_runtime.KimiApplication._prepare_coding_attachment_delivery(
        app,
        task,
        channel,
        output_files=[str(output)],
        allowed_roots=[str(tmp_path)],
    )

    assert plan.files == ()
    assert [item.filename for item in plan.omitted] == ["large.zip"]
    save_plan_call = save_plan.await_args
    assert save_plan_call is not None
    frozen = save_plan_call.args[1]
    notice = frozen["notice_text"]
    assert notice == attachment_delivery_notice(plan)
    assert "**" not in notice
    assert "`" not in notice

    guild.filesize_limit = 100
    recovered_task = cast(
        CodingTask,
        SimpleNamespace(id="task-1", checkpoint={"delivery": {"attachment_plan": frozen}}),
    )
    recovered = await app_runtime.KimiApplication._prepare_coding_attachment_delivery(
        app,
        recovered_task,
        channel,
        output_files=[str(output)],
        allowed_roots=[str(tmp_path)],
    )

    assert recovered.effective_limit_bytes == 4
    assert recovered.files == ()
    assert attachment_delivery_notice(recovered) == notice
    save_plan.assert_awaited_once()


@pytest.mark.asyncio
async def test_durable_delivery_notice_is_persisted_in_assistant_transcript() -> None:
    save_messages = AsyncMock()
    app = object.__new__(app_runtime.KimiApplication)
    app.conversation_store = cast(
        Any,
        SimpleNamespace(save_channel_messages=save_messages),
    )
    task = cast(
        CodingTask,
        SimpleNamespace(
            id="3ff8bac7f9e24ed19a65d267c188d7ea",
            conversation_id=7,
        ),
    )
    notice = "Delivery notice: Discord did not attach large.zip because it exceeds the limit."
    message = cast(
        discord.Message,
        SimpleNamespace(
            id=123,
            content=f"**Coding result `3ff8bac7`**\n{notice}\n\nReport body.",
            created_at=None,
        ),
    )

    await app_runtime.KimiApplication._persist_coding_final_messages(
        app,
        task,
        [message],
        channel_id="10",
    )

    save_messages_call = save_messages.await_args
    assert save_messages_call is not None
    records = save_messages_call.args[1]
    assert records[0].content == f"{notice}\n\nReport body."
    assert save_messages_call.kwargs == {"context_channel_id": "10"}


def test_build_app_wires_shared_registry(monkeypatch) -> None:
    monkeypatch.setattr(
        app_runtime,
        "build_provider_manager",
        lambda settings: StubProviderManager(settings),
    )

    app = app_runtime.build_app(_settings())

    assert app.bot is not None
    assert app.registry is app.tools.registry
    assert app.memory_manager.registry is app.registry
    assert app.moderation_service is None


def test_members_intent_is_off_unless_requested(monkeypatch) -> None:
    monkeypatch.setattr(
        app_runtime,
        "build_provider_manager",
        lambda settings: StubProviderManager(settings),
    )

    assert app_runtime.build_app(_settings()).bot.intents.members is False
    enabled = app_runtime.build_app(_settings(members_intent=True))
    assert enabled.bot.intents.members is True


@pytest.mark.asyncio
async def test_first_init_core_has_no_optional_module_tables(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        app_runtime,
        "build_provider_manager",
        lambda settings: StubProviderManager(settings),
    )
    app = app_runtime.build_app(_settings(database_path=str(tmp_path / "bot.db")))
    await app._first_init_core()

    cursor = await app.database.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    tables = {str(row[0]) for row in await cursor.fetchall()}
    assert "reference_kudos_kudos" not in tables
    assert app.tools.module_manager.load_state.loaded == ()
    await app.database.close()


def test_build_app_rejects_invalid_global_tool_policy(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / "tools.md").write_text(
        "---\nblocked_tools: not-a-list\n---\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        app_runtime,
        "build_provider_manager",
        lambda settings: StubProviderManager(settings),
    )

    with pytest.raises(ToolPolicyLoadError, match="Could not load global tool policy"):
        app_runtime.build_app(_settings(config_dir=str(tmp_path)))


def test_build_app_wires_moderation_service_when_enabled(monkeypatch) -> None:
    monkeypatch.setattr(
        app_runtime,
        "build_provider_manager",
        lambda settings: StubProviderManager(settings),
    )

    app = app_runtime.build_app(
        _settings(
            moderation_enabled=True,
            moderation_api_key="moderation-key",
            moderation_output_exempt_tier="regular",
        )
    )

    assert app.moderation_service is not None
    assert app.moderation_service.enabled is True
    assert app.moderation_service.output_exempt_tier is TrustTier.REGULAR


def test_build_app_binds_discord_events(monkeypatch) -> None:
    monkeypatch.setattr(
        app_runtime,
        "build_provider_manager",
        lambda settings: StubProviderManager(settings),
    )
    bound: list[str] = []

    class RecordingBot(app_runtime.KimiBot):
        def event(self, coro: Any) -> Any:
            bound.append(coro.__name__)
            return coro

    monkeypatch.setattr(app_runtime, "KimiBot", RecordingBot)

    app_runtime.build_app(_settings())

    assert bound == [
        "on_ready",
        "on_disconnect",
        "on_resumed",
        "on_message",
        "on_guild_join",
    ]


def test_app_command_tree_rejects_unapproved_guild_before_dispatch(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        app_runtime,
        "build_provider_manager",
        lambda settings: StubProviderManager(settings),
    )
    app = app_runtime.build_app(_settings(allowed_guild_ids="111", config_dir=str(tmp_path)))
    app.gateway_ready = True
    response = SimpleNamespace(is_done=lambda: False, send_message=AsyncMock())
    interaction: Any = SimpleNamespace(
        id=42,
        guild_id=999,
        type=discord.InteractionType.application_command,
        response=response,
        followup=SimpleNamespace(send=AsyncMock()),
    )

    allowed = asyncio.run(app.bot.tree.interaction_check(interaction))

    assert allowed is False
    response.send_message.assert_awaited_once_with(
        "This bot is not available in this server.",
        ephemeral=True,
    )


def test_saved_server_setup_activates_guild_without_restart(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        app_runtime,
        "build_provider_manager",
        lambda settings: StubProviderManager(settings),
    )
    app = app_runtime.build_app(_settings(config_dir=str(tmp_path)))
    app.gateway_ready = True
    assert app.active_guilds() == set()

    servers = tmp_path / "servers"
    servers.mkdir()
    (servers / "999.md").write_text("---\nbot_active: true\n---\n", encoding="utf-8")
    asyncio.run(app.refresh_guild_activation(999))
    response = SimpleNamespace(is_done=lambda: False, send_message=AsyncMock())
    interaction: Any = SimpleNamespace(
        id=43,
        guild_id=999,
        type=discord.InteractionType.application_command,
        response=response,
        followup=SimpleNamespace(send=AsyncMock()),
    )

    allowed = asyncio.run(app.bot.tree.interaction_check(interaction))

    assert allowed is True
    assert app.active_guilds() == {999}
    response.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_module_guild_settings_refresh_notifies_on_event_loop(tmp_path: Path) -> None:
    guild_id = 999
    document = tmp_path / GUILD_MODULES_DIR / str(guild_id) / "mod.md"
    document.parent.mkdir(parents=True)
    document.write_text("---\ncount: nope\n---\n", encoding="utf-8")
    loop_thread = threading.get_ident()
    read_threads: list[int] = []
    callback_threads: list[int] = []
    health_threads: list[int] = []

    def config_dir() -> Path:
        read_threads.append(threading.get_ident())
        return tmp_path

    service = GuildSettingsService(
        config_dir=config_dir,
        schemas={
            "mod": GuildSettingsSchema(
                fields=(GuildSettingField("count", "int", required=True),),
                invalid_policy="disable_module",
            )
        },
        on_health=lambda _module, _state, _detail: health_threads.append(threading.get_ident()),
    )
    service.subscribe("mod", lambda _guild_id: callback_threads.append(threading.get_ident()))
    app = cast(
        app_runtime.KimiApplication,
        SimpleNamespace(
            tools=SimpleNamespace(module_manager=SimpleNamespace(guild_settings=service))
        ),
    )

    await app_runtime.KimiApplication._refresh_module_guild_settings(app, guild_id)

    assert read_threads and all(thread_id != loop_thread for thread_id in read_threads)
    assert callback_threads == [loop_thread]
    assert health_threads == [loop_thread]


def test_saved_deactivation_overrides_environment_allowlist(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        app_runtime,
        "build_provider_manager",
        lambda settings: StubProviderManager(settings),
    )
    servers = tmp_path / "servers"
    servers.mkdir()
    (servers / "999.md").write_text("---\nbot_active: false\n---\n", encoding="utf-8")

    app = app_runtime.build_app(_settings(allowed_guild_ids="999", config_dir=str(tmp_path)))

    assert app.active_guilds() == set()
    assert app.guild_activation_state(999) == {
        "active": False,
        "activation": "deactivated",
        "setup_state": "deactivated",
        "environment_approved": True,
    }


def test_unconfigured_guild_join_stays_connected(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        app_runtime,
        "build_provider_manager",
        lambda settings: StubProviderManager(settings),
    )
    app = app_runtime.build_app(_settings(config_dir=str(tmp_path)))
    guild = SimpleNamespace(id=999, name="Pending", leave=AsyncMock())

    asyncio.run(app.on_guild_join(cast(discord.Guild, guild)))

    guild.leave.assert_not_awaited()


@pytest.mark.asyncio
async def test_first_init_restores_global_model_selection(
    monkeypatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "bot.db"
    seed_db = Database(path)
    await seed_db.connect()
    await ModelSelectionStore(seed_db).set("stub-model")
    await seed_db.close()
    monkeypatch.setattr(
        app_runtime,
        "build_provider_manager",
        lambda settings: StubProviderManager(settings),
    )
    app = app_runtime.build_app(_settings(database_path=str(path), owner_user_id="42"))

    await app._first_init_core()

    assert app.provider_manager.active_chat_model == "stub-model"
    assert app.bot.tree.get_command("models") is not None
    await app.database.close()


def test_build_app_reads_the_operator_overlay_from_the_configured_instance_dir(
    tmp_path, monkeypatch
):
    """build_app applied settings.md before set_default_config_dir ran, so the
    overlay was read from the checkout and a production CONFIG_DIR's file was
    silently ignored. Pin the explicit config_dir handoff."""
    from pathlib import Path as _Path

    import app.runtime as app_runtime
    from config.settings import Settings
    from tests.helpers import StubProviderManager

    recorded: dict = {}

    def record_overlay(settings, *, config_dir=None):
        recorded["config_dir"] = config_dir
        return []

    monkeypatch.setattr(app_runtime, "apply_operator_settings", record_overlay)
    monkeypatch.setattr(
        app_runtime, "build_provider_manager", lambda settings: StubProviderManager(settings)
    )
    config_dir = tmp_path / "instance-config"
    config_dir.mkdir()
    app_runtime.build_app(
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            model_api_key="key",
            config_dir=str(config_dir),
        )
    )

    assert recorded["config_dir"] == _Path(str(config_dir)).resolve()
