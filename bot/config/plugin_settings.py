"""Declared, safe operator settings owned by optional plugins.

Plugins explicitly classify every field in their private ``BaseSettings`` model.
Only the exposed subset can be persisted as ``config/plugins`` overlay fragments;
credentials, endpoints, and other deployment wiring remain env-only.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path
from types import NoneType
from typing import Any, get_args

import yaml  # type: ignore[import-untyped]

from utils.frontmatter import FrontmatterError, find_frontmatter
from pydantic import SecretStr, ValidationError as PydanticValidationError
from pydantic_settings import BaseSettings

from config import field_kinds
from config.environment import selected_env_file
from config.field_kinds import coerce_scalar

# Shared with the deployment-settings and per-tool config surfaces.
KIND_INT = field_kinds.KIND_INT
KIND_FLOAT = field_kinds.KIND_FLOAT
KIND_BOOL = field_kinds.KIND_BOOL
KIND_TEXT = field_kinds.KIND_TEXT
KIND_CHOICE = field_kinds.KIND_CHOICE

_PLUGIN_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_ENV_ONLY_SUFFIXES = (
    "_base",
    "_bin",
    "_dir",
    "_directory",
    "_endpoint",
    "_file",
    "_host",
    "_path",
    "_uri",
    "_url",
)
_SECRET_TOKENS = (
    "_api_key",
    "_key",
    "_token",
    "_secret",
    "_password",
    "_credential",
    "_credentials",
)
_ENV_ONLY_EXACT_NAMES = frozenset(
    {suffix.removeprefix("_") for suffix in _ENV_ONLY_SUFFIXES}
    | {token.removeprefix("_") for token in _SECRET_TOKENS}
)


class PluginSettingsError(RuntimeError):
    """A plugin settings declaration or persisted override is invalid."""


@dataclass(frozen=True)
class PluginSetting:
    """Presentation metadata for one explicitly exposed model field."""

    field: str
    label: str
    help: str = ""
    choices: tuple[str, ...] = ()
    minimum: int | float | None = None
    multiline: bool = False


@dataclass(frozen=True)
class PluginSettingsDefinition:
    """Complete safe-settings declaration contributed by one plugin module."""

    name: str
    label: str
    model: type[BaseSettings]
    exposed: tuple[PluginSetting, ...]
    environment_only: frozenset[str]


@dataclass(frozen=True)
class PluginSettingSpec:
    field: str
    label: str
    kind: str
    help: str = ""
    choices: tuple[str, ...] = ()
    minimum: int | float | None = None
    maximum: int | float | None = None
    nullable: bool = False
    multiline: bool = False


@dataclass(frozen=True)
class PluginSettingsEntry:
    definition: PluginSettingsDefinition
    specs: tuple[PluginSettingSpec, ...]
    inherited: BaseSettings
    active: BaseSettings
    load_error: str | None = None

    @property
    def can_register(self) -> bool:
        return self.load_error is None


def _scalar_type(annotation: Any) -> tuple[Any, bool]:
    args = get_args(annotation)
    members = tuple(member for member in (args or (annotation,)) if member is not NoneType)
    if len(members) != 1:
        return object, False
    return members[0], NoneType in (args or ())


def _resolve_specs(definition: PluginSettingsDefinition) -> tuple[PluginSettingSpec, ...]:
    if not isinstance(definition.name, str) or not _PLUGIN_NAME_RE.fullmatch(definition.name):
        raise PluginSettingsError(
            "plugin settings name must be lowercase letters, digits, '-' or '_' (max 32)"
        )
    if not isinstance(definition.label, str) or not definition.label.strip():
        raise PluginSettingsError(f"plugin settings {definition.name!r} has an empty label")
    if not issubclass(definition.model, BaseSettings):
        raise PluginSettingsError(f"plugin settings {definition.name!r} model is not BaseSettings")

    model_fields = set(definition.model.model_fields)
    if not isinstance(definition.exposed, tuple) or not isinstance(
        definition.environment_only, frozenset
    ):
        raise PluginSettingsError(
            f"plugin settings {definition.name!r} classifications must be immutable"
        )
    if any(not isinstance(entry, PluginSetting) for entry in definition.exposed):
        raise PluginSettingsError(
            f"plugin settings {definition.name!r} exposed entries must be PluginSetting"
        )
    exposed_names = [entry.field for entry in definition.exposed]
    if any(not isinstance(field, str) for field in exposed_names) or any(
        not isinstance(field, str) for field in definition.environment_only
    ):
        raise PluginSettingsError(
            f"plugin settings {definition.name!r} classification names must be strings"
        )
    exposed = set(exposed_names)
    environment_only = set(definition.environment_only)
    if len(exposed_names) != len(exposed):
        raise PluginSettingsError(f"plugin settings {definition.name!r} repeats an exposed field")
    overlap = sorted(exposed & environment_only)
    if overlap:
        raise PluginSettingsError(
            f"plugin settings {definition.name!r} classifies fields twice: {', '.join(overlap)}"
        )
    missing = sorted(model_fields - exposed - environment_only)
    unknown = sorted((exposed | environment_only) - model_fields)
    if missing:
        raise PluginSettingsError(
            f"plugin settings {definition.name!r} leaves fields unclassified: " + ", ".join(missing)
        )
    if unknown:
        raise PluginSettingsError(
            f"plugin settings {definition.name!r} names unknown fields: " + ", ".join(unknown)
        )

    specs: list[PluginSettingSpec] = []
    if len(definition.exposed) > 64:
        raise PluginSettingsError(
            f"plugin settings {definition.name!r} exposes more than 64 fields"
        )
    for presentation in definition.exposed:
        field = presentation.field
        if not isinstance(field, str) or not isinstance(presentation.label, str):
            raise PluginSettingsError(
                f"plugin settings {definition.name!r} has invalid field presentation"
            )
        if not isinstance(presentation.help, str) or not isinstance(presentation.multiline, bool):
            raise PluginSettingsError(f"plugin setting presentation is invalid: {field}")
        if not isinstance(presentation.choices, tuple):
            raise PluginSettingsError(f"plugin setting choices must be a tuple: {field}")
        annotation = definition.model.model_fields[field].annotation
        scalar, nullable = _scalar_type(annotation)
        if (
            scalar is SecretStr
            or field in _ENV_ONLY_EXACT_NAMES
            or field.endswith(_ENV_ONLY_SUFFIXES)
            or any(token in field for token in _SECRET_TOKENS)
        ):
            raise PluginSettingsError(
                f"plugin settings {definition.name!r} exposes environment-only field {field!r}"
            )
        if presentation.choices:
            if scalar is not str:
                raise PluginSettingsError(f"choices require a string field: {field}")
            if len(presentation.choices) > 64 or any(
                not isinstance(choice, str) or not choice or len(choice) > 128
                for choice in presentation.choices
            ):
                raise PluginSettingsError(
                    f"choices must be 1-64 non-empty strings of at most 128 chars: {field}"
                )
            if len(set(presentation.choices)) != len(presentation.choices):
                raise PluginSettingsError(f"choices contain duplicates: {field}")
            kind = KIND_CHOICE
        elif scalar is bool:
            kind = KIND_BOOL
        elif scalar is int:
            kind = KIND_INT
        elif scalar is float:
            kind = KIND_FLOAT
        elif scalar is str:
            kind = KIND_TEXT
        else:
            raise PluginSettingsError(f"unsupported exposed field type: {field}")
        if kind in {KIND_INT, KIND_FLOAT} and presentation.minimum is None:
            raise PluginSettingsError(f"numeric plugin setting needs a minimum: {field}")
        if presentation.minimum is not None and kind not in {KIND_INT, KIND_FLOAT}:
            raise PluginSettingsError(f"minimum is only valid for numeric fields: {field}")
        if isinstance(presentation.minimum, bool) or (
            presentation.minimum is not None
            and (
                not isinstance(presentation.minimum, (int, float))
                or not math.isfinite(float(presentation.minimum))
            )
        ):
            raise PluginSettingsError(f"minimum must be a finite number: {field}")
        if presentation.multiline and kind != KIND_TEXT:
            raise PluginSettingsError(f"multiline is only valid for text fields: {field}")
        if not presentation.label.strip():
            raise PluginSettingsError(f"plugin setting has an empty label: {field}")
        if len(presentation.label) > 128 or len(presentation.help) > 1000:
            raise PluginSettingsError(f"plugin setting presentation is too long: {field}")
        specs.append(
            PluginSettingSpec(
                field=field,
                label=presentation.label,
                kind=kind,
                help=presentation.help,
                choices=presentation.choices,
                minimum=presentation.minimum,
                nullable=nullable,
                multiline=presentation.multiline,
            )
        )
    return tuple(specs)


def coerce_value(spec: PluginSettingSpec, raw: Any) -> Any:
    if raw is None:
        if spec.nullable:
            return None
        raise ValueError("null is not allowed")
    return coerce_scalar(
        spec.kind,
        raw,
        choices=spec.choices,
        minimum=spec.minimum,
        maximum=spec.maximum,
    )


def settings_values(entry: PluginSettingsEntry, settings: BaseSettings) -> dict[str, Any]:
    return {spec.field: getattr(settings, spec.field) for spec in entry.specs}


def extension_settings_path(name: str, *, config_dir: Path, namespace: str) -> Path:
    if not _PLUGIN_NAME_RE.fullmatch(name):
        raise PluginSettingsError(f"invalid plugin settings name {name!r}")
    if namespace not in {"plugins", "modules"}:
        raise PluginSettingsError(f"invalid extension settings namespace {namespace!r}")
    return config_dir / namespace / f"{name}.md"


def plugin_settings_path(name: str, *, config_dir: Path) -> Path:
    return extension_settings_path(name, config_dir=config_dir, namespace="plugins")


def _parse_document(text: str, path: Path) -> dict[str, Any]:
    if not text.strip():
        return {}
    try:
        found = find_frontmatter(text)
    except FrontmatterError as exc:
        raise PluginSettingsError(f"Invalid plugin settings {path}: {exc}") from exc
    if found is None:
        raise PluginSettingsError(f"Invalid plugin settings {path}: expected frontmatter only")
    raw_frontmatter, body = found
    if body.strip():
        raise PluginSettingsError(f"Invalid plugin settings {path}: content is not allowed")
    try:
        parsed = yaml.safe_load(raw_frontmatter)
    except yaml.YAMLError as exc:
        raise PluginSettingsError(f"Invalid plugin settings {path}: invalid YAML") from exc
    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        raise PluginSettingsError(f"Invalid plugin settings {path}: frontmatter must be a mapping")
    return {str(key): value for key, value in parsed.items()}


def parse_overrides(entry: PluginSettingsEntry, content: str, *, path: Path) -> dict[str, Any]:
    raw = _parse_document(content, path)
    specs = {spec.field: spec for spec in entry.specs}
    unknown = sorted(set(raw) - set(specs))
    if unknown:
        raise PluginSettingsError(f"Invalid plugin settings {path}: unknown setting {unknown[0]!r}")
    resolved: dict[str, Any] = {}
    for field, value in raw.items():
        try:
            resolved[field] = coerce_value(specs[field], value)
        except ValueError as exc:
            raise PluginSettingsError(f"Invalid plugin setting {field!r} in {path}: {exc}") from exc
    return resolved


def validate_candidate(entry: PluginSettingsEntry, overrides: dict[str, Any]) -> BaseSettings:
    data = entry.inherited.model_dump(mode="python")
    data.update(overrides)
    try:
        candidate = entry.definition.model.model_validate(data)
    except PydanticValidationError as exc:
        first = exc.errors(include_url=False)[0]
        location = ".".join(str(part) for part in first.get("loc", ()))
        prefix = f"{location}: " if location else ""
        raise PluginSettingsError(
            f"Invalid plugin settings candidate: {prefix}{first['msg']}"
        ) from exc
    # Validate every exposed value, including inherited values, against the
    # declared spec (not merely the fields present in one override document).
    for spec in entry.specs:
        try:
            coerce_value(spec, getattr(candidate, spec.field))
        except ValueError as exc:
            raise PluginSettingsError(
                f"Invalid plugin settings candidate: {spec.field}: {exc}"
            ) from exc
    return candidate


class PluginSettingsRegistry:
    """Prepared plugin settings and their startup active/inherited snapshots."""

    def __init__(self, *, config_dir: Path, namespace: str = "plugins") -> None:
        self.config_dir = config_dir.resolve()
        if namespace not in {"plugins", "modules"}:
            raise PluginSettingsError(f"invalid extension settings namespace {namespace!r}")
        self.namespace = namespace
        self._entries: dict[str, PluginSettingsEntry] = {}

    def prepare(self, definition: PluginSettingsDefinition) -> PluginSettingsEntry:
        specs = _resolve_specs(definition)
        if definition.name in self._entries:
            raise PluginSettingsError(f"duplicate plugin settings name {definition.name!r}")
        try:
            inherited = definition.model(_env_file=selected_env_file())
        except PydanticValidationError as exc:
            raise PluginSettingsError(
                f"Environment settings for plugin {definition.name!r} are invalid"
            ) from exc
        provisional = PluginSettingsEntry(
            definition=definition,
            specs=specs,
            inherited=inherited,
            active=inherited,
        )
        path = extension_settings_path(
            definition.name,
            config_dir=self.config_dir,
            namespace=self.namespace,
        )
        load_error: str | None = None
        active = inherited
        try:
            content = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            content = ""
        except OSError, UnicodeError:
            load_error = f"Unable to read plugin settings {path}"
            content = ""
        if load_error is None:
            try:
                overrides = parse_overrides(provisional, content, path=path)
                active = validate_candidate(provisional, overrides)
            except PluginSettingsError as exc:
                load_error = str(exc)
        entry = PluginSettingsEntry(
            definition=definition,
            specs=specs,
            inherited=inherited,
            active=active,
            load_error=load_error,
        )
        self._entries[definition.name] = entry
        return entry

    def get(self, name: str) -> PluginSettingsEntry | None:
        return self._entries.get(name)

    def entries(self) -> tuple[PluginSettingsEntry, ...]:
        return tuple(self._entries[name] for name in sorted(self._entries))
