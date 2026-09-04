"""Operator plugin loading.

A plugin is an importable module named in ``PLUGIN_MODULES`` that exposes
``register(ctx: PluginContext) -> None`` and an integer ``PLUGIN_API_VERSION``.
Plugins are operator-trusted code. This is a
composition seam, not a sandbox: they receive the full public ``Settings``
object, the live tool registry, and the Discord gateway. Private integrations
(their own clients, their own ``pydantic_settings.BaseSettings`` over the same
selected dotenv, their guild scoping) live behind this contract so the public core
never references them by name.

A plugin may expose a ``PLUGIN_SETTINGS`` declaration. Its exhaustive field
classification is validated before registration; safe overrides are loaded from
``<CONFIG_DIR>/plugins/<name>.md`` and the resulting prepared instance is passed
through ``PluginContext``. An invalid fragment disables that plugin without
discarding the operator's repairable settings file.

Failure semantics mirror ``skills/registration.py``: a plugin that fails to
import or register is logged and skipped (never a boot abort), and any tools
it half-registered are rolled back so a broken plugin cannot leave a partial
surface behind. Core registers first, so a duplicate tool name raises inside
the plugin's ``register()`` and resolves in core's favor via the same skip.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar

from pydantic_settings import BaseSettings

from agent.activity import register_tool_labels
from app.tool_surfaces import (
    declare_surface_tools,
    restore_surface_tools,
    snapshot_surface_tools,
)
from config.plugin_settings import (
    PluginSetting,
    PluginSettingsDefinition,
    PluginSettingsError,
    PluginSettingsRegistry,
)
from config.settings import Settings
from tools.registry import ToolRegistry

if TYPE_CHECKING:
    from discord_adapter.gateway import DiscordGateway

log = logging.getLogger(__name__)

PLUGIN_API_VERSION = 2
_SettingsT = TypeVar("_SettingsT", bound=BaseSettings)

__all__ = [
    "PLUGIN_API_VERSION",
    "PluginContext",
    "PluginSetting",
    "PluginSettingsDefinition",
    "build_plugin_context",
    "load_plugins_with_settings",
]


@dataclass(frozen=True)
class PluginContext:
    """Everything a plugin's ``register()`` receives.

    ``settings`` is the public Settings instance (shared values such as
    ``gemini_api_key``); ``plugin_settings`` is the private settings instance
    prepared from the selected dotenv plus its safe override file.
    ``register_tool_labels`` merges friendly
    activity labels for the plugin's tools (first-wins; core is authoritative).
    ``declare_surface_tools`` adds the plugin's own tools to the eval-harness
    surfaces (see ``app/tool_surfaces.py``), because public core cannot name them.
    """

    settings: Settings
    registry: ToolRegistry
    gateway: DiscordGateway
    register_tool_labels: Callable[[Mapping[str, str]], None]
    plugin_settings: BaseSettings | None = None

    def declare_surface_tools(self, surface: str, names: Sequence[str]) -> None:
        declare_surface_tools(surface, names)

    def settings_for(self, settings_type: type[_SettingsT]) -> _SettingsT:
        """Return the prepared settings instance for this plugin.

        The runtime loader supplies the prepared, overlaid instance for every
        plugin that declares ``PLUGIN_SETTINGS``.
        """
        if self.plugin_settings is None:
            raise TypeError("plugin has no prepared settings; declare PLUGIN_SETTINGS")
        if not isinstance(self.plugin_settings, settings_type):
            raise TypeError(f"prepared plugin settings are not {settings_type.__name__}")
        return self.plugin_settings


def build_plugin_context(
    settings: Settings,
    registry: ToolRegistry,
    gateway: DiscordGateway,
) -> PluginContext:
    return PluginContext(
        settings=settings,
        registry=registry,
        gateway=gateway,
        register_tool_labels=register_tool_labels,
    )


def load_plugins_with_settings(
    module_names: Sequence[str],
    ctx: PluginContext,
    *,
    settings_registry: PluginSettingsRegistry | None = None,
) -> PluginSettingsRegistry:
    """Load plugins and prepare any declared safe settings before registration."""
    registry = settings_registry or PluginSettingsRegistry(config_dir=Path(ctx.settings.config_dir))
    for name in module_names:
        before = ctx.registry.registered_names()
        surfaces_before = snapshot_surface_tools()
        try:
            module = importlib.import_module(name)
            version = getattr(module, "PLUGIN_API_VERSION", None)
            if version != PLUGIN_API_VERSION:
                log.warning(
                    "Skipping plugin %s: PLUGIN_API_VERSION %r is not the supported %d",
                    name,
                    version,
                    PLUGIN_API_VERSION,
                )
                continue
            register = getattr(module, "register", None)
            if not callable(register):
                log.warning("Skipping plugin %s: it exposes no callable register(ctx)", name)
                continue
            declaration = getattr(module, "PLUGIN_SETTINGS", None)
            plugin_ctx = ctx
            if declaration is not None:
                if not isinstance(declaration, PluginSettingsDefinition):
                    raise PluginSettingsError("PLUGIN_SETTINGS must be a PluginSettingsDefinition")
                prepared = registry.prepare(declaration)
                if not prepared.can_register:
                    log.error(
                        "Skipping plugin %s because its saved settings are invalid: %s",
                        name,
                        prepared.load_error,
                    )
                    continue
                plugin_ctx = replace(ctx, plugin_settings=prepared.active)
            register(plugin_ctx)
        except Exception:
            log.exception("Plugin %s failed; continuing without it", name)
            leftover = ctx.registry.registered_names() - before
            if leftover:
                ctx.registry.remove_tools(set(leftover))
                log.warning(
                    "Rolled back %d tool(s) from failed plugin %s: %s",
                    len(leftover),
                    name,
                    ", ".join(sorted(leftover)),
                )
            # A failed plugin's surface declarations roll back with it, so it
            # leaves nothing behind at all.
            restore_surface_tools(surfaces_before)
            continue
        log.info("Plugin registered: %s", name)
    return registry
