"""Stable public contracts for trusted Kimi application modules.

Modules run in-process and are trusted by installation. These contracts are a
compatibility boundary, not a sandbox: core owns the implementations while
external packages depend only on the shapes exported here.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, TypeVar

from pydantic_settings import BaseSettings

from kimi_agent_module_api.contracts import (
    ConfigSnapshot,
    DiscordActions,
    EventBus,
    GuildSettings,
    HealthReporter,
    InteractionRouter,
    ModuleHttp,
    ModuleStorage,
    Scheduler,
    ServiceRegistry,
    TrustLookup,
    GuildSettingsSchema,
    ModulePermissions,
    ProposalActor,
    ProposalError,
    ProposalRef,
    ProposalService,
    ProposalState,
    ServiceDeclaration,
    ServiceRequirement,
)
from config.module_settings import ModuleSetting, ModuleSettingsDefinition
from trust.tiers import TrustTier

if TYPE_CHECKING:
    import aiosqlite

    from tools.registry import ToolRegistry

MODULE_API_VERSION = 2
MODULE_ENTRYPOINT_GROUP = "kimi_agent.modules"

type ModuleMigration = tuple[str, Callable[["aiosqlite.Connection"], Awaitable[None]]]
_SettingsT = TypeVar("_SettingsT", bound=BaseSettings)


@dataclass(frozen=True)
class ModuleCapabilities:
    available: frozenset[str]
    members_intent: bool
    message_content_intent: bool

    def require(self, name: str) -> None:
        if name not in self.available:
            raise RuntimeError(f"Kimi core does not provide required capability {name!r}")


class AppModule(Protocol):
    # Raw-connection migrations. A module that also defines
    # ``scoped_migrations: Sequence[ScopedModuleMigration]`` gets those run
    # instead, with a MigrationContext whose ``table()`` applies the prefix.
    migrations: Sequence[ModuleMigration]

    async def start(self, ctx: ModuleRuntimeContext) -> None: ...

    async def close(self) -> None: ...


@dataclass(frozen=True)
class ModuleSpec:
    name: str
    version: str
    create: Callable[[ModuleLoadContext], AppModule]
    api_version: int = MODULE_API_VERSION
    dependencies: tuple[str, ...] = ()
    settings: ModuleSettingsDefinition | None = None
    requires_capabilities: tuple[str, ...] = ()
    activation_capabilities: tuple[str, ...] = ()
    # Declarations. Validated at selection preflight; enforced by the
    # runtime services as they land. Defaults declare nothing.
    permissions: ModulePermissions = field(default_factory=ModulePermissions)
    guild_settings: GuildSettingsSchema | None = None
    provides: tuple[ServiceDeclaration, ...] = ()
    consumes: tuple[ServiceRequirement, ...] = ()
    # Logical table name -> legacy physical name, so an installation keeps
    # its data until a later release renames tables to the module prefix.
    table_aliases: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ModuleLoadContext:
    capabilities: ModuleCapabilities
    registry: ToolRegistry
    module_settings: BaseSettings | None
    _register_tool_labels: Callable[[Mapping[str, str]], None]
    _declare_surface_tools: Callable[[str, Sequence[str]], None]

    def settings_for(self, settings_type: type[_SettingsT]) -> _SettingsT:
        if self.module_settings is None or not isinstance(self.module_settings, settings_type):
            raise TypeError(f"prepared module settings are not {settings_type.__name__}")
        return self.module_settings

    def register_tool_labels(self, labels: Mapping[str, str]) -> None:
        self._register_tool_labels(labels)

    def declare_surface_tools(self, surface: str, names: Sequence[str]) -> None:
        self._declare_surface_tools(surface, names)


@dataclass(frozen=True)
class ModuleRuntimeContext:
    """What one module receives in ``start()``: services, never raw core objects.

    ``raw_bot`` and ``raw_storage`` are populated only for modules whose
    permissions declare them. They are audited escape hatches for trusted,
    owner-installed code, not a security boundary.
    """

    module_name: str
    is_guild_active: Callable[[int], bool]
    current_config_dir: Callable[[], Path]
    capabilities: ModuleCapabilities
    events: EventBus
    scheduler: Scheduler
    storage: ModuleStorage
    health: HealthReporter
    discord: DiscordActions
    interactions: InteractionRouter
    http: ModuleHttp
    services: ServiceRegistry
    trust: TrustLookup
    guild_settings: GuildSettings | None = None
    proposals: ProposalService | None = None
    raw_bot: Any = None
    raw_storage: Any = None


__all__ = [
    "MODULE_API_VERSION",
    "MODULE_ENTRYPOINT_GROUP",
    "AppModule",
    "ConfigSnapshot",
    "GuildSettingsSchema",
    "ModuleCapabilities",
    "ModuleLoadContext",
    "ModuleMigration",
    "ModulePermissions",
    "ModuleRuntimeContext",
    "ModuleSetting",
    "ModuleSettingsDefinition",
    "ModuleSpec",
    "ProposalActor",
    "ProposalError",
    "ProposalRef",
    "ProposalService",
    "ProposalState",
    "ServiceDeclaration",
    "ServiceRequirement",
    "TrustTier",
]
