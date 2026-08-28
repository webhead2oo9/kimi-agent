"""Test scaffolding that needs nothing from the host.

``kimi_agent_module_api.testing`` ships a fake for every runtime port except
real SQL, because the API package has no database dependency. This conftest
adds that one piece (``MemoryStorage`` over an in-memory aiosqlite connection)
and a ``started_module`` fixture that assembles a full ``ModuleRuntimeContext``
from the public fakes, applies the module's migrations, and starts it.

Everything here is what a third-party module would write for itself; none of
it imports the bot.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiosqlite
import pytest
import pytest_asyncio
from kimi_agent_module_api import (
    ModuleCapabilities,
    ModuleLoadContext,
    ModuleRuntimeContext,
    ModuleToolRegistry,
    TrustTier,
)
from kimi_agent_module_api.contracts import MigrationContext, ModuleContractError
from kimi_agent_module_api.testing import (
    FakeDiscordActions,
    FakeEvents,
    FakeGuildSettings,
    FakeHealth,
    FakeHttp,
    FakeInteractions,
    FakeProposals,
    FakeScheduler,
    FakeServiceRegistry,
    FakeTrust,
)

from community_agent_reference_module.module import MODULE_NAME, KudosModule
from community_agent_reference_module.settings import KudosSettings
from community_agent_reference_module.spec import SPEC, create

GUILD = 100
STAFF = 1
ALICE = 2
BOB = 3

# The module's tests own the clock so the 24-hour window is deterministic.
T0 = 1_000_000.0


class Clock:
    def __init__(self, now: float = T0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class MemoryStorage:
    """``ModuleStorage`` over one in-memory SQLite connection.

    The naming rule mirrors the host: ``table("kudos")`` is ``"<module>_kudos"``.
    ``write_transaction`` commits on success and rolls back on error, which is
    the only property module code should rely on.
    """

    def __init__(self, connection: aiosqlite.Connection, module_name: str) -> None:
        self._connection = connection
        self._prefix = module_name.replace("-", "_")

    @property
    def connection(self) -> aiosqlite.Connection:
        return self._connection

    def table(self, name: str) -> str:
        if not name.isidentifier():
            raise ModuleContractError(f"bad table name {name!r}")
        return f'"{self._prefix}_{name}"'

    @contextlib.asynccontextmanager
    async def _transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        try:
            yield self._connection
        except BaseException:
            await self._connection.rollback()
            raise
        await self._connection.commit()

    def write_transaction(self) -> Any:
        return self._transaction()


@dataclass(slots=True)
class RecordedTool:
    description: str
    parameters: dict[str, Any]
    handler: Any
    min_tier: TrustTier
    searchable: bool


class RecordingRegistry:
    """Satisfies ``ModuleToolRegistry`` and remembers what was registered."""

    def __init__(self) -> None:
        self.tools: dict[str, RecordedTool] = {}

    def register(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        handler: Any,
        min_tier: TrustTier = TrustTier.MEMBER,
        searchable: bool = False,
        *,
        owner_only: bool = False,
        guild_ids: frozenset[str] | None = None,
    ) -> None:
        self.tools[name] = RecordedTool(description, parameters, handler, min_tier, searchable)


@dataclass(slots=True)
class ToolContext:
    """The attributes of ``ModuleToolContext`` a handler may read."""

    user_id: str
    user_name: str = "tester"
    guild_id: str | None = str(GUILD)
    channel_id: str = "555"
    thread_id: str | None = None
    trust_tier: TrustTier = TrustTier.MEMBER
    tool_configs: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass(slots=True)
class Harness:
    """Everything a test needs to poke the started module and inspect the fakes."""

    module: KudosModule
    ctx: ModuleRuntimeContext
    registry: RecordingRegistry
    labels: dict[str, str]
    clock: Clock
    events: FakeEvents
    scheduler: FakeScheduler
    discord: FakeDiscordActions
    interactions: FakeInteractions
    guild_settings: FakeGuildSettings
    health: FakeHealth
    services: FakeServiceRegistry
    trust: FakeTrust
    proposals: FakeProposals

    async def tool(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> str:
        result = await self.registry.tools[name].handler(arguments, ctx)
        assert isinstance(result, str)
        return result


def load_context(
    registry: ModuleToolRegistry, settings: KudosSettings, labels: dict[str, str]
) -> ModuleLoadContext:
    return ModuleLoadContext(
        capabilities=ModuleCapabilities(frozenset({"proposals.v2"}), False, False),
        registry=registry,
        module_settings=settings,
        _register_tool_labels=labels.update,
        _declare_surface_tools=lambda _surface, _names: None,
    )


@pytest_asyncio.fixture
async def connection() -> AsyncIterator[aiosqlite.Connection]:
    async with aiosqlite.connect(":memory:") as conn:
        yield conn


@pytest_asyncio.fixture
async def started(connection: aiosqlite.Connection, tmp_path: Path) -> AsyncIterator[Harness]:
    """A started module with a modest daily limit, one guild, and a digest channel."""
    settings = KudosSettings(daily_limit=2, board_size=3, digest_interval_seconds=7 * 86_400)
    registry = RecordingRegistry()
    labels: dict[str, str] = {}
    clock = Clock()

    # ``create`` returns the AppModule; re-wrap so tests can inject the clock.
    created = create(load_context(registry, settings, labels))
    assert isinstance(created, KudosModule)
    module = KudosModule(settings, clock=clock)
    # Re-point the recorded handlers at the clock-controlled instance.
    for recorded in registry.tools.values():
        recorded.handler = getattr(module, recorded.handler.__name__)

    storage = MemoryStorage(connection, MODULE_NAME)
    for _name, migration in module.scoped_migrations:
        await migration(MigrationContext(connection=connection, table=storage.table))
    await connection.commit()

    events = FakeEvents(MODULE_NAME)
    scheduler = FakeScheduler()
    discord = FakeDiscordActions(MODULE_NAME, SPEC.permissions.discord_actions)
    interactions = FakeInteractions(MODULE_NAME)
    guild_settings = FakeGuildSettings({GUILD: {"digest_channel_id": 900}})
    health = FakeHealth()
    services = FakeServiceRegistry()
    trust = FakeTrust({(GUILD, STAFF): "staff"})
    proposals = FakeProposals(MODULE_NAME)
    ctx = ModuleRuntimeContext(
        module_name=MODULE_NAME,
        is_guild_active=lambda _guild_id: True,
        current_config_dir=lambda: tmp_path,
        capabilities=ModuleCapabilities(frozenset({"proposals.v2"}), False, False),
        events=events,
        scheduler=scheduler,
        storage=storage,
        health=health,
        discord=discord,
        interactions=interactions,
        http=FakeHttp(),
        services=services,
        trust=trust,
        guild_settings=guild_settings,
        proposals=proposals,
    )
    await module.start(ctx)
    try:
        yield Harness(
            module,
            ctx,
            registry,
            labels,
            clock,
            events,
            scheduler,
            discord,
            interactions,
            guild_settings,
            health,
            services,
            trust,
            proposals,
        )
    finally:
        await module.close()


@pytest.fixture
def member_ctx() -> ToolContext:
    return ToolContext(user_id=str(ALICE))
