"""Test scaffolding that needs nothing from the host.

``kimi_agent_module_api.testing`` ships a fake for every runtime port,
including ``MemoryStorage`` (real SQL over in-memory SQLite, via the
``testing`` extra). The ``started`` fixture assembles a full
``ModuleRuntimeContext`` from those fakes, applies the module's migrations,
and starts it. None of this imports the bot.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from kimi_agent_module_api import (
    ModuleCapabilities,
    ModuleRuntimeContext,
    ModuleToolContext,
    TrustTier,
)
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
    MemoryStorage,
    RecordingToolRegistry,
    load_context,
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


@dataclass(slots=True)
class ToolContext:
    """A ``ModuleToolContext`` with test defaults."""

    user_id: int
    user_name: str = "tester"
    guild_id: int | None = GUILD
    channel_id: int = 555
    thread_id: int | None = None
    trust_tier: TrustTier = TrustTier.MEMBER
    tool_configs: dict[str, dict[str, Any]] = field(default_factory=dict)

    def sdk(self) -> ModuleToolContext:
        return ModuleToolContext(
            self.user_id,
            self.user_name,
            self.guild_id,
            self.channel_id,
            self.thread_id,
            self.trust_tier,
            self.tool_configs,
        )


@dataclass(slots=True)
class Harness:
    """Everything a test needs to poke the started module and inspect the fakes."""

    module: KudosModule
    ctx: ModuleRuntimeContext
    registry: RecordingToolRegistry
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
        result = await self.registry.tools[name].handler(arguments, ctx.sdk())
        assert isinstance(result, str)
        return result


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[MemoryStorage]:
    async with MemoryStorage.open(MODULE_NAME) as memory:
        yield memory


@pytest_asyncio.fixture
async def started(storage: MemoryStorage, tmp_path: Path) -> AsyncIterator[Harness]:
    """A started module with a modest daily limit, one guild, and a digest channel."""
    settings = KudosSettings(daily_limit=2, board_size=3, digest_interval_seconds=7 * 86_400)
    clock = Clock()

    # ``create`` returns the AppModule; re-wrap so tests can inject the clock.
    context, recorder = load_context(settings)
    created = create(context)
    assert isinstance(created, KudosModule)
    module = KudosModule(settings, clock=clock)
    registry = recorder.registry
    # Re-point the recorded handlers at the clock-controlled instance.
    for recorded in registry.tools.values():
        recorded.handler = getattr(module, recorded.handler.__name__)
    labels = recorder.labels

    await storage.migrate(module.scoped_migrations)

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
    return ToolContext(user_id=ALICE)
