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

from config.fragments.guild_config import parse_id_list, read_guild_frontmatter
from config.module_settings import ModuleSetting, ModuleSettingsDefinition
from discord_adapter.io import build_embed, send_response
from storage.db import Database
from tools.embeds import EmbedSpec
from trust.resolver import TrustResolver
from trust.tiers import TrustTier

if TYPE_CHECKING:
    import aiosqlite
    from discord.ext import commands

    from discord_adapter.gateway import DiscordGateway
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
type GuildConfigValidator = Callable[[Mapping[str, Any]], bool]
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


@dataclass(frozen=True)
class ModuleLoadContext:
    capabilities: ModuleCapabilities
    registry: ToolRegistry
    gateway: DiscordGateway
    module_settings: BaseSettings | None
    _register_guild_validator: Callable[[str, GuildConfigValidator], None]
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

    def register_guild_validator(self, name: str, validator: GuildConfigValidator) -> None:
        self._register_guild_validator(name, validator)


@dataclass(frozen=True)
class ModuleRuntimeContext:
    bot: commands.Bot
    database: Database
    trust_resolver: TrustResolver
    gateway: DiscordGateway
    config_dir: Path
    is_guild_active: Callable[[int], bool]
    get_module: Callable[[str], AppModule]
    capabilities: ModuleCapabilities = field(
        default_factory=lambda: ModuleCapabilities(
            available=frozenset(),
            members_intent=False,
            message_content_intent=False,
        )
    )
    proposals: ProposalService | None = None
    configuration: ConfigurationService | None = None
    restart: RestartService | None = None


__all__ = [
    "MODULE_API_VERSION",
    "MODULE_ENTRYPOINT_GROUP",
    "ActivationMode",
    "AppModule",
    "ConfigSnapshot",
    "ConfigurationService",
    "Database",
    "EmbedSpec",
    "GuildConfigValidator",
    "ModuleCapabilities",
    "ModuleLoadContext",
    "ModuleMigration",
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
    "TrustResolver",
    "TrustTier",
    "build_embed",
    "parse_id_list",
    "read_guild_frontmatter",
    "send_response",
]
