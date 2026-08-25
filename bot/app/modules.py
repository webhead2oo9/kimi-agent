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
from typing import TYPE_CHECKING, Any, Protocol, TypeVar

from pydantic_settings import BaseSettings

from agent.activity import register_tool_labels
from app.tool_surfaces import declare_surface_tools
from config.module_settings import (
    ModuleSetting,
    ModuleSettingsDefinition,
    ModuleSettingsError,
    ModuleSettingsRegistry,
)

if TYPE_CHECKING:
    import aiosqlite
    from discord.ext import commands

    from config.settings import Settings
    from discord_adapter.gateway import DiscordGateway
    from storage.db import Database
    from tools.registry import ToolRegistry
    from trust.resolver import TrustResolver

log = logging.getLogger(__name__)

MODULE_API_VERSION = 1
MODULE_ENTRYPOINT_GROUP = "kimi_agent.modules"

GuildConfigValidator = Callable[[Mapping[str, Any]], bool]
ModuleMigration = tuple[str, Callable[["aiosqlite.Connection"], Awaitable[None]]]
_SettingsT = TypeVar("_SettingsT", bound=BaseSettings)


class AppModule(Protocol):
    """Runtime object created once for one configured application module."""

    migrations: Sequence[ModuleMigration]

    async def start(self, ctx: ModuleRuntimeContext) -> None: ...

    async def close(self) -> None: ...


@dataclass(frozen=True)
class ModuleSpec:
    """Object exported by a package's ``kimi_agent.modules`` entry point."""

    name: str
    version: str
    create: Callable[[ModuleLoadContext], AppModule]
    api_version: int = MODULE_API_VERSION
    dependencies: tuple[str, ...] = ()
    settings: ModuleSettingsDefinition | None = None


@dataclass(frozen=True)
class ModuleLoadContext:
    """Composition-time services, including the optional LLM tool seam."""

    core_settings: Settings
    registry: ToolRegistry
    gateway: DiscordGateway
    module_settings: BaseSettings | None
    _register_guild_validator: Callable[[str, GuildConfigValidator], None]

    def settings_for(self, settings_type: type[_SettingsT]) -> _SettingsT:
        if self.module_settings is None or not isinstance(self.module_settings, settings_type):
            raise TypeError(f"prepared module settings are not {settings_type.__name__}")
        return self.module_settings

    def register_tool_labels(self, labels: Mapping[str, str]) -> None:
        register_tool_labels(labels)

    def declare_surface_tools(self, surface: str, names: Sequence[str]) -> None:
        declare_surface_tools(surface, names)

    def register_guild_validator(self, name: str, validator: GuildConfigValidator) -> None:
        self._register_guild_validator(name, validator)


@dataclass(frozen=True)
class ModuleRuntimeContext:
    """Services available after the shared database connection is ready."""

    bot: commands.Bot
    database: Database
    trust_resolver: TrustResolver
    gateway: DiscordGateway
    config_dir: Path
    is_guild_active: Callable[[int], bool]
    get_module: Callable[[str], AppModule]


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
        specs = _ordered_specs(
            names,
            installed if installed is not None else _installed_specs(names),
        )
        for spec in specs:
            if spec.api_version != MODULE_API_VERSION:
                raise RuntimeError(
                    f"Kimi module {spec.name!r} requires module API {spec.api_version}; "
                    f"core provides {MODULE_API_VERSION}"
                )
            before = registry.registered_names()
            try:
                prepared = settings_registry.prepare(spec.settings) if spec.settings else None
                if prepared is not None and not prepared.can_register:
                    raise ModuleSettingsError(prepared.load_error or "invalid module settings")
                ctx = ModuleLoadContext(
                    core_settings=core_settings,
                    registry=registry,
                    gateway=gateway,
                    module_settings=prepared.active if prepared is not None else None,
                    _register_guild_validator=manager._register_guild_validator,
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
]
