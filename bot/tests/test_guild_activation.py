from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.guild_activation import GuildActivationConfig, GuildActivationService
from app.modules import ModuleManager
from config.fragments.guild_config import server_setup_activation
from kimi_agent_module_api.contracts import GuildSettingField, GuildSettingsSchema
from modules.guild_settings import GUILD_MODULES_DIR, GuildSettingsService
from tests.app_state_probes import guild_activation_task


class FakeBot:
    guilds: list[Any] = []

    def get_channel(self, channel_id: int, /) -> None:
        del channel_id

    async def fetch_channel(self, channel_id: int, /) -> Any:
        raise AssertionError(f"unexpected channel fetch: {channel_id}")


def _service(
    tmp_path: Path,
    *,
    allowed_guilds: frozenset[int] = frozenset(),
    manager: ModuleManager | None = None,
    refresh_seconds: float = 5.0,
) -> GuildActivationService:
    return GuildActivationService(
        config=GuildActivationConfig(
            config_dir=tmp_path,
            allowed_guilds=allowed_guilds,
            refresh_seconds=refresh_seconds,
        ),
        bot=FakeBot(),
        module_manager=manager or ModuleManager(),
        activation_parser=server_setup_activation,
    )


@pytest.mark.asyncio
async def test_saved_server_setup_activates_guild_after_refresh(tmp_path: Path) -> None:
    service = _service(tmp_path)
    assert service.active_guilds() == set()

    servers = tmp_path / "servers"
    servers.mkdir()
    (servers / "999.md").write_text("---\nbot_active: true\n---\n", encoding="utf-8")

    await service.refresh_guild_activation(999)

    assert service.active_guilds() == {999}
    assert service.guild_activation_state(999).active is True
    assert service.guild_activation_state(999).activation == "server_setup"


def test_saved_deactivation_overrides_environment_allowlist(tmp_path: Path) -> None:
    servers = tmp_path / "servers"
    servers.mkdir()
    (servers / "999.md").write_text("---\nbot_active: false\n---\n", encoding="utf-8")

    service = _service(tmp_path, allowed_guilds=frozenset({999}))

    assert service.active_guilds() == set()
    assert service.guild_activation_state(999).as_dict() == {
        "active": False,
        "activation": "deactivated",
        "setup_state": "deactivated",
        "environment_approved": True,
    }


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

    guild_settings = GuildSettingsService(
        config_dir=config_dir,
        schemas={
            "mod": GuildSettingsSchema(
                fields=(GuildSettingField("count", "int", required=True),),
                invalid_policy="disable_module",
            )
        },
        on_health=lambda _module, _state, _detail: health_threads.append(threading.get_ident()),
    )
    guild_settings.subscribe(
        "mod", lambda _guild_id: callback_threads.append(threading.get_ident())
    )
    manager = ModuleManager(guild_settings=guild_settings)
    service = _service(tmp_path, manager=manager)

    await service.refresh_module_guild_settings(guild_id)

    assert read_threads and all(thread_id != loop_thread for thread_id in read_threads)
    assert callback_threads == [loop_thread]
    assert health_threads == [loop_thread]


@pytest.mark.asyncio
async def test_close_cancels_activation_refresher(tmp_path: Path) -> None:
    service = _service(tmp_path, refresh_seconds=60.0)
    service.start()
    task = guild_activation_task(service)
    assert task is not None

    await service.close()

    assert task.cancelled()
    assert guild_activation_task(service) is None


@pytest.mark.asyncio
async def test_known_guilds_and_channel_resolution_use_bot_state(tmp_path: Path) -> None:
    class ChannelBot(FakeBot):
        guilds = [SimpleNamespace(id=222)]

        def get_channel(self, channel_id: int, /) -> Any:
            assert channel_id == 333
            return SimpleNamespace(guild=SimpleNamespace(id=444))

    service = GuildActivationService(
        config=GuildActivationConfig(
            config_dir=tmp_path,
            allowed_guilds=frozenset({111}),
            refresh_seconds=5.0,
        ),
        bot=ChannelBot(),
        module_manager=ModuleManager(),
        activation_parser=server_setup_activation,
    )

    assert service.known_guild_ids() == {111, 222}
    assert await service.channel_guild_id(333) == 444
