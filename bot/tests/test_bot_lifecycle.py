from __future__ import annotations

from workspace import WorkspaceKey

import asyncio
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from agent.context import ContextManager
from app.memory import MemoryManager
from app import providers as provider_runtime
from app import runtime as app_runtime
from utils.privacy_barrier import PrivacyDeletionPendingError
from config.model_config import ModelConfig
from config.settings import Settings
from discord_adapter.module_events import ModuleEventPublisher
from providers.base import LLMProvider
from storage.auto_retain import AutoRetainStore
from storage.conversations import ConversationStore, UserDataDeletion
from storage.db import Database
from storage.preferences import PreferenceStore
from storage.privacy import PrivacyDeletionRequestStore
from tests.helpers import StubProviderManager
from tools.workspace.common import UserLocks


def _settings(**kwargs: object) -> Settings:
    values = {
        "model_api_key": "main-key",
        "tool_event_log_enabled": False,
        "memory_auto_retain_enabled": False,
        "transcript_retention_days": 0,
        **kwargs,
    }
    return Settings.model_validate(values)


def _single_model_config(
    *,
    provider_type: str = "openai_compat",
    base_url: str = "https://llm-gateway.example.invalid/v1",
    api_key_env: str = "MODEL_API_KEY",
) -> ModelConfig:
    return ModelConfig.model_validate(
        {
            "providers": {
                "main": {
                    "type": provider_type,
                    "base_url": base_url,
                    "api_key_env": api_key_env,
                }
            },
            "models": {"chat": {"provider": "main", "model": "model"}},
            "roles": {"chat": "chat", "compaction": "chat"},
        }
    )


class CloseableProvider:
    def __init__(self, provider_key: str = "fake") -> None:
        self.provider_key = provider_key
        self.close_count = 0

    async def close(self) -> None:
        self.close_count += 1


class FakeMemoryManager:
    def __init__(self, client: object | None = object()) -> None:
        self.client = client
        self.ready = False
        self.ensure_calls: list[tuple[object, object]] = []
        self.close_count = 0

    def active_client(self) -> object | None:
        return self.client if self.ready else None

    async def ensure_ready(
        self,
        conversation_store: object,
        preference_store: object,
    ) -> None:
        self.ensure_calls.append((conversation_store, preference_store))

    async def close(self) -> None:
        self.close_count += 1


def _build_test_app(monkeypatch: pytest.MonkeyPatch) -> app_runtime.KimiApplication:
    monkeypatch.setattr(
        app_runtime,
        "build_provider_manager",
        lambda settings: StubProviderManager(settings),
    )
    return app_runtime.build_app(_settings(discord_bot_token="discord-token"))


@pytest.mark.asyncio
async def test_component_interaction_readiness_closes_before_resource_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _build_test_app(monkeypatch)
    close_started = asyncio.Event()
    release_close = asyncio.Event()

    async def slow_close_resources() -> None:
        close_started.set()
        await release_close.wait()

    monkeypatch.setattr(app, "_close_resources", slow_close_resources)
    app.gateway_ready = True
    assert app.gateway_interactions_ready() is True

    closing = asyncio.create_task(app.close())
    await close_started.wait()

    # Resource cleanup has not proceeded, but new component callbacks must
    # already be rejected at the same boundary as slash commands.
    assert app.gateway_ready is True
    assert app.gateway_interactions_ready() is False

    release_close.set()
    await closing


@pytest.mark.asyncio
async def test_ready_does_not_initialize_after_close_has_started(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _build_test_app(monkeypatch)
    close_started = asyncio.Event()
    release_close = asyncio.Event()
    initialize = AsyncMock(return_value=True)
    sync = AsyncMock(return_value=[])
    start_background = MagicMock()

    async def blocked_close_resources() -> None:
        close_started.set()
        await release_close.wait()

    monkeypatch.setattr(app, "_close_resources", blocked_close_resources)
    monkeypatch.setattr(app, "_initialize_ready_locked", initialize)
    monkeypatch.setattr(app.bot.tree, "sync", sync)
    monkeypatch.setattr(app, "_start_ready_background_tasks_locked", start_background)

    closing = asyncio.create_task(app.close())
    await close_started.wait()
    await app.on_ready()

    initialize.assert_not_awaited()
    sync.assert_not_awaited()
    start_background.assert_not_called()
    assert app.gateway_ready is False

    release_close.set()
    await closing


@pytest.mark.asyncio
async def test_close_waits_for_ready_initialization_then_prevents_reentry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _build_test_app(monkeypatch)
    initialize_started = asyncio.Event()
    release_initialize = asyncio.Event()
    close_started = asyncio.Event()
    events: list[str] = []
    initialize_calls = 0

    async def blocked_initialize() -> bool:
        nonlocal initialize_calls
        initialize_calls += 1
        events.append("ready-start")
        initialize_started.set()
        await release_initialize.wait()
        events.append("ready-finish")
        return True

    async def close_resources() -> None:
        events.append("close")
        close_started.set()

    monkeypatch.setattr(app, "_initialize_ready_locked", blocked_initialize)
    monkeypatch.setattr(app, "_close_resources", close_resources)
    monkeypatch.setattr(app, "_start_ready_background_tasks_locked", MagicMock())
    app.workspace_sweeper_started = True

    ready = asyncio.create_task(app.on_ready())
    await initialize_started.wait()
    closing = asyncio.create_task(app.close())
    await asyncio.sleep(0)

    assert close_started.is_set() is False
    assert app._closed is False

    release_initialize.set()
    await asyncio.gather(ready, closing)
    await app.on_ready()

    assert events == ["ready-start", "ready-finish", "close"]
    assert initialize_calls == 1
    assert app._closed is True


@pytest.mark.asyncio
async def test_concurrent_close_waits_for_the_single_teardown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _build_test_app(monkeypatch)
    close_started = asyncio.Event()
    release_close = asyncio.Event()
    close_calls = 0

    async def blocked_close_resources() -> None:
        nonlocal close_calls
        close_calls += 1
        close_started.set()
        await release_close.wait()

    monkeypatch.setattr(app, "_close_resources", blocked_close_resources)

    first = asyncio.create_task(app.close())
    await close_started.wait()
    second = asyncio.create_task(app.close())
    await asyncio.sleep(0)

    assert second.done() is False
    release_close.set()
    await asyncio.gather(first, second)

    assert close_calls == 1


@pytest.mark.asyncio
async def test_application_close_uninstalls_module_gateway_events_before_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _build_test_app(monkeypatch)
    published: list[str] = []

    class _Gateway:
        def __init__(self) -> None:
            self.listeners: list[tuple[Any, str]] = []

        def add_listener(self, callback: Any, name: str) -> None:
            self.listeners.append((callback, name))

        def remove_listener(self, callback: Any, name: str) -> None:
            self.listeners.remove((callback, name))

        async def dispatch_message(self, message: object) -> None:
            for callback, name in list(self.listeners):
                if name == "on_message":
                    await callback(message)

    gateway = _Gateway()
    publisher = ModuleEventPublisher(
        gateway,  # type: ignore[arg-type]
        lambda topic, _payload: published.append(topic),
    )
    publisher.install()
    app._module_event_publisher = publisher
    close_started = asyncio.Event()
    release_close = asyncio.Event()

    class _BlockingScheduler:
        async def close(self) -> None:
            close_started.set()
            await release_close.wait()

    monkeypatch.setattr(app.tools.module_manager, "scheduler", _BlockingScheduler())
    message = SimpleNamespace(
        id=3,
        guild=SimpleNamespace(id=1),
        channel=SimpleNamespace(id=2, parent_id=None),
        author=SimpleNamespace(id=4, display_name="Ada", bot=False),
        content="hello",
        attachments=(),
        jump_url="",
        created_at=None,
        reference=None,
        pinned=False,
        edited_at=None,
        embeds=(),
    )

    await gateway.dispatch_message(message)
    assert published == ["discord.message"]
    published.clear()

    closing = asyncio.create_task(app.close())
    await close_started.wait()
    await gateway.dispatch_message(message)

    assert published == []
    assert app._module_event_publisher is None

    release_close.set()
    await closing
    await app.close()


@pytest.mark.asyncio
async def test_on_ready_closes_client_when_required_startup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _build_test_app(monkeypatch)
    failure = RuntimeError("required module failed")

    async def fail_startup() -> None:
        raise failure

    close = AsyncMock()
    monkeypatch.setattr(app, "_first_init_core", fail_startup)
    monkeypatch.setattr(app.bot, "close", close)

    await app.on_ready()

    response = MagicMock()
    response.is_done.return_value = False
    response.send_message = AsyncMock()
    interaction = MagicMock()
    interaction.id = 42
    interaction.type = discord.InteractionType.application_command
    interaction.response = response
    interaction.followup.send = AsyncMock()
    allowed = await app.bot.tree.interaction_check(interaction)

    assert app._startup_error is failure
    assert app.db_initialized is False
    assert app.gateway_ready is False
    assert allowed is False
    response.send_message.assert_awaited_once_with(
        "The bot is still starting up or temporarily unavailable. Please try again shortly.",
        ephemeral=True,
    )
    close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_on_message_is_ignored_before_ready_initialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _build_test_app(monkeypatch)
    app.context_manager = cast(ContextManager, object())
    handler = AsyncMock()
    monkeypatch.setattr(app, "_on_message_for_user", handler)

    await app.on_message(cast(discord.Message, object()))

    handler.assert_not_awaited()
    assert (await app.turn_admission.snapshot()).active_total == 0


@pytest.mark.asyncio
async def test_on_ready_delegates_memory_manager_and_starts_one_activation_refresher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _build_test_app(monkeypatch)
    created_tasks = []
    manager = FakeMemoryManager()
    conversation_store = object()
    preference_store = object()

    def fake_create_task(coro: Any) -> object:
        created_tasks.append(coro)
        coro.close()
        return object()

    async def fake_sync() -> list[object]:
        return []

    app.db_initialized = True
    app.context_manager = cast(ContextManager, object())
    app.conversation_store = cast(ConversationStore, conversation_store)
    app.preference_store = cast(PreferenceStore, preference_store)
    app.memory_manager = cast(MemoryManager, manager)
    app.workspace_sweeper_started = True
    monkeypatch.setattr(app.settings, "tool_event_log_enabled", False)
    monkeypatch.setattr(app.bot.tree, "sync", fake_sync)
    monkeypatch.setattr(asyncio, "create_task", fake_create_task)

    await app.on_ready()
    await app.on_ready()

    assert len(created_tasks) == 1
    assert manager.ensure_calls == [
        (conversation_store, preference_store),
        (conversation_store, preference_store),
    ]
    assert app.gateway_ready is True


@pytest.mark.asyncio
async def test_concurrent_ready_starts_one_attachment_and_workspace_sweeper_pair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _build_test_app(monkeypatch)
    app.db_initialized = True
    app.memory_manager = cast(MemoryManager, FakeMemoryManager(client=None))
    app.workspace_sweeper_started = False
    app._guild_activation_refresh_task = cast(asyncio.Task, object())
    sweep_started = asyncio.Event()
    release_sweep = asyncio.Event()
    startup_calls: list[tuple[object, float, int]] = []
    background_coroutines: list[str] = []
    real_create_task = asyncio.create_task

    async def fake_sync(*, guild: object | None = None) -> list[object]:
        assert guild is None
        return []

    async def fake_startup_sweep(
        store: object,
        *,
        max_age_seconds: float,
        max_files: int,
    ) -> int:
        startup_calls.append((store, max_age_seconds, max_files))
        sweep_started.set()
        await release_sweep.wait()
        return 0

    def fake_background_task(coro: Any) -> object:
        background_coroutines.append(coro.cr_code.co_name)
        coro.close()
        return object()

    monkeypatch.setattr(app.settings, "tool_event_log_enabled", False)
    monkeypatch.setattr(app.bot.tree, "sync", fake_sync)
    monkeypatch.setattr(app_runtime, "sweep_attachment_orphans_once", fake_startup_sweep)
    monkeypatch.setattr(asyncio, "create_task", fake_background_task)

    first = real_create_task(app.on_ready())
    await sweep_started.wait()
    second = real_create_task(app.on_ready())
    await asyncio.sleep(0)
    release_sweep.set()
    await asyncio.gather(first, second)

    assert startup_calls == [
        (
            app.tools.attachment_store,
            app.settings.attachment_orphan_ttl_seconds,
            app.settings.attachment_orphan_sweep_max_files,
        )
    ]
    assert sorted(background_coroutines) == [
        "attachment_orphan_sweeper",
        "workspace_sweeper",
    ]


@pytest.mark.asyncio
async def test_concurrent_ready_installs_one_nonblocking_video_sweeper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _build_test_app(monkeypatch)
    app.db_initialized = True
    app.memory_manager = cast(MemoryManager, FakeMemoryManager(client=None))
    app.workspace_sweeper_started = True
    app.video_session_store = cast(Any, object())
    app._guild_activation_refresh_task = cast(asyncio.Task, object())
    sweep_started = asyncio.Event()
    release_sweep = asyncio.Event()
    sweep_calls = 0

    class BlockingVideoService:
        async def sweep(self, *, now: float | None = None) -> tuple[int, bool]:
            nonlocal sweep_calls
            sweep_calls += 1
            sweep_started.set()
            await release_sweep.wait()
            return 0, True

        async def close(self) -> None:
            return None

    async def fake_sync(*, guild: object | None = None) -> list[object]:
        assert guild is None
        return []

    app.tools.video_service = cast(Any, BlockingVideoService())
    monkeypatch.setattr(app.settings, "tool_event_log_enabled", False)
    monkeypatch.setattr(app.bot.tree, "sync", fake_sync)

    await asyncio.wait_for(asyncio.gather(app.on_ready(), app.on_ready()), timeout=0.5)
    await asyncio.wait_for(sweep_started.wait(), timeout=0.5)

    assert sweep_calls == 1
    assert app.video_session_sweeper_started is True
    assert app._video_session_sweeper_task is not None
    release_sweep.set()
    app._guild_activation_refresh_task = None
    await app.close()


@pytest.mark.asyncio
async def test_close_during_startup_attachment_sweep_does_not_start_sweepers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _build_test_app(monkeypatch)
    app.db_initialized = True
    app.memory_manager = cast(MemoryManager, FakeMemoryManager(client=None))
    app.workspace_sweeper_started = False
    sweep_started = asyncio.Event()
    release_sweep = asyncio.Event()

    async def fake_sync(*, guild: object | None = None) -> list[object]:
        assert guild is None
        return []

    async def fake_startup_sweep(
        store: object,
        *,
        max_age_seconds: float,
        max_files: int,
    ) -> int:
        del store, max_age_seconds, max_files
        sweep_started.set()
        await release_sweep.wait()
        return 0

    monkeypatch.setattr(app.settings, "tool_event_log_enabled", False)
    monkeypatch.setattr(app.bot.tree, "sync", fake_sync)
    monkeypatch.setattr(app_runtime, "sweep_attachment_orphans_once", fake_startup_sweep)

    ready_task = asyncio.get_running_loop().create_task(app.on_ready())
    await sweep_started.wait()
    closing = asyncio.create_task(app.close())
    await asyncio.sleep(0)
    assert closing.done() is False
    release_sweep.set()
    await asyncio.gather(ready_task, closing)

    assert app._workspace_sweeper_task is None
    assert app._attachment_sweeper_task is None


@pytest.mark.asyncio
async def test_on_ready_ensures_memory_before_sync_on_reconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _build_test_app(monkeypatch)
    events: list[str] = []

    class OrderingMemoryManager(FakeMemoryManager):
        async def ensure_ready(
            self,
            conversation_store: object,
            preference_store: object,
        ) -> None:
            events.append("memory")
            await super().ensure_ready(conversation_store, preference_store)

    async def fake_sync() -> list[object]:
        events.append("sync")
        return []

    app.db_initialized = True
    app.context_manager = cast(ContextManager, object())
    app.conversation_store = cast(ConversationStore, object())
    app.preference_store = cast(PreferenceStore, object())
    app.memory_manager = cast(MemoryManager, OrderingMemoryManager())
    app.workspace_sweeper_started = True
    monkeypatch.setattr(app.settings, "tool_event_log_enabled", False)
    monkeypatch.setattr(app.bot.tree, "sync", fake_sync)

    await app.on_ready()

    assert events == ["memory", "sync"]


@pytest.mark.asyncio
async def test_on_ready_waits_for_first_startup_before_reconnect_ready_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _build_test_app(monkeypatch)
    first_init_started = asyncio.Event()
    release_first_init = asyncio.Event()
    events: list[str] = []
    init_calls = 0
    sync_count = 0

    async def fake_first_init_core() -> None:
        nonlocal init_calls
        init_calls += 1
        events.append("init-start")
        first_init_started.set()
        await release_first_init.wait()
        app.context_manager = cast(ContextManager, object())
        app.conversation_store = cast(ConversationStore, object())
        app.preference_store = cast(PreferenceStore, object())
        events.append("init-finish")

    class OrderingMemoryManager(FakeMemoryManager):
        async def ensure_ready(
            self,
            conversation_store: object,
            preference_store: object,
        ) -> None:
            events.append("memory")
            await super().ensure_ready(conversation_store, preference_store)

    async def fake_sync() -> list[object]:
        nonlocal sync_count
        sync_count += 1
        events.append("sync")
        return []

    manager = OrderingMemoryManager()
    app.memory_manager = cast(MemoryManager, manager)
    app.workspace_sweeper_started = True
    monkeypatch.setattr(app.settings, "tool_event_log_enabled", False)
    monkeypatch.setattr(app, "_first_init_core", fake_first_init_core)
    monkeypatch.setattr(app.bot.tree, "sync", fake_sync)

    first_ready = asyncio.create_task(app.on_ready())
    await first_init_started.wait()
    second_ready = asyncio.create_task(app.on_ready())
    await asyncio.sleep(0)

    assert app.db_initialized is False
    second_finished_during_first_init = second_ready.done()

    release_first_init.set()
    results = await asyncio.gather(first_ready, second_ready, return_exceptions=True)

    assert second_finished_during_first_init is False
    assert not any(isinstance(result, BaseException) for result in results), results
    assert init_calls == 1
    assert len(manager.ensure_calls) == 2
    assert sync_count == 2
    assert events.index("init-finish") < events.index("memory")


@pytest.mark.asyncio
async def test_on_ready_skips_memory_manager_when_no_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _build_test_app(monkeypatch)
    created_tasks = []
    manager = FakeMemoryManager(client=None)

    def fake_create_task(coro: Any) -> object:
        created_tasks.append(coro)
        coro.close()
        return object()

    async def fake_sync() -> list[object]:
        return []

    app.db_initialized = True
    app.context_manager = cast(ContextManager, object())
    app.conversation_store = cast(ConversationStore, object())
    app.preference_store = cast(PreferenceStore, object())
    app.memory_manager = cast(MemoryManager, manager)
    app.workspace_sweeper_started = True
    monkeypatch.setattr(app.settings, "tool_event_log_enabled", False)
    monkeypatch.setattr(app.bot.tree, "sync", fake_sync)
    monkeypatch.setattr(asyncio, "create_task", fake_create_task)

    await app.on_ready()

    assert len(created_tasks) == 1
    assert manager.ensure_calls == []


@pytest.mark.asyncio
async def test_application_close_runs_memory_and_provider_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _build_test_app(monkeypatch)
    memory_manager = FakeMemoryManager()
    provider_manager = StubProviderManager(app.settings)
    app.memory_manager = cast(MemoryManager, memory_manager)
    app.provider_manager = cast(provider_runtime.ProviderManager, provider_manager)

    await app.close()
    await app.close()

    assert memory_manager.close_count == 1
    assert provider_manager.close_count == 1


@pytest.mark.asyncio
async def test_application_close_drains_active_message_before_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _build_test_app(monkeypatch)
    app.gateway_ready = True
    app.context_manager = cast(ContextManager, object())
    app.blocked_user_store = None
    events: list[str] = []
    entered = asyncio.Event()

    async def active_message(_message: object) -> None:
        events.append("turn-start")
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            events.append("turn-exit")

    async def close_provider() -> None:
        events.append("provider")

    class _OrderingMemoryManager(FakeMemoryManager):
        async def close(self) -> None:
            events.append("memory")

    class _OrderingDatabase:
        async def close(self) -> None:
            events.append("database")

    monkeypatch.setattr(app_runtime, "is_eligible_to_respond", lambda *args, **kwargs: True)
    monkeypatch.setattr(app_runtime, "can_send_reply", lambda *args, **kwargs: True)
    monkeypatch.setattr(app, "_should_respond", lambda *args, **kwargs: True)
    monkeypatch.setattr(app, "_on_message_for_user", active_message)
    monkeypatch.setattr(app.provider_manager, "close", close_provider)
    app.memory_manager = cast(MemoryManager, _OrderingMemoryManager())
    app.database = cast(Database, _OrderingDatabase())
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 100
    guild = MagicMock()
    guild.id = 999
    guild.me = None
    author = MagicMock()
    author.id = 123
    message = MagicMock()
    message.channel = channel
    message.guild = guild
    message.author = author
    message.content = "work"
    message.type = discord.MessageType.default

    turn = asyncio.create_task(app.on_message(message))
    await entered.wait()
    await app.close()

    with pytest.raises(asyncio.CancelledError):
        await turn
    assert events == ["turn-start", "turn-exit", "memory", "provider", "database"]


@pytest.mark.asyncio
async def test_bot_close_drains_application_before_discord_disconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _build_test_app(monkeypatch)
    events: list[str] = []

    async def close_application() -> None:
        events.append("application")

    async def close_discord(_bot: object) -> None:
        events.append("discord")

    monkeypatch.setattr(app, "close", close_application)
    monkeypatch.setattr(app_runtime.commands.Bot, "close", close_discord)

    await app.bot.close()

    assert events == ["application", "discord"]


@pytest.mark.asyncio
async def test_close_cancels_activation_refresher(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _build_test_app(monkeypatch)
    task = asyncio.create_task(app._guild_activation_refresh_loop())
    app._guild_activation_refresh_task = task

    await app.close()

    assert task.cancelled()
    assert app._guild_activation_refresh_task is None


@pytest.mark.asyncio
async def test_close_cancels_video_session_sweeper(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _build_test_app(monkeypatch)
    started = asyncio.Event()

    async def blocked_sweeper() -> None:
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(blocked_sweeper())
    await started.wait()
    app._video_session_sweeper_task = task
    app.video_session_sweeper_started = True

    await app.close()

    assert task.cancelled()
    assert app._video_session_sweeper_task is None
    assert app.video_session_sweeper_started is False


@pytest.mark.asyncio
async def test_application_close_drains_privacy_deletions_before_state_clients(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _build_test_app(monkeypatch)
    events: list[str] = []

    class _OrderingMemoryManager(FakeMemoryManager):
        async def close(self) -> None:
            events.append("memory")
            await super().close()

    class _OrderingDatabase:
        async def close(self) -> None:
            events.append("database")

    async def drain_privacy_deletions() -> None:
        events.append("privacy")

    app.memory_manager = cast(MemoryManager, _OrderingMemoryManager())
    app.database = cast(Database, _OrderingDatabase())
    monkeypatch.setattr(
        app_runtime,
        "drain_confirmed_privacy_deletions",
        drain_privacy_deletions,
    )

    await app.close()

    assert events == ["privacy", "memory", "database"]


@pytest.mark.asyncio
async def test_startup_replay_tombstones_failed_user_but_allows_others(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    app = _build_test_app(monkeypatch)
    app.database = Database(tmp_path / "bot.db")
    await app.database.connect()
    app.memory_manager = cast(MemoryManager, FakeMemoryManager(client=None))

    class _Conversations:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.saw_later_user_tombstoned = False

        async def delete_user_data(self, user_id: str) -> UserDataDeletion:
            self.calls.append(user_id)
            if user_id == "42":
                with pytest.raises(PrivacyDeletionPendingError):
                    async with app.privacy_barrier.activity(WorkspaceKey("99")):
                        pass
                self.saw_later_user_tombstoned = True
                raise RuntimeError("simulated startup deletion failure")
            return UserDataDeletion(conversations_deleted=0, messages_scrubbed=0)

    class _Preferences:
        async def set_memory_enabled(self, user_id: str, enabled: bool) -> bool:
            return True

        async def clear_persona(self, user_id: str) -> None:
            return None

    conversations = _Conversations()
    app.conversation_store = cast(ConversationStore, conversations)
    app.preference_store = cast(PreferenceStore, _Preferences())
    app.privacy_deletion_store = PrivacyDeletionRequestStore(app.database)
    for user_id in ("42", "99"):
        await app.privacy_deletion_store.request(
            user_id=user_id,
            scope="all",
            memory_backend_required=False,
        )
    try:
        await app._resume_pending_privacy_deletions(
            auto_retain_watermarks=AutoRetainStore(app.database),
        )

        assert conversations.calls == ["42", "99"]
        assert conversations.saw_later_user_tombstoned is True
        assert [request.user_id for request in await app.privacy_deletion_store.list_pending()] == [
            "42"
        ]
        with pytest.raises(PrivacyDeletionPendingError):
            async with app.privacy_barrier.activity(WorkspaceKey("42")):
                pass
        async with app.privacy_barrier.activity(WorkspaceKey("99")):
            pass
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_close_provider_resources_closes_main_compaction_and_persona(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main_provider = CloseableProvider("main")
    compaction_provider = CloseableProvider("compaction")
    persona_provider = CloseableProvider("persona")
    manager = provider_runtime.ProviderManager(
        settings=_settings(),
        main=cast(LLMProvider, main_provider),
        compaction=cast(LLMProvider, compaction_provider),
        persona=cast(LLMProvider, persona_provider),
    )

    await manager.close()

    assert main_provider.close_count == 1
    assert compaction_provider.close_count == 1
    # ProviderManager is the persona provider's sole shutdown owner.
    assert persona_provider.close_count == 1
    assert manager.main is None
    assert manager.compaction is None
    assert manager.persona is None


@pytest.mark.asyncio
async def test_close_provider_resources_closes_shared_provider_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = CloseableProvider()
    manager = provider_runtime.ProviderManager(
        settings=_settings(),
        main=cast(LLMProvider, provider),
        compaction=cast(LLMProvider, provider),
    )

    await manager.close()

    assert provider.close_count == 1
    assert manager.main is None
    assert manager.compaction is None


@pytest.mark.asyncio
async def test_on_ready_starts_retention_sweeper_when_window_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _build_test_app(monkeypatch)
    monkeypatch.setattr(app.settings, "transcript_retention_days", 30)
    created_tasks: list[Any] = []
    manager = FakeMemoryManager(client=None)

    def fake_create_task(coro: Any) -> object:
        created_tasks.append(coro)
        coro.close()
        return object()

    async def fake_sync() -> list[object]:
        return []

    app.db_initialized = True
    app.context_manager = cast(ContextManager, object())
    app.conversation_store = cast(ConversationStore, object())
    app.preference_store = cast(PreferenceStore, object())
    app.memory_manager = cast(MemoryManager, manager)
    app.workspace_sweeper_started = True
    monkeypatch.setattr(app.bot.tree, "sync", fake_sync)
    monkeypatch.setattr(asyncio, "create_task", fake_create_task)

    await app.on_ready()

    assert app.transcript_retention_sweeper_started is True
    assert len(created_tasks) == 2
    # Idempotent: a reconnect does not start a second sweeper.
    await app.on_ready()
    assert len(created_tasks) == 2


@pytest.mark.asyncio
async def test_global_command_sync_failure_does_not_block_local_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _build_test_app(monkeypatch)
    app.db_initialized = True
    app.context_manager = cast(ContextManager, object())
    app.conversation_store = cast(ConversationStore, object())
    app.preference_store = cast(PreferenceStore, object())
    app.memory_manager = cast(MemoryManager, FakeMemoryManager(client=None))
    app.workspace_sweeper_started = True

    async def fail_sync(*args: object, **kwargs: object) -> list[object]:
        raise discord.HTTPException(
            cast(Any, type("R", (), {"status": 500, "reason": "error"})()),
            "sync failed",
        )

    monkeypatch.setattr(app.bot.tree, "sync", fail_sync)

    # Reaching the end of on_ready is the assertion: execution passed the
    # sync try/except and ran the remaining READY work to completion.
    await app.on_ready()


@pytest.mark.asyncio
async def test_transcript_retention_sweeper_drains_in_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import discord_adapter.lifecycle as bot_lifecycle

    batch = bot_lifecycle._RETENTION_BATCH_SIZE
    # Full, full, partial -> the partial batch signals the backlog is drained.
    returns = iter([batch, batch, 3])
    calls: list[int] = []

    class FakeStore:
        async def delete_conversations_older_than(self, cutoff: float, *, limit: int) -> int:
            calls.append(limit)
            return next(returns, 0)

    sleeps = 0

    async def fake_sleep(_seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        # Let the first sweep run, then break out of the while-True loop.
        if sleeps >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(bot_lifecycle.asyncio, "sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await bot_lifecycle.transcript_retention_sweeper(
            cast(Any, FakeStore()),
            retention_days=30,
            sweep_interval=3600,
        )

    # One sweep drained in three batches; every batch used the configured size.
    assert calls == [batch, batch, batch]


@pytest.mark.asyncio
async def test_attachment_orphan_sweeper_is_bounded_and_periodic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import discord_adapter.lifecycle as bot_lifecycle

    calls: list[tuple[float, int]] = []

    class FakeAttachmentStore:
        async def sweep_orphans(
            self,
            *,
            max_age_seconds: float,
            max_files: int,
        ) -> int:
            calls.append((max_age_seconds, max_files))
            return 2

    sleeps = 0

    async def fake_sleep(seconds: float) -> None:
        nonlocal sleeps
        assert seconds == 3600
        sleeps += 1
        if sleeps >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(bot_lifecycle.asyncio, "sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await bot_lifecycle.attachment_orphan_sweeper(
            cast(Any, FakeAttachmentStore()),
            sweep_interval=3600,
            max_age_seconds=86400,
            max_files=1000,
        )

    assert calls == [(86400, 1000)]


@pytest.mark.asyncio
async def test_attachment_orphan_sweep_failure_degrades_to_zero() -> None:
    import discord_adapter.lifecycle as bot_lifecycle

    class FailingAttachmentStore:
        async def sweep_orphans(
            self,
            *,
            max_age_seconds: float,
            max_files: int,
        ) -> int:
            raise OSError("disk unavailable")

    assert (
        await bot_lifecycle.sweep_attachment_orphans_once(
            cast(Any, FailingAttachmentStore()),
            max_age_seconds=86400,
            max_files=1000,
        )
        == 0
    )


@pytest.mark.asyncio
async def test_workspace_sweeper_waits_for_active_workspace_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import discord_adapter.lifecycle as bot_lifecycle

    locks = UserLocks()
    swept = asyncio.Event()

    class FakeWorkspace:
        async def sweep_expired(
            self, *, excluded_workspace_keys: frozenset[str] = frozenset()
        ) -> int:
            del excluded_workspace_keys
            swept.set()
            return 0

    real_sleep = asyncio.sleep

    async def no_sleep(_seconds: float) -> None:
        await real_sleep(0)

    monkeypatch.setattr(bot_lifecycle.asyncio, "sleep", no_sleep)
    async with locks.activity(WorkspaceKey("user__guild")):
        task = asyncio.create_task(
            bot_lifecycle.workspace_sweeper(
                cast(Any, FakeWorkspace()),
                sweep_interval=3600,
                workspace_locks=locks,
            )
        )
        await asyncio.sleep(0)
        assert not swept.is_set()

    await asyncio.wait_for(swept.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
