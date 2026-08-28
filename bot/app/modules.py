"""Required, lifecycle-aware application modules.

Unlike operator plugins, application modules may own Discord commands/listeners,
database schema, background work, and optional LLM tools.  Installed packages
are discovered through Python entry points, but only names explicitly listed in
``KIMI_MODULES`` are loaded.  A requested module is part of the deployment
contract: any load or startup failure aborts startup instead of silently
removing the capability.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from importlib.metadata import entry_points
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from agent.activity import register_tool_labels
from app.tool_surfaces import declare_surface_tools
from config.module_settings import ModuleSettingsError, ModuleSettingsRegistry
from kimi_agent_module_api.contracts import (
    DiscordActions,
    ModuleHealth,
    TrustLookup,
    table_prefix,
    validate_guild_settings_schema,
    validate_module_name,
    validate_permissions,
    validate_services,
)
from kimi_agent_module_api import (
    AppModule,
    MODULE_API_VERSION,
    MODULE_ENTRYPOINT_GROUP,
    ModuleCapabilities,
    ModuleLoadContext,
    ModuleRuntimeContext,
    ModuleSetting,
    ModuleSettingsDefinition,
    ModuleSpec,
    ModuleToolRegistry,
    ProposalService,
)

if TYPE_CHECKING:
    from config.settings import Settings
    from tools.registry import ToolRegistry

from modules.actions import DeclaredDiscordActions
from storage.db import Database
from modules.events import EventBusImpl, ModuleEventView
from modules.guild_settings import GuildSettingsService
from modules.health import HealthRegistry
from modules.http import ModuleHttpRuntime, ResolvedHostRule, resolve_host_rules
from modules.scheduler import DurableScheduler
from modules.services import ModuleServiceView, ServiceRegistryImpl, undeclared_provisions
from modules.storage import ModuleStorageImpl, validate_table_aliases

log = logging.getLogger(__name__)


def _module_tool_registry(registry: ToolRegistry) -> ModuleToolRegistry:
    """Keep the core registry statically conformant with the published SDK."""
    return registry


def module_capabilities(core_settings: Settings) -> ModuleCapabilities:
    """Build the stable capability advertisement for one core configuration."""
    available = {"discord.history.v1", "proposals.v2"}
    if core_settings.message_content_intent:
        available.add("discord.message_content.v1")
    return ModuleCapabilities(
        available=frozenset(available),
        members_intent=bool(core_settings.members_intent),
        message_content_intent=bool(core_settings.message_content_intent),
    )


@dataclass(frozen=True)
class ModuleLoadState:
    requested: tuple[str, ...] = ()
    loaded: tuple[str, ...] = ()
    disabled: tuple[tuple[str, str, str], ...] = ()


def _installed_specs(requested: Sequence[str]) -> dict[str, ModuleSpec]:
    requested_names = set(requested)
    found: dict[str, ModuleSpec] = {}
    for point in entry_points(group=MODULE_ENTRYPOINT_GROUP):
        if point.name not in requested_names:
            continue
        if point.name in found:
            raise RuntimeError(f"Duplicate installed Kimi module entry point {point.name!r}")
        loaded = point.load()
        if not isinstance(loaded, ModuleSpec):
            raise TypeError(f"Kimi module entry point {point.name!r} did not export ModuleSpec")
        if loaded.name != point.name:
            raise RuntimeError(
                f"Kimi module entry point {point.name!r} exports mismatched name {loaded.name!r}"
            )
        found[point.name] = loaded
    return found


def _ordered_specs(
    requested: Sequence[str], installed: Mapping[str, ModuleSpec]
) -> list[ModuleSpec]:
    requested_tuple = tuple(requested)
    requested_set = set(requested_tuple)
    if len(requested_set) != len(requested_tuple):
        raise RuntimeError("KIMI_MODULES contains a duplicate module name")
    missing = [name for name in requested_tuple if name not in installed]
    if missing:
        raise RuntimeError(f"Configured Kimi module is not installed: {missing[0]}")

    visiting: set[str] = set()
    visited: set[str] = set()
    ordered: list[ModuleSpec] = []

    def visit(name: str) -> None:
        if name in visited:
            return
        if name in visiting:
            raise RuntimeError(f"Kimi module dependency cycle includes {name!r}")
        visiting.add(name)
        spec = installed[name]
        for dependency in spec.dependencies:
            if dependency not in requested_set:
                raise RuntimeError(f"Kimi module {name!r} requires active module {dependency!r}")
            visit(dependency)
        visiting.remove(name)
        visited.add(name)
        ordered.append(spec)

    for requested_name in requested_tuple:
        visit(requested_name)
    return ordered


def validate_module_selection(
    names: Sequence[str],
    *,
    core_settings: Settings,
    installed: Mapping[str, ModuleSpec] | None = None,
) -> tuple[ModuleSpec, ...]:
    """Preflight one explicit module set without creating module instances."""
    if not names:
        return ()
    specs = _ordered_specs(
        names,
        installed if installed is not None else _installed_specs(names),
    )
    by_prefix: dict[str, str] = {}
    for spec in specs:
        prefix = table_prefix(spec.name)
        if previous := by_prefix.get(prefix):
            raise RuntimeError(
                f"Kimi modules {previous!r} and {spec.name!r} share normalized prefix {prefix!r}"
            )
        by_prefix[prefix] = spec.name
    capabilities = module_capabilities(core_settings)
    for spec in specs:
        if spec.api_version != MODULE_API_VERSION:
            raise RuntimeError(
                f"Kimi module {spec.name!r} requires module API {spec.api_version}; "
                f"core provides {MODULE_API_VERSION}"
            )
        missing_capability = next(
            (
                capability
                for capability in spec.requires_capabilities
                if capability not in capabilities.available
            ),
            None,
        )
        if missing_capability is not None:
            raise RuntimeError(
                f"Kimi module {spec.name!r} requires unavailable capability {missing_capability!r}"
            )
        _validate_declarations(spec)
    by_name = {spec.name: spec for spec in specs}
    for spec in specs:
        for requirement in spec.consumes:
            provider = by_name[requirement.provider]
            if not any(
                declaration.name == requirement.name and declaration.version == requirement.version
                for declaration in provider.provides
            ):
                raise RuntimeError(
                    f"Kimi module {spec.name!r} consumes {requirement.name}@"
                    f"{requirement.version} from {requirement.provider!r}, but that provider "
                    "does not declare it"
                )
    return tuple(specs)


def _validate_declarations(spec: ModuleSpec) -> None:
    """Reject malformed declarations before any module code is created."""
    try:
        if spec.name == "proposals":
            raise ValueError("module name 'proposals' is reserved by core")
        validate_module_name(spec.name)
        validate_permissions(spec.name, spec.permissions)
        validate_services(spec.name, spec.dependencies, spec.provides, spec.consumes)
        if spec.guild_settings is not None:
            validate_guild_settings_schema(spec.name, spec.guild_settings)
        validate_table_aliases(spec.name, spec.table_aliases)
    except ValueError as exc:
        raise RuntimeError(f"Kimi module {spec.name!r} has an invalid declaration: {exc}") from exc


def _activation_disabled(
    specs: Sequence[ModuleSpec], capabilities: ModuleCapabilities
) -> dict[str, str]:
    disabled: dict[str, str] = {}
    for spec in specs:
        missing = [
            capability
            for capability in spec.activation_capabilities
            if capability not in capabilities.available
        ]
        if missing:
            disabled[spec.name] = "missing activation capability " + ", ".join(missing)
            continue
        unavailable_dependencies = [name for name in spec.dependencies if name in disabled]
        if unavailable_dependencies:
            disabled[spec.name] = "dependency disabled: " + ", ".join(unavailable_dependencies)
    return disabled


@dataclass(frozen=True)
class ModuleRuntimeBase:
    """Core-side inputs the manager turns into per-module contexts."""

    database: Database
    bot: Any
    is_guild_active: Callable[[int], bool]
    current_config_dir: Callable[[], Path]
    capabilities: ModuleCapabilities
    trust: TrustLookup
    discord: DiscordActions | None = None
    proposals: ProposalViewFactory | None = None


class ProposalViewFactory(Protocol):
    def view_for(self, module_name: str) -> ProposalService: ...


_REQUIRED_PORTS = (
    "events",
    "scheduler",
    "storage",
    "health",
    "discord",
    "interactions",
    "http",
    "services",
    "trust",
)


@dataclass
class ModuleManager:
    """Configured module instances and their coordinated lifecycle."""

    load_state: ModuleLoadState = field(default_factory=ModuleLoadState)
    settings: ModuleSettingsRegistry | None = None
    _specs: tuple[ModuleSpec, ...] = ()
    _modules: dict[str, AppModule] = field(default_factory=dict)
    _started: list[str] = field(default_factory=list)
    _contexts: dict[str, ModuleRuntimeContext] = field(default_factory=dict)
    health: HealthRegistry = field(default_factory=HealthRegistry)
    services: ServiceRegistryImpl = field(default_factory=ServiceRegistryImpl)
    events: EventBusImpl | None = None
    scheduler: DurableScheduler | None = None
    guild_settings: GuildSettingsService | None = None
    http: ModuleHttpRuntime | None = None
    _host_rules: dict[str, tuple[ResolvedHostRule, ...]] = field(default_factory=dict)

    @property
    def config_dir(self) -> Path:
        if self.settings is None:
            raise RuntimeError("Kimi module manager has not been loaded")
        return self.settings.config_dir

    @classmethod
    def load(
        cls,
        names: Sequence[str],
        *,
        core_settings: Settings,
        registry: ToolRegistry,
        installed: Mapping[str, ModuleSpec] | None = None,
    ) -> ModuleManager:
        settings_registry = ModuleSettingsRegistry(config_dir=Path(core_settings.config_dir))
        manager = cls(
            load_state=ModuleLoadState(requested=tuple(names)),
            settings=settings_registry,
        )
        if not names:
            return manager
        specs = validate_module_selection(
            names,
            core_settings=core_settings,
            installed=installed,
        )
        capabilities = module_capabilities(core_settings)
        disabled = _activation_disabled(specs, capabilities)
        active_specs = tuple(spec for spec in specs if spec.name not in disabled)
        for spec in active_specs:
            before = registry.registered_names()
            try:
                prepared = (
                    settings_registry.prepare_module(spec.settings) if spec.settings else None
                )
                if prepared is not None and not prepared.can_register:
                    raise ModuleSettingsError(prepared.load_error or "invalid module settings")
                ctx = ModuleLoadContext(
                    capabilities=capabilities,
                    registry=_module_tool_registry(registry),
                    module_settings=prepared.active if prepared is not None else None,
                    _register_tool_labels=register_tool_labels,
                    _declare_surface_tools=declare_surface_tools,
                )
                settings_values = prepared.active.model_dump() if prepared is not None else None
                manager._host_rules[spec.name] = resolve_host_rules(
                    spec.name, spec.permissions.http_hosts, settings_values
                )
                instance = spec.create(ctx)
            except Exception:
                registry.remove_tools(set(registry.registered_names() - before))
                raise
            manager._modules[spec.name] = instance
            log.info("Kimi module composed: %s %s", spec.name, spec.version)
        for spec in specs:
            if reason := disabled.get(spec.name):
                log.warning("Kimi module disabled: %s %s (%s)", spec.name, spec.version, reason)
        manager._specs = active_specs
        manager.load_state = ModuleLoadState(
            requested=tuple(names),
            loaded=tuple(spec.name for spec in active_specs),
            disabled=tuple(
                (spec.name, spec.version, disabled[spec.name])
                for spec in specs
                if spec.name in disabled
            ),
        )
        return manager

    @property
    def specs(self) -> Mapping[str, ModuleSpec]:
        return {spec.name: spec for spec in self._specs}

    @property
    def disabled_modules(self) -> Mapping[str, tuple[str, str]]:
        return {name: (version, reason) for name, version, reason in self.load_state.disabled}

    @property
    def guild_settings_schemas(self) -> Mapping[str, Any]:
        return {
            spec.name: spec.guild_settings
            for spec in self._specs
            if spec.guild_settings is not None
        }

    def spec(self, name: str) -> ModuleSpec:
        for spec in self._specs:
            if spec.name == name:
                return spec
        raise RuntimeError(f"Kimi module {name!r} is not active")

    def context_for(self, name: str) -> ModuleRuntimeContext:
        """The per-module runtime context handed to ``start`` (after it ran)."""
        try:
            return self._contexts[name]
        except KeyError as exc:
            raise RuntimeError(f"Kimi module {name!r} has not been started") from exc

    async def start(
        self,
        base: ModuleRuntimeBase,
        *,
        customize: Callable[[ModuleSpec, dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        """Migrate every module, then start them in dependency order.

        Each module receives its own frozen ``ModuleRuntimeContext`` assembled
        from ``base`` plus the per-module ports. ``customize`` lets the
        composition root (or a test harness) add or replace ports before the
        context is frozen; missing required ports abort startup.
        """
        try:
            for spec in self._specs:
                instance = self._modules[spec.name]
                storage = ModuleStorageImpl(base.database, spec.name, spec.table_aliases)
                await base.database.apply_module_migrations(
                    spec.name, _migrations_for(instance, storage)
                )
            for spec in self._specs:
                instance = self._modules[spec.name]
                ports = self._ports_for(spec, base)
                if customize is not None:
                    ports = customize(spec, ports)
                missing = sorted(name for name in _REQUIRED_PORTS if ports.get(name) is None)
                if missing:
                    raise RuntimeError(
                        f"Kimi module {spec.name!r} cannot start: core provided no "
                        f"{', '.join(missing)} port"
                    )
                module_ctx = ModuleRuntimeContext(**ports)
                self._contexts[spec.name] = module_ctx
                self._started.append(spec.name)
                self.health.set(spec.name, "starting")
                try:
                    await instance.start(module_ctx)
                except BaseException as exc:
                    self.health.set(spec.name, "failed", _summarize(exc))
                    raise
                self._settle_health(spec)
                log.info("Kimi module started: %s %s", spec.name, spec.version)
        except BaseException:
            await self.close()
            raise

    def _ports_for(self, spec: ModuleSpec, base: ModuleRuntimeBase) -> dict[str, Any]:
        def is_module_guild_active(guild_id: int) -> bool:
            if not base.is_guild_active(guild_id):
                return False
            if self.guild_settings is None or spec.guild_settings is None:
                return True
            return self.guild_settings.get(guild_id, spec.name).valid

        return {
            "module_name": spec.name,
            "is_guild_active": is_module_guild_active,
            "current_config_dir": base.current_config_dir,
            "capabilities": base.capabilities,
            "events": (
                ModuleEventView(
                    self.events,
                    spec.name,
                    spec.permissions,
                    is_guild_active=is_module_guild_active,
                )
                if self.events is not None
                else None
            ),
            "scheduler": self.scheduler.view_for(spec.name) if self.scheduler is not None else None,
            "storage": ModuleStorageImpl(base.database, spec.name, spec.table_aliases),
            "health": self.health.reporter_for(spec.name),
            "discord": (
                DeclaredDiscordActions(base.discord, spec.name, spec.permissions.discord_actions)
                if base.discord is not None
                else None
            ),
            "interactions": None,
            "http": (
                self.http.client_for(spec.name, self._host_rules.get(spec.name, ()))
                if self.http is not None
                else None
            ),
            "services": ModuleServiceView(self.services, spec.name, spec.provides, spec.consumes),
            "trust": base.trust,
            "guild_settings": (
                self.guild_settings.view_for(spec.name, base.is_guild_active)
                if self.guild_settings is not None and spec.guild_settings is not None
                else None
            ),
            "proposals": base.proposals.view_for(spec.name) if base.proposals is not None else None,
            "raw_bot": base.bot if spec.permissions.raw_bot else None,
            "raw_storage": base.database if spec.permissions.raw_storage else None,
        }

    def _settle_health(self, spec: ModuleSpec) -> None:
        """After a clean start: healthy unless the module said otherwise."""
        missing = undeclared_provisions(spec.name, spec.provides, self.services.provided_by)
        current = self.health.module_state(spec.name)
        if missing:
            self.health.set_constraint(
                spec.name,
                "services",
                "degraded",
                "declared but did not provide " + ", ".join(missing),
            )
        else:
            self.health.set_constraint(spec.name, "services", "healthy")
        if current is None or current.state == "starting":
            self.health.set(spec.name, "healthy")

    def host_rules(self, name: str) -> tuple[ResolvedHostRule, ...]:
        return self._host_rules.get(name, ())

    def health_snapshot(self) -> Mapping[str, ModuleHealth]:
        return self.health.snapshot()

    async def close(self) -> None:
        while self._started:
            name = self._started.pop()
            try:
                await self._modules[name].close()
            except Exception:
                log.exception("Error closing Kimi module %s", name)
            finally:
                router = getattr(self._contexts.get(name), "interactions", None)
                close_router = getattr(router, "close", None)
                if callable(close_router):
                    try:
                        close_router()
                    except Exception:
                        log.exception("Error closing interactions for Kimi module %s", name)
                if self.events is not None:
                    await self.events.close_module(name)
                if self.scheduler is not None:
                    self.scheduler.unregister_module(name)
                self.services.retire_module(name)
                self.health.forget(name)
                self._contexts.pop(name, None)


def _migrations_for(instance: AppModule, storage: ModuleStorageImpl) -> tuple[Any, ...]:
    scoped = tuple(getattr(instance, "scoped_migrations", ()))

    def wrap(migrate: Callable[[Any], Awaitable[None]]) -> Callable[[Any], Awaitable[None]]:
        async def run(conn: Any) -> None:
            await migrate(storage.migration_context(conn))

        return run

    return tuple((name, wrap(migrate)) for name, migrate in scoped)


def _summarize(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}".strip()
    return text[:200]


__all__ = [
    "MODULE_API_VERSION",
    "MODULE_ENTRYPOINT_GROUP",
    "AppModule",
    "ModuleLoadContext",
    "ModuleLoadState",
    "ModuleManager",
    "ModuleRuntimeBase",
    "ModuleRuntimeContext",
    "ModuleSetting",
    "ModuleSettingsDefinition",
    "ModuleSpec",
    "validate_module_selection",
]
