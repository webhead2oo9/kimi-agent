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
from dataclasses import dataclass, field, replace
from importlib.metadata import entry_points
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent.activity import register_tool_labels
from app.tool_surfaces import declare_surface_tools
from config.module_settings import ModuleSettingsError, ModuleSettingsRegistry
from kimi_agent_module_api.contracts import (
    ModuleHealth,
    validate_guild_settings_schema,
    validate_module_name,
    validate_permissions,
    validate_services,
)
from kimi_agent_module_api import (
    MODULE_API_VERSION,
    MODULE_ENTRYPOINT_GROUP,
    AppModule,
    GuildConfigValidator,
    ModuleCapabilities,
    ModuleLoadContext,
    ModuleMigration,
    ModuleRuntimeContext,
    ModuleSetting,
    ModuleSettingsDefinition,
    ModuleSpec,
)

if TYPE_CHECKING:
    from config.settings import Settings
    from discord_adapter.gateway import DiscordGateway
    from tools.registry import ToolRegistry

from modules.actions import DeclaredDiscordActions
from modules.events import EventBusImpl, ModuleEventView
from modules.health import HealthRegistry
from modules.services import ModuleServiceView, ServiceRegistryImpl, undeclared_provisions
from modules.storage import ModuleStorageImpl, validate_table_aliases

log = logging.getLogger(__name__)


def module_capabilities(core_settings: Settings) -> ModuleCapabilities:
    """Build the stable capability advertisement for one core configuration."""
    available = (
        frozenset({"proposals.v1", "config.v1", "restart.v1"})
        if core_settings.control_plane_enabled
        else frozenset()
    )
    return ModuleCapabilities(
        available=available,
        members_intent=bool(core_settings.members_intent),
        message_content_intent=bool(core_settings.message_content_intent),
    )


@dataclass(frozen=True)
class ModuleLoadState:
    requested: tuple[str, ...] = ()
    loaded: tuple[str, ...] = ()


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
    return tuple(specs)


def _validate_declarations(spec: ModuleSpec) -> None:
    """Reject malformed declarations before any module code is created."""
    try:
        validate_module_name(spec.name)
        validate_permissions(spec.name, spec.permissions)
        validate_services(spec.name, spec.dependencies, spec.provides, spec.consumes)
        if spec.guild_settings is not None:
            validate_guild_settings_schema(spec.name, spec.guild_settings)
        validate_table_aliases(spec.name, spec.table_aliases)
    except ValueError as exc:
        raise RuntimeError(f"Kimi module {spec.name!r} has an invalid declaration: {exc}") from exc


@dataclass
class ModuleManager:
    """Configured module instances and their coordinated lifecycle."""

    load_state: ModuleLoadState = field(default_factory=ModuleLoadState)
    settings: ModuleSettingsRegistry | None = None
    _specs: tuple[ModuleSpec, ...] = ()
    _modules: dict[str, AppModule] = field(default_factory=dict)
    _validators: dict[str, GuildConfigValidator] = field(default_factory=dict)
    _started: list[str] = field(default_factory=list)
    _contexts: dict[str, ModuleRuntimeContext] = field(default_factory=dict)
    health: HealthRegistry = field(default_factory=HealthRegistry)
    services: ServiceRegistryImpl = field(default_factory=ServiceRegistryImpl)
    events: EventBusImpl | None = None

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
        gateway: DiscordGateway,
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
        for spec in specs:
            before = registry.registered_names()
            try:
                prepared = settings_registry.prepare(spec.settings) if spec.settings else None
                if prepared is not None and not prepared.can_register:
                    raise ModuleSettingsError(prepared.load_error or "invalid module settings")
                ctx = ModuleLoadContext(
                    capabilities=capabilities,
                    registry=registry,
                    gateway=gateway,
                    module_settings=prepared.active if prepared is not None else None,
                    _register_guild_validator=manager._register_guild_validator,
                    _register_tool_labels=register_tool_labels,
                    _declare_surface_tools=declare_surface_tools,
                )
                instance = spec.create(ctx)
            except Exception:
                registry.remove_tools(set(registry.registered_names() - before))
                raise
            manager._modules[spec.name] = instance
            log.info("Kimi module composed: %s %s", spec.name, spec.version)
        manager._specs = tuple(specs)
        manager.load_state = ModuleLoadState(
            requested=tuple(names),
            loaded=tuple(spec.name for spec in specs),
        )
        return manager

    def _register_guild_validator(self, name: str, validator: GuildConfigValidator) -> None:
        if not name or name in self._validators:
            raise RuntimeError(f"Duplicate Kimi module guild validator {name!r}")
        self._validators[name] = validator

    def validate_guild_config(self, metadata: Mapping[str, Any]) -> bool:
        return all(validator(metadata) for validator in self._validators.values())

    def get(self, name: str) -> AppModule:
        try:
            return self._modules[name]
        except KeyError as exc:
            raise RuntimeError(f"Kimi module {name!r} is not active") from exc

    @property
    def specs(self) -> Mapping[str, ModuleSpec]:
        return {spec.name: spec for spec in self._specs}

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
        ctx: ModuleRuntimeContext,
        *,
        customize: Callable[[ModuleSpec, ModuleRuntimeContext], ModuleRuntimeContext] | None = None,
    ) -> None:
        """Migrate every module, then start them in dependency order.

        Each module receives its own frozen copy of ``ctx`` carrying its
        ``module_name``; ``customize`` lets the composition root attach the
        per-module service ports before the module sees the context.
        """
        try:
            for spec in self._specs:
                instance = self._modules[spec.name]
                storage = ModuleStorageImpl(ctx.database, spec.name, spec.table_aliases)
                await ctx.database.apply_module_migrations(
                    spec.name, _migrations_for(instance, storage)
                )
            for spec in self._specs:
                instance = self._modules[spec.name]
                module_ctx = replace(
                    ctx,
                    module_name=spec.name,
                    storage=ModuleStorageImpl(ctx.database, spec.name, spec.table_aliases),
                    health=self.health.reporter_for(spec.name),
                    services=ModuleServiceView(
                        self.services, spec.name, spec.provides, spec.consumes
                    ),
                    events=(
                        ModuleEventView(self.events, spec.name, spec.permissions)
                        if self.events is not None
                        else None
                    ),
                    discord=(
                        DeclaredDiscordActions(
                            ctx.discord, spec.name, spec.permissions.discord_actions
                        )
                        if ctx.discord is not None
                        else None
                    ),
                )
                if customize is not None:
                    module_ctx = customize(spec, module_ctx)
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

    def _settle_health(self, spec: ModuleSpec) -> None:
        """After a clean start: healthy unless the module said otherwise."""
        missing = undeclared_provisions(spec.name, spec.provides, self.services.provided_by)
        current = self.health.get(spec.name)
        if missing:
            self.health.set(
                spec.name, "degraded", "declared but did not provide " + ", ".join(missing)
            )
        elif current is None or current.state == "starting":
            self.health.set(spec.name, "healthy")

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
                if self.events is not None:
                    await self.events.close_module(name)
                self.services.retire_module(name)
                self.health.forget(name)
                self._contexts.pop(name, None)


def _migrations_for(instance: AppModule, storage: ModuleStorageImpl) -> tuple[Any, ...]:
    scoped = tuple(getattr(instance, "scoped_migrations", ()))
    if not scoped:
        return tuple(getattr(instance, "migrations", ()))

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
    "ModuleMigration",
    "ModuleRuntimeContext",
    "ModuleSetting",
    "ModuleSettingsDefinition",
    "ModuleSpec",
    "validate_module_selection",
]
