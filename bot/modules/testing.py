"""Integration harness: real core composition for module tests.

``build_test_runtime`` opens a real SQLite database under ``tmp_path``, loads
the requested modules through ``ModuleManager`` exactly as the bot does,
applies their migrations, and starts them with a runtime context whose ports
are the real implementations where core has them and the public fakes from
``kimi_agent_module_api.testing`` elsewhere. Health, services, and storage are
always the real core implementations; inspect them via ``runtime.manager``.
Module-package tests may import this; module production source may not.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.modules import ModuleManager, ModuleRuntimeContext, ModuleSpec
from config.settings import Settings
from kimi_agent_module_api.testing import (
    FakeDiscordActions,
    FakeEvents,
    FakeGuildSettings,
    FakeHttp,
    FakeInteractions,
    FakeScheduler,
    FakeTrust,
)
from storage.db import Database
from tools.registry import ToolRegistry


class FakeTree:
    def __init__(self) -> None:
        self.commands: list[Any] = []

    def add_command(self, command: Any, **_kwargs: Any) -> None:
        self.commands.append(command)

    def remove_command(self, name: str) -> None:
        self.commands = [command for command in self.commands if command.name != name]


class FakeBot:
    """The raw ``ctx.bot`` stand-in for modules that still register directly."""

    def __init__(self) -> None:
        self.tree = FakeTree()
        self.listeners: list[tuple[Any, str]] = []

    def add_listener(self, callback: Any, name: str) -> None:
        self.listeners.append((callback, name))

    def remove_listener(self, callback: Any, name: str) -> None:
        self.listeners.remove((callback, name))


@dataclass(slots=True)
class ModulePorts:
    """The fakes handed to one module; inspect them to assert what it did."""

    events: FakeEvents
    scheduler: FakeScheduler
    discord: FakeDiscordActions
    interactions: FakeInteractions
    guild_settings: FakeGuildSettings
    http: FakeHttp


@dataclass(slots=True)
class TestRuntime:
    manager: ModuleManager
    database: Database
    bot: FakeBot
    settings: Settings
    config_dir: Path
    trust: FakeTrust
    ports: dict[str, ModulePorts] = field(default_factory=dict)
    _closed: bool = False

    def ctx_for(self, module_name: str) -> ModuleRuntimeContext:
        return self.manager.context_for(module_name)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self.manager.close()
        await self.database.close()


def write_guild_config(config_dir: Path, guild_id: int, frontmatter: Mapping[str, Any]) -> Path:
    """Write ``servers/<guild_id>.md`` with the given frontmatter keys."""
    servers = config_dir / "servers"
    servers.mkdir(parents=True, exist_ok=True)
    lines = ["---"]
    for key, value in frontmatter.items():
        if isinstance(value, list | tuple):
            rendered = "[" + ", ".join(str(item) for item in value) + "]"
        else:
            rendered = str(value)
        lines.append(f"{key}: {rendered}")
    lines.append("---")
    lines.append("")
    path = servers / f"{guild_id}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


async def build_test_runtime(
    tmp_path: Path,
    names: Sequence[str],
    *,
    installed: Mapping[str, ModuleSpec] | None = None,
    env: Mapping[str, str] | None = None,
    guild_config: Mapping[int, Mapping[str, Any]] | None = None,
    active_guilds: Callable[[int], bool] | None = None,
    trust: FakeTrust | None = None,
    http_routes: Mapping[str, Any] | None = None,
) -> TestRuntime:
    """Load, migrate, and start ``names`` against a fresh database in ``tmp_path``.

    ``env`` is applied to ``os.environ`` for the duration of module settings
    construction only; callers that need it to persist should use monkeypatch.
    ``installed`` bypasses entry-point discovery, as ``ModuleManager.load`` does.
    """
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    for guild_id, frontmatter in (guild_config or {}).items():
        write_guild_config(config_dir, guild_id, frontmatter)

    previous = {key: os.environ.get(key) for key in (env or {})}
    os.environ.update(env or {})
    try:
        settings = Settings(_env_file=None, config_dir=str(config_dir))  # type: ignore[call-arg]
        manager = ModuleManager.load(
            tuple(names),
            core_settings=settings,
            registry=ToolRegistry(),
            gateway=object(),  # type: ignore[arg-type]
            installed=installed,
        )
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    database = Database(str(tmp_path / "bot.db"))
    await database.connect()
    bot = FakeBot()
    trust_lookup = trust or FakeTrust()
    ports: dict[str, ModulePorts] = {}

    def ports_for(spec: ModuleSpec) -> ModulePorts:
        created = ModulePorts(
            events=FakeEvents(spec.name),
            scheduler=FakeScheduler(),
            discord=FakeDiscordActions(spec.name, spec.permissions.discord_actions),
            interactions=FakeInteractions(spec.name),
            guild_settings=FakeGuildSettings(),
            http=FakeHttp(http_routes),
        )
        ports[spec.name] = created
        return created

    base = ModuleRuntimeContext(
        bot=bot,  # type: ignore[arg-type]
        database=database,
        trust_resolver=object(),  # type: ignore[arg-type]
        gateway=object(),  # type: ignore[arg-type]
        config_dir=config_dir,
        is_guild_active=active_guilds or (lambda _guild_id: True),
        get_module=manager.get,
        trust=trust_lookup,
        current_config_dir=lambda: config_dir,
    )

    def per_module(spec: ModuleSpec, ctx: ModuleRuntimeContext) -> ModuleRuntimeContext:
        from dataclasses import replace

        p = ports_for(spec)
        return replace(
            ctx,
            events=p.events,
            scheduler=p.scheduler,
            discord=p.discord,
            interactions=p.interactions,
            guild_settings=p.guild_settings,
            http=p.http,
        )

    runtime = TestRuntime(
        manager=manager,
        database=database,
        bot=bot,
        settings=settings,
        config_dir=config_dir,
        trust=trust_lookup,
        ports=ports,
    )
    try:
        await manager.start(base, customize=per_module)
    except BaseException:
        await database.close()
        raise
    return runtime


__all__ = [
    "FakeBot",
    "FakeTree",
    "ModulePorts",
    "TestRuntime",
    "build_test_runtime",
    "write_guild_config",
]
