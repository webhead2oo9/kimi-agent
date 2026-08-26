"""Required, lifecycle-aware application modules.

Unlike operator plugins, application modules may own Discord commands/listeners,
database schema, background work, and optional LLM tools.  Installed packages
are discovered through Python entry points, but only names explicitly listed in
``KIMI_MODULES`` are loaded.  A requested module is part of the deployment
contract: any load or startup failure aborts startup instead of silently
removing the capability.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from importlib.metadata import entry_points
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent.activity import register_tool_labels
from app.tool_surfaces import declare_surface_tools
from config.module_settings import ModuleSettingsError, ModuleSettingsRegistry
from kimi_agent_module_api.contracts import (
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

    async def start(self, ctx: ModuleRuntimeContext) -> None:
        try:
            for spec in self._specs:
                instance = self._modules[spec.name]
                migrations = tuple(getattr(instance, "migrations", ()))
                await ctx.database.apply_module_migrations(spec.name, migrations)
            for spec in self._specs:
                instance = self._modules[spec.name]
                self._started.append(spec.name)
                await instance.start(ctx)
                log.info("Kimi module started: %s %s", spec.name, spec.version)
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        while self._started:
            name = self._started.pop()
            try:
                await self._modules[name].close()
            except Exception:
                log.exception("Error closing Kimi module %s", name)


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
