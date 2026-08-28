"""Stable public contracts for trusted, installed assistant modules."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, TypeVar

from pydantic_settings import BaseSettings

from kimi_agent_module_api.contracts import (
    ConfigSnapshot,
    DiscordActions,
    EventBus,
    GuildSettings,
    GuildSettingsSchema,
    HealthReporter,
    InteractionRouter,
    ModuleHttp,
    ModulePermissions,
    ModuleStorage,
    ScopedModuleMigration,
    ProposalActor,
    ProposalError,
    ProposalRef,
    ProposalService,
    ProposalState,
    Scheduler,
    ServiceDeclaration,
    ServiceRegistry,
    ServiceRequirement,
    TrustLookup,
)
from kimi_agent_module_api.settings import ModuleSetting, ModuleSettingsDefinition
from kimi_agent_module_api.tools import (
    ModuleToolContext,
    ModuleToolHandler,
    ModuleToolRegistry,
)
from kimi_agent_module_api.trust import TrustTier

MODULE_API_VERSION = 1
MODULE_ENTRYPOINT_GROUP = "kimi_agent.modules"

_SettingsT = TypeVar("_SettingsT", bound=BaseSettings)


@dataclass(frozen=True)
class ModuleCapabilities:
    available: frozenset[str]
    members_intent: bool
    message_content_intent: bool

    def require(self, name: str) -> None:
        if name not in self.available:
            raise RuntimeError(f"the host does not provide required capability {name!r}")


class AppModule(Protocol):
    scoped_migrations: Sequence[ScopedModuleMigration]

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
    permissions: ModulePermissions = field(default_factory=ModulePermissions)
    guild_settings: GuildSettingsSchema | None = None
    provides: tuple[ServiceDeclaration, ...] = ()
    consumes: tuple[ServiceRequirement, ...] = ()
    table_aliases: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ModuleLoadContext:
    capabilities: ModuleCapabilities
    registry: ModuleToolRegistry
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
    """Runtime ports supplied to one module after it has been loaded."""

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
    "ModulePermissions",
    "ModuleRuntimeContext",
    "ModuleSetting",
    "ModuleSettingsDefinition",
    "ModuleSpec",
    "ModuleToolContext",
    "ModuleToolHandler",
    "ModuleToolRegistry",
    "ProposalActor",
    "ProposalError",
    "ProposalRef",
    "ProposalService",
    "ProposalState",
    "ScopedModuleMigration",
    "ServiceDeclaration",
    "ServiceRequirement",
    "TrustTier",
]
