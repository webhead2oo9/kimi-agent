"""Settings declarations for lifecycle-aware application modules.

The validation rules are shared with operator plugins, but module overrides live
under ``config/modules`` so the two extension surfaces never collide.
"""

from __future__ import annotations

from pathlib import Path

from community_agent_module_api import ModuleSetting, ModuleSettingsDefinition

from config.plugin_settings import (
    PluginSetting,
    PluginSettingsDefinition,
    PluginSettingsEntry as ModuleSettingsEntry,
    PluginSettingsError as ModuleSettingsError,
    PluginSettingsRegistry,
)


class ModuleSettingsRegistry(PluginSettingsRegistry):
    def __init__(self, *, config_dir: Path) -> None:
        super().__init__(config_dir=config_dir, namespace="modules")

    def prepare_module(self, definition: ModuleSettingsDefinition) -> ModuleSettingsEntry:
        """Validate a public module declaration with the shared settings engine."""
        internal = PluginSettingsDefinition(
            name=definition.name,
            label=definition.label,
            model=definition.model,
            exposed=tuple(
                PluginSetting(
                    field=setting.field,
                    label=setting.label,
                    help=setting.help,
                    choices=setting.choices,
                    minimum=setting.minimum,
                    multiline=setting.multiline,
                )
                for setting in definition.exposed
            ),
            environment_only=definition.environment_only,
        )
        return super().prepare(internal)


__all__ = [
    "ModuleSetting",
    "ModuleSettingsDefinition",
    "ModuleSettingsEntry",
    "ModuleSettingsError",
    "ModuleSettingsRegistry",
]
