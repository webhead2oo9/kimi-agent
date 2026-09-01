from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import discord
import pytest

from app import runtime as app_runtime
from config.fragments.tool_policy import ToolPolicyLoadError
from config.settings import Settings
from storage.db import Database
from storage.model_selection import ModelSelectionStore
from tests.helpers import LifecycleProbe, StubProviderManager
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


class _DeliveryApp:
    """The slice of KimiApplication that send_response touches.

    send_response routes through the real workspace guard so the lock
    condition under test is the production one, not a copy.
    """

    _deliver_with_workspace_guard = app_runtime.KimiApplication._deliver_with_workspace_guard

    def __init__(self, gateway: object, locks: UserLocks) -> None:
        self.discord_gateway = gateway
        self.tools = SimpleNamespace(workspace_locks=locks)


@pytest.mark.asyncio
async def test_text_response_does_not_wait_for_workspace_writer() -> None:
    locks = UserLocks()
    gateway = SimpleNamespace(send_response=AsyncMock(return_value=[]))
    app = cast(app_runtime.KimiApplication, _DeliveryApp(gateway, locks))
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
    app = cast(app_runtime.KimiApplication, _DeliveryApp(gateway, locks))
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
    await LifecycleProbe(app).first_init_core()

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


def test_saved_server_setup_activates_command_tree_without_restart(
    monkeypatch, tmp_path: Path
) -> None:
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

    await LifecycleProbe(app).first_init_core()

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
