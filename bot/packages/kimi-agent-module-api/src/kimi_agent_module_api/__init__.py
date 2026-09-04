"""Stable public contracts for trusted, installed assistant modules."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import KW_ONLY, dataclass, field
from pathlib import Path
from typing import Any, Protocol, TypeVar

from pydantic_settings import BaseSettings

from kimi_agent_module_api import contracts as _contracts
from kimi_agent_module_api.contracts import (
    ConfigSnapshot,
    RoleSnapshot,
    render_guild_settings,
    GuildSettingsSchema,
    InviteSnapshot,
    LayoutGallery,
    LayoutItem,
    LayoutSection,
    LayoutSeparator,
    LayoutSeparatorSpacing,
    LayoutText,
    ModalSpec,
    ModulePermissions,
    OutgoingLayout,
    ScopedModuleMigration,
    ProposalActor,
    ProposalError,
    ProposalRef,
    ProposalService,
    ProposalState,
    ServiceDeclaration,
    ServiceRequirement,
    TextInputSpec,
    TextInputStyle,
)
from kimi_agent_module_api.settings import ModuleSetting, ModuleSettingsDefinition
from kimi_agent_module_api.tools import (
    ModuleToolContext,
    ModuleToolHandler,
    ModuleToolRegistry,
)
from kimi_agent_module_api.trust import TrustTier

MODULE_API_VERSION = 2
MODULE_ENTRYPOINT_GROUP = "kimi_agent.modules"
# Capabilities every compatible host advertises regardless of configuration.
BASELINE_CAPABILITIES: frozenset[str] = frozenset({"discord.history.v1", "proposals.v2"})

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
    """A module declaration with an explicit, source-pinned host API version."""

    name: str
    version: str
    create: Callable[[ModuleLoadContext], AppModule]
    _: KW_ONLY
    api_version: int
    dependencies: tuple[str, ...] = ()
    settings: ModuleSettingsDefinition | None = None
    requires_capabilities: tuple[str, ...] = ()
    activation_capabilities: tuple[str, ...] = ()
    permissions: ModulePermissions = field(default_factory=ModulePermissions)
    guild_settings: GuildSettingsSchema | None = None
    provides: tuple[ServiceDeclaration, ...] = ()
    consumes: tuple[ServiceRequirement, ...] = ()


@dataclass(frozen=True)
class ModuleLoadContext:
    """What ``ModuleSpec.create`` receives.

    ``create()`` is pure wiring: read prepared settings, register LLM tools,
    construct the module object. No migration has run and no dependency has
    started, so nothing here reaches storage, services, or Discord; that work
    belongs in ``start()``. The tool registry is sealed once loading finishes,
    so tools cannot be registered later from ``start()`` either.
    """

    capabilities: ModuleCapabilities
    registry: ModuleToolRegistry
    module_settings: BaseSettings | None
    # Host sinks behind the two convenience methods below. Tests build a
    # context with ``kimi_agent_module_api.testing.load_context``.
    label_sink: Callable[[Mapping[str, str]], None]
    surface_sink: Callable[[str, Sequence[str]], None]

    def settings_for(self, settings_type: type[_SettingsT]) -> _SettingsT:
        if self.module_settings is None or not isinstance(self.module_settings, settings_type):
            raise TypeError(f"prepared module settings are not {settings_type.__name__}")
        return self.module_settings

    def register_tool_labels(self, labels: Mapping[str, str]) -> None:
        """Gerund phrases shown while a tool runs, e.g. ``{"give_kudos": "Giving kudos"}``."""
        self.label_sink(labels)

    def declare_surface_tools(self, surface: str, names: Sequence[str]) -> None:
        """Declare which tools belong to a named evaluation surface."""
        self.surface_sink(surface, names)


@dataclass(frozen=True)
class ModuleRuntimeContext:
    """Runtime ports supplied to one module after it has been loaded."""

    module_name: str
    is_guild_active: Callable[[int], bool]
    current_config_dir: Callable[[], Path]
    capabilities: ModuleCapabilities
    events: _contracts.EventBus
    scheduler: _contracts.Scheduler
    storage: _contracts.ModuleStorage
    health: _contracts.HealthReporter
    discord: _contracts.DiscordActions
    interactions: _contracts.InteractionRouter
    http: _contracts.ModuleHttp
    services: _contracts.ServiceRegistry
    trust: _contracts.TrustLookup
    guild_settings: _contracts.GuildSettings | None = None
    proposals: ProposalService | None = None
    raw_bot: Any = None
    raw_storage: Any = None


__all__ = [
    "BASELINE_CAPABILITIES",
    "MODULE_API_VERSION",
    "MODULE_ENTRYPOINT_GROUP",
    "AppModule",
    "ConfigSnapshot",
    "GuildSettingsSchema",
    "InviteSnapshot",
    "LayoutGallery",
    "LayoutItem",
    "LayoutSection",
    "LayoutSeparator",
    "LayoutSeparatorSpacing",
    "LayoutText",
    "ModalSpec",
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
    "OutgoingLayout",
    "ProposalActor",
    "ProposalError",
    "ProposalRef",
    "ProposalService",
    "ProposalState",
    "RoleSnapshot",
    "ScopedModuleMigration",
    "ServiceDeclaration",
    "ServiceRequirement",
    "TextInputSpec",
    "TextInputStyle",
    "TrustTier",
    "render_guild_settings",
]
