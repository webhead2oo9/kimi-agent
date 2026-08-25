"""Settings declarations for lifecycle-aware application modules.

The validation rules are shared with operator plugins, but module overrides live
under ``config/modules`` so the two extension surfaces never collide.
"""

from __future__ import annotations

from pathlib import Path

from config.plugin_settings import (
    PluginSetting as ModuleSetting,
    PluginSettingsDefinition as ModuleSettingsDefinition,
    PluginSettingsEntry as ModuleSettingsEntry,
    PluginSettingsError as ModuleSettingsError,
    PluginSettingsRegistry,
)


class ModuleSettingsRegistry(PluginSettingsRegistry):
    def __init__(self, *, config_dir: Path) -> None:
        super().__init__(config_dir=config_dir, namespace="modules")


__all__ = [
    "ModuleSetting",
    "ModuleSettingsDefinition",
    "ModuleSettingsEntry",
    "ModuleSettingsError",
    "ModuleSettingsRegistry",
]
