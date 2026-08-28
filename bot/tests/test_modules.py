"""Versioned application-module discovery, lifecycle, schema, and tool seam."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import aiosqlite
import pytest

from app import modules as module_runtime
from app.modules import (
    MODULE_API_VERSION,
    ModuleLoadContext,
    ModuleManager,
    ModuleRuntimeBase,
    ModuleRuntimeContext,
    ModuleSpec,
)
from kimi_agent_module_api import ModuleCapabilities, events as ev
from kimi_agent_module_api.contracts import (
    GuildSettingField,
    GuildSettingsSchema,
    MigrationContext,
    ScopedModuleMigration,
    ModulePermissions,
    ServiceDeclaration,
    ServiceRequirement,
)
from kimi_agent_module_api.testing import (
    FakeDiscordActions,
    FakeEvents,
    FakeHttp,
    FakeInteractions,
    FakeScheduler,
    FakeTrust,
)
from modules.guild_settings import GUILD_MODULES_DIR, GuildSettingsService
from modules.events import EventBusImpl
from modules.testing import fake_ports
from config.settings import Settings
from storage.db import Database
from tools.registry import ToolRegistry


class FakeModule:
    def __init__(
        self,
        name: str,
        events: list[str],
        migrations: tuple[ScopedModuleMigration, ...] = (),
        *,
        fail_start: bool = False,
    ) -> None:
        self.name = name
        self.events = events
        self.scoped_migrations: Sequence[ScopedModuleMigration] = migrations
        self.fail_start = fail_start

    async def start(self, _ctx: ModuleRuntimeContext) -> None:
        self.events.append(f"start:{self.name}")
        if self.fail_start:
            raise RuntimeError(f"failed:{self.name}")

    async def close(self) -> None:
        self.events.append(f"close:{self.name}")


def _settings(tmp_path: Path) -> Settings:
    return Settings(_env_file=None, config_dir=str(tmp_path))  # type: ignore[call-arg]


def _manager(
    tmp_path: Path,
    names: tuple[str, ...],
    installed: dict[str, ModuleSpec],
    registry: ToolRegistry | None = None,
) -> ModuleManager:
    return ModuleManager.load(
        names,
        core_settings=_settings(tmp_path),
        registry=registry or ToolRegistry(),
        installed=installed,
    )


def _spec(
    name: str,
    module: FakeModule,
    *,
    dependencies: tuple[str, ...] = (),
    api_version: int = MODULE_API_VERSION,
) -> ModuleSpec:
    return ModuleSpec(
        name=name,
        version="1.2.3",
        dependencies=dependencies,
        api_version=api_version,
        create=lambda _ctx: module,
    )


def _base(database: Database, manager: ModuleManager) -> ModuleRuntimeBase:
    return ModuleRuntimeBase(
        database=database,
        bot=object(),
        is_guild_active=lambda _guild_id: True,
        current_config_dir=lambda: manager.config_dir,
        capabilities=ModuleCapabilities(
            available=frozenset(), members_intent=False, message_content_intent=False
        ),
        trust=FakeTrust(),
    )


def test_empty_configuration_discovers_nothing(tmp_path: Path) -> None:
    manager = _manager(tmp_path, (), {})

    assert manager.load_state.requested == ()
    assert manager.load_state.loaded == ()
    assert manager.config_dir == tmp_path


def test_missing_activation_capability_soft_disables_module_and_dependents(
    tmp_path: Path,
) -> None:
    created: list[str] = []

    def create(name: str) -> Callable[[ModuleLoadContext], FakeModule]:
        def factory(_ctx: ModuleLoadContext) -> FakeModule:
            created.append(name)
            return FakeModule(name, [])

        return factory

    installed = {
        "index": ModuleSpec(
            name="index",
            version="1",
            create=create("index"),
            activation_capabilities=("discord.message_content.v1",),
        ),
        "consumer": ModuleSpec(
            name="consumer",
            version="1",
            create=create("consumer"),
            dependencies=("index",),
        ),
    }
    settings = Settings(
        _env_file=None,
        config_dir=str(tmp_path),
        message_content_intent=False,
    )  # type: ignore[call-arg]
    manager = ModuleManager.load(
        ("consumer", "index"),
        core_settings=settings,
        registry=ToolRegistry(),
        installed=installed,
    )

    assert manager.load_state.loaded == ()
    assert created == []
    disabled_reasons = {name: reason for name, _version, reason in manager.load_state.disabled}
    assert "missing activation capability discord.message_content.v1" in disabled_reasons["index"]
    assert manager.disabled_modules["consumer"][1] == "dependency disabled: index"


def test_missing_required_capability_remains_a_startup_error(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        config_dir=str(tmp_path),
        message_content_intent=False,
    )  # type: ignore[call-arg]
    spec = ModuleSpec(
        name="index",
        version="1",
        create=lambda _ctx: FakeModule("index", []),
        requires_capabilities=("discord.message_content.v1",),
    )

    with pytest.raises(RuntimeError, match="requires unavailable capability"):
        ModuleManager.load(
            ("index",),
            core_settings=settings,
            registry=ToolRegistry(),
            installed={"index": spec},
        )


def test_discovery_imports_only_configured_entrypoints(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded: list[str] = []

    class FakeEntryPoint:
        def __init__(self, name: str, value: ModuleSpec | Exception) -> None:
            self.name = name
            self.value = value

        def load(self) -> ModuleSpec:
            loaded.append(self.name)
            if isinstance(self.value, Exception):
                raise self.value
            return self.value

    configured = FakeModule("configured", [])
    points = (
        FakeEntryPoint("unused_broken", RuntimeError("must not import")),
        FakeEntryPoint("configured", _spec("configured", configured)),
    )
    monkeypatch.setattr(module_runtime, "entry_points", lambda **_kwargs: points)

    manager = ModuleManager.load(
        ("configured",),
        core_settings=_settings(tmp_path),
        registry=ToolRegistry(),
    )

    assert manager.load_state.loaded == ("configured",)
    assert loaded == ["configured"]


@pytest.mark.parametrize(
    ("names", "installed", "match"),
    [
        (("missing",), {}, "not installed"),
        (
            ("new_api",),
            {
                "new_api": ModuleSpec(
                    name="new_api",
                    version="1",
                    api_version=MODULE_API_VERSION + 1,
                    create=lambda _ctx: FakeModule("new_api", []),
                )
            },
            "requires module API",
        ),
        (
            ("dependent",),
            {
                "dependent": ModuleSpec(
                    name="dependent",
                    version="1",
                    dependencies=("base",),
                    create=lambda _ctx: FakeModule("dependent", []),
                )
            },
            "requires active module",
        ),
    ],
)
def test_invalid_required_module_configuration_fails_startup(
    tmp_path: Path,
    names: tuple[str, ...],
    installed: dict[str, ModuleSpec],
    match: str,
) -> None:
    with pytest.raises(RuntimeError, match=match):
        _manager(tmp_path, names, installed)


def test_service_requirement_must_match_the_provider_declaration(tmp_path: Path) -> None:
    provider = ModuleSpec(
        name="provider",
        version="1",
        provides=(ServiceDeclaration("cases", 1),),
        create=lambda _ctx: FakeModule("provider", []),
    )
    consumer = ModuleSpec(
        name="consumer",
        version="1",
        dependencies=("provider",),
        consumes=(ServiceRequirement("missing", 1, provider="provider"),),
        create=lambda _ctx: FakeModule("consumer", []),
    )
    with pytest.raises(RuntimeError, match="does not declare"):
        _manager(
            tmp_path,
            ("consumer", "provider"),
            {"provider": provider, "consumer": consumer},
        )


@pytest.mark.asyncio
async def test_invalid_disable_module_settings_gate_the_runtime_context(tmp_path: Path) -> None:
    guild_id = 123
    seen: list[ModuleRuntimeContext] = []

    class CaptureModule(FakeModule):
        async def start(self, ctx: ModuleRuntimeContext) -> None:
            seen.append(ctx)

    module = CaptureModule("optional", [])
    schema = GuildSettingsSchema(
        fields=(GuildSettingField("channels", "id_list"),),
        invalid_policy="disable_module",
    )
    spec = ModuleSpec(
        name="optional",
        version="1",
        guild_settings=schema,
        permissions=ModulePermissions(event_topics=(ev.TOPIC_MESSAGE,)),
        create=lambda _ctx: module,
    )
    manager = _manager(tmp_path, ("optional",), {"optional": spec})
    manager.events = EventBusImpl()
    settings_path = tmp_path / GUILD_MODULES_DIR / str(guild_id) / "optional.md"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text("---\nchannels: [invalid]\n---\n", encoding="utf-8")
    manager.guild_settings = GuildSettingsService(
        config_dir=lambda: tmp_path,
        schemas=manager.guild_settings_schemas,
    )
    manager.guild_settings.refresh((guild_id,))
    database = Database(tmp_path / "bot.db")
    await database.connect()
    try:
        await manager.start(_base(database, manager), customize=fake_ports)
        assert seen[0].is_guild_active(guild_id) is False
        delivered: list[str] = []

        async def on_message(event: Any) -> None:
            delivered.append(event.topic)

        seen[0].events.subscribe(ev.TOPIC_MESSAGE, on_message)
        assert manager.events is not None
        payload = SimpleNamespace(message=SimpleNamespace(ref=SimpleNamespace(guild_id=guild_id)))
        manager.events.publish_core(ev.TOPIC_MESSAGE, payload)
        await manager.events.drain()
        assert delivered == []
        settings_path.write_text("---\nchannels: []\n---\n", encoding="utf-8")
        manager.guild_settings.refresh((guild_id,))
        assert seen[0].is_guild_active(guild_id) is True
        manager.events.publish_core(ev.TOPIC_MESSAGE, payload)
        await manager.events.drain()
        assert delivered == [ev.TOPIC_MESSAGE]
    finally:
        await manager.close()
        await database.close()


def test_dependencies_compose_in_order_and_can_register_llm_tools(tmp_path: Path) -> None:
    events: list[str] = []
    registry = ToolRegistry()
    base = FakeModule("base", events)
    dependent = FakeModule("dependent", events)

    def create_base(ctx: ModuleLoadContext) -> FakeModule:
        async def handler(_arguments: dict[str, Any], _ctx: Any) -> str:
            return "from module"

        ctx.registry.register("module_demo", "demo", {}, handler)
        return base

    installed = {
        "base": ModuleSpec(name="base", version="1", create=create_base),
        "dependent": _spec("dependent", dependent, dependencies=("base",)),
    }

    manager = _manager(tmp_path, ("dependent", "base"), installed, registry)

    assert manager.load_state.loaded == ("base", "dependent")
    assert registry.is_registered("module_demo")
    assert manager.spec("base").name == "base"


@pytest.mark.asyncio
async def test_module_schema_lifecycle_and_reverse_close(tmp_path: Path) -> None:
    events: list[str] = []

    async def create_demo_table(ctx: MigrationContext) -> None:
        events.append("migrate:base")
        await ctx.connection.execute("CREATE TABLE demo_module_data (id INTEGER PRIMARY KEY)")

    async def create_dependent_table(ctx: MigrationContext) -> None:
        events.append("migrate:dependent")
        await ctx.connection.execute("CREATE TABLE dependent_module_data (id INTEGER PRIMARY KEY)")

    base = FakeModule("base", events, (("initial", create_demo_table),))
    dependent = FakeModule("dependent", events, (("initial", create_dependent_table),))
    installed = {
        "base": _spec("base", base),
        "dependent": _spec("dependent", dependent, dependencies=("base",)),
    }
    manager = _manager(tmp_path, ("dependent", "base"), installed)
    database = Database(str(tmp_path / "bot.db"))
    await database.connect()

    await manager.start(_base(database, manager), customize=fake_ports)
    await manager.close()

    cursor = await database.conn.execute(
        "SELECT version FROM module_schema_versions WHERE module_name = 'base'"
    )
    assert [int(row[0]) for row in await cursor.fetchall()] == [1]
    assert events == [
        "migrate:base",
        "migrate:dependent",
        "start:base",
        "start:dependent",
        "close:dependent",
        "close:base",
    ]
    await database.close()


@pytest.mark.asyncio
async def test_existing_core_v1_database_gains_module_ledger(tmp_path: Path) -> None:
    path = tmp_path / "old-v1.db"
    raw = await aiosqlite.connect(path)
    await raw.execute(
        "CREATE TABLE schema_version (version INTEGER PRIMARY KEY, name TEXT, applied_at TEXT)"
    )
    await raw.execute("INSERT INTO schema_version VALUES (1, 'core_v1', 'now')")
    await raw.execute("CREATE TABLE legacy_core_data (id INTEGER PRIMARY KEY)")
    await raw.execute("CREATE TABLE coding_tasks (id TEXT PRIMARY KEY)")
    await raw.commit()
    await raw.close()

    database = Database(path)
    await database.connect()
    cursor = await database.conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'module_schema_versions'"
    )
    assert await cursor.fetchone() is not None
    await database.close()


@pytest.mark.asyncio
async def test_start_failure_closes_already_started_modules(tmp_path: Path) -> None:
    events: list[str] = []
    base = FakeModule("base", events)
    failing = FakeModule("failing", events, fail_start=True)
    manager = _manager(
        tmp_path,
        ("base", "failing"),
        {"base": _spec("base", base), "failing": _spec("failing", failing)},
    )
    database = Database(str(tmp_path / "bot.db"))
    await database.connect()

    with pytest.raises(RuntimeError, match="failed:failing"):
        await manager.start(_base(database, manager), customize=fake_ports)

    assert events == ["start:base", "start:failing", "close:failing", "close:base"]
    await database.close()


class SlowModule(FakeModule):
    def __init__(self, name: str, events: list[str], *, slow_start: bool, slow_close: bool) -> None:
        super().__init__(name, events)
        self.slow_start = slow_start
        self.slow_close = slow_close

    async def start(self, ctx: ModuleRuntimeContext) -> None:
        await super().start(ctx)
        if self.slow_start:
            await asyncio.sleep(10)

    async def close(self) -> None:
        if self.slow_close:
            await asyncio.sleep(10)
        await super().close()


@pytest.mark.asyncio
async def test_start_timeout_fails_the_module_and_aborts(tmp_path: Path) -> None:
    events: list[str] = []
    slow = SlowModule("slow", events, slow_start=True, slow_close=False)
    manager = _manager(tmp_path, ("slow",), {"slow": _spec("slow", slow)})
    manager.start_timeout_seconds = 0.05
    database = Database(str(tmp_path / "bot.db"))
    await database.connect()

    with pytest.raises(RuntimeError, match="start\\(\\) exceeded 0.05s"):
        await manager.start(_base(database, manager), customize=fake_ports)

    assert events == ["start:slow", "close:slow"]
    await database.close()


@pytest.mark.asyncio
async def test_close_timeout_is_logged_and_shutdown_continues(tmp_path: Path) -> None:
    events: list[str] = []
    stuck = SlowModule("stuck", events, slow_start=False, slow_close=True)
    other = FakeModule("other", events)
    manager = _manager(
        tmp_path,
        ("stuck", "other"),
        {"stuck": _spec("stuck", stuck), "other": _spec("other", other)},
    )
    manager.close_timeout_seconds = 0.05
    database = Database(str(tmp_path / "bot.db"))
    await database.connect()
    await manager.start(_base(database, manager), customize=fake_ports)

    await manager.close()

    # "stuck" never appended close (it was cancelled); "other" still closed.
    assert events == ["start:stuck", "start:other", "close:other"]
    assert manager.health_snapshot() == {}
    await database.close()


def test_tool_registry_is_sealed_after_loading(tmp_path: Path) -> None:
    captured: list[ModuleLoadContext] = []

    def create(ctx: ModuleLoadContext) -> FakeModule:
        captured.append(ctx)
        ctx.registry.register("early", "ok", {"type": "object"}, _noop_tool)
        return FakeModule("m", [])

    registry = ToolRegistry()
    _manager(tmp_path, ("m",), {"m": ModuleSpec("m", "1.0", create)}, registry)

    assert registry.is_registered("early")
    with pytest.raises(RuntimeError, match="not start\\(\\)"):
        captured[0].registry.register("late", "no", {"type": "object"}, _noop_tool)
    assert not registry.is_registered("late")


async def _noop_tool(_arguments: dict[str, Any], _ctx: Any) -> str:
    return ""


@pytest.mark.asyncio
async def test_base_factories_build_per_module_ports(tmp_path: Path) -> None:
    seen: list[tuple[str, str]] = []

    def discord_actions(spec: ModuleSpec, _active: Callable[[int], bool]) -> Any:
        seen.append(("discord", spec.name))
        return FakeDiscordActions(spec.name)

    def interactions(name: str, _active: Callable[[int], bool]) -> Any:
        seen.append(("interactions", name))
        return FakeInteractions(name)

    module = FakeModule("m", [])
    manager = _manager(tmp_path, ("m",), {"m": _spec("m", module)})
    database = Database(str(tmp_path / "bot.db"))
    await database.connect()
    base = ModuleRuntimeBase(
        database=database,
        bot=object(),
        is_guild_active=lambda _guild_id: True,
        current_config_dir=lambda: manager.config_dir,
        capabilities=ModuleCapabilities(frozenset(), False, False),
        trust=FakeTrust(),
        discord_actions=discord_actions,
        interactions=interactions,
    )

    def only_bus(spec: ModuleSpec, ports: dict[str, Any]) -> dict[str, Any]:
        return {
            **ports,
            "events": FakeEvents(spec.name),
            "scheduler": FakeScheduler(),
            "http": FakeHttp(),
        }

    await manager.start(base, customize=only_bus)
    try:
        ctx = manager.context_for("m")
        assert isinstance(ctx.interactions, FakeInteractions)
        assert sorted(seen) == [("discord", "m"), ("interactions", "m")]
    finally:
        await manager.close()
        await database.close()


class StubbornModule(FakeModule):
    """Swallows cancellation: the ceiling must still hold."""

    async def start(self, ctx: ModuleRuntimeContext) -> None:
        self.events.append(f"start:{self.name}")
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            # Ignore the first cancellation (the host's), so the grace period
            # elapses; yield to the loop's own teardown after that.
            await asyncio.sleep(10)


@pytest.mark.asyncio
async def test_start_timeout_abandons_a_module_that_ignores_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(module_runtime, "run_bounded", _fast_grace(module_runtime.run_bounded))
    events: list[str] = []
    stubborn = StubbornModule("stubborn", events)
    manager = _manager(tmp_path, ("stubborn",), {"stubborn": _spec("stubborn", stubborn)})
    manager.start_timeout_seconds = 0.05
    database = Database(str(tmp_path / "bot.db"))
    await database.connect()

    with pytest.raises(RuntimeError, match="exceeded 0.05s"):
        await asyncio.wait_for(
            manager.start(_base(database, manager), customize=fake_ports), timeout=2
        )
    await database.close()


def _fast_grace(original: Any) -> Any:
    async def run(coro: Any, *, timeout: float, grace: float = 0.05, what: str) -> Any:
        return await original(coro, timeout=timeout, grace=0.05, what=what)

    return run


@pytest.mark.asyncio
async def test_a_module_raising_timeout_error_is_not_a_deadline(tmp_path: Path) -> None:
    class Impatient(FakeModule):
        async def start(self, ctx: ModuleRuntimeContext) -> None:
            raise TimeoutError("upstream took too long")

    manager = _manager(tmp_path, ("m",), {"m": _spec("m", Impatient("m", []))})
    database = Database(str(tmp_path / "bot.db"))
    await database.connect()

    with pytest.raises(TimeoutError, match="upstream"):
        await manager.start(_base(database, manager), customize=fake_ports)
    await database.close()


def test_tool_registry_is_sealed_even_when_a_later_create_fails(tmp_path: Path) -> None:
    captured: list[ModuleLoadContext] = []

    def first(ctx: ModuleLoadContext) -> FakeModule:
        captured.append(ctx)
        return FakeModule("first", [])

    def second(_ctx: ModuleLoadContext) -> FakeModule:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        _manager(
            tmp_path,
            ("first", "second"),
            {"first": ModuleSpec("first", "1", first), "second": ModuleSpec("second", "1", second)},
        )

    with pytest.raises(RuntimeError, match="after module loading"):
        captured[0].registry.register("late", "no", {"type": "object"}, _noop_tool)
