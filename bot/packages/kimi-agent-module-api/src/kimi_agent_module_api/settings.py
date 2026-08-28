"""Public settings declarations owned by the module API."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic_settings import BaseSettings


@dataclass(frozen=True)
class ModuleSetting:
    """Presentation metadata for one explicitly exposed settings field."""

    field: str
    label: str
    help: str = ""
    choices: tuple[str, ...] = ()
    minimum: int | float | None = None
    multiline: bool = False


@dataclass(frozen=True)
class ModuleSettingsDefinition:
    """A module's settings model and its operator-editable subset."""

    name: str
    label: str
    model: type[BaseSettings]
    exposed: tuple[ModuleSetting, ...]
    environment_only: frozenset[str] = frozenset()


__all__ = ["ModuleSetting", "ModuleSettingsDefinition"]
