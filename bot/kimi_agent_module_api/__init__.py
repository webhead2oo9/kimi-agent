"""Stable public contracts for trusted Kimi application modules.

Modules run in-process and are trusted by installation. These contracts are a
compatibility boundary, not a sandbox: core owns the implementations while
external packages depend only on the shapes exported here.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypeVar

from pydantic_settings import BaseSettings

from kimi_agent_module_api.contracts import (
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
    ServiceDeclaration,
    ServiceRequirement,
)
from config.module_settings import ModuleSetting, ModuleSettingsDefinition
from trust.tiers import TrustTier

if TYPE_CHECKING:
    import aiosqlite

    from tools.registry import ToolRegistry

MODULE_API_VERSION = 1
MODULE_ENTRYPOINT_GROUP = "kimi_agent.modules"

type ProposalState = Literal[
    "pending",
    "rejected",
    "stale",
    "applying",
    "restart_pending",
    "applied",
    "failed",
    "rolled_back",
]
type ActivationMode = Literal["live", "restart", "maintenance"]
type ModuleMigration = tuple[str, Callable[["aiosqlite.Connection"], Awaitable[None]]]
_SettingsT = TypeVar("_SettingsT", bound=BaseSettings)


class ProposalError(RuntimeError):
    """Base error raised by the durable proposal service."""


class ProposalNotFound(ProposalError):
    pass


class ProposalNotPending(ProposalError):
    pass


class ProposalStale(ProposalError):
    pass


@dataclass(frozen=True)
class ModuleCapabilities:
    available: frozenset[str]
    members_intent: bool
    message_content_intent: bool

    def require(self, name: str) -> None:
        if name not in self.available:
            raise RuntimeError(f"Kimi core does not provide required capability {name!r}")


@dataclass(frozen=True)
class ProposalActor:
    user_id: str
    source: str
    guild_id: str | None = None
    channel_id: str | None = None


@dataclass(frozen=True)
class ProposalDraft:
    action: str
    target: str
    summary: str
    changes: Mapping[str, Any]
    actor: ProposalActor
    expected_revision: str | None = None


@dataclass(frozen=True)
class ProposalPreview:
    revision: str
    redacted_changes: Mapping[str, Any]
    activation: ActivationMode = "live"
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProposalApplyResult:
    activation: ActivationMode
    revision: str
    message: str


@dataclass(frozen=True)
class ProposalRecord:
    proposal_id: str
    module_name: str
    action: str
    target: str
    summary: str
    changes: Mapping[str, Any]
    actor: ProposalActor
    expected_revision: str | None
    preview: ProposalPreview
    state: ProposalState
    created_at: float
    updated_at: float
    decided_by: str | None = None
    decision_reason: str = ""
    result_message: str = ""


class ProposalActionHandler(Protocol):
    async def preview(self, draft: ProposalDraft) -> ProposalPreview: ...

    async def apply(self, proposal: ProposalRecord) -> ProposalApplyResult: ...


class ProposalService(Protocol):
    def register_handler(
        self, module_name: str, action: str, handler: ProposalActionHandler
    ) -> None: ...

    def unregister_module(self, module_name: str) -> None: ...

    async def create(self, module_name: str, draft: ProposalDraft) -> ProposalRecord: ...

    async def get(self, proposal_id: str) -> ProposalRecord | None: ...

    async def list(self, *, state: ProposalState | None = None) -> list[ProposalRecord]: ...

    async def approve(self, proposal_id: str, *, owner_user_id: str) -> ProposalRecord: ...

    async def reject(
        self, proposal_id: str, *, owner_user_id: str, reason: str = ""
    ) -> ProposalRecord: ...


@dataclass(frozen=True)
class ConfigSnapshot:
    target: str
    revision: str
    values: Mapping[str, Any]


class ConfigurationService(Protocol):
    async def snapshot(self, target: str) -> ConfigSnapshot: ...

    async def propose(
        self,
        module_name: str,
        *,
        target: str,
        changes: Mapping[str, Any],
        summary: str,
        actor: ProposalActor,
        expected_revision: str | None = None,
    ) -> ProposalRecord: ...

    async def stage_secret(self, name: str, value: str) -> str: ...


class RestartService(Protocol):
    @property
    def requested(self) -> bool: ...

    async def request(self, *, reason: str, revision: str) -> None: ...


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
    configuration: ConfigurationService | None = None
    restart: RestartService | None = None
    raw_bot: Any = None
    raw_storage: Any = None


__all__ = [
    "MODULE_API_VERSION",
    "MODULE_ENTRYPOINT_GROUP",
    "ActivationMode",
    "AppModule",
    "ConfigSnapshot",
    "ConfigurationService",
    "GuildSettingsSchema",
    "ModuleCapabilities",
    "ModuleLoadContext",
    "ModuleMigration",
    "ModulePermissions",
    "ModuleRuntimeContext",
    "ModuleSetting",
    "ModuleSettingsDefinition",
    "ModuleSpec",
    "ProposalActionHandler",
    "ProposalActor",
    "ProposalApplyResult",
    "ProposalDraft",
    "ProposalError",
    "ProposalNotFound",
    "ProposalNotPending",
    "ProposalPreview",
    "ProposalRecord",
    "ProposalService",
    "ProposalStale",
    "ProposalState",
    "RestartService",
    "ServiceDeclaration",
    "ServiceRequirement",
    "TrustTier",
]
