"""Typed per-guild settings for modules, cached like guild activation.

Each module declares a ``GuildSettingsSchema``. Values live in
``<config_dir>/guild-modules/<guild_id>/<module_name>.md`` (frontmatter only)
so every module document has its own revision hash under the control plane.

Invalid documents never half-apply. Under ``disable_module`` the module is
simply disabled for that guild; under ``disable_guild`` (the default for
enforcement modules) the guild is removed from the bot's active set until the
document is fixed, so an active guild can never run with broken moderation.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Any

from kimi_agent_module_api.contracts import (
    GuildSettingField,
    GuildSettingsSchema,
    GuildSettingsSnapshot,
    HealthState,
    coerce_guild_setting_value,
)
from utils.frontmatter import FrontmatterError, split_frontmatter_strict

log = logging.getLogger(__name__)

GUILD_MODULES_DIR = "guild-modules"
type ChangeCallback = Callable[[int], None]
type _RefreshKey = tuple[int, str]
type _RefreshBatch = tuple[tuple[int, str, int, GuildSettingsSnapshot], ...]


def coerce_value(field_spec: GuildSettingField, raw: Any) -> tuple[Any, str | None]:
    """Return (value, error). ``None`` raw means "use the default"."""
    return coerce_guild_setting_value(field_spec, raw)


def coerce_document(
    schema: GuildSettingsSchema, metadata: Mapping[str, Any]
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Apply a schema to raw frontmatter: unknown keys and bad values are errors."""
    errors: list[str] = []
    values: dict[str, Any] = {}
    known = {f.name for f in schema.fields}
    for key in metadata:
        if key not in known:
            errors.append(f"unknown setting {key!r}")
    for field_spec in schema.fields:
        value, error = coerce_value(field_spec, metadata.get(field_spec.name))
        if error is not None:
            errors.append(error)
        else:
            values[field_spec.name] = value
    if not errors and schema.validate is not None:
        try:
            errors.extend(schema.validate(values))
        except Exception as exc:
            errors.append(f"validation failed: {exc}")
    return values, tuple(errors)


def _revision(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class _Entry:
    snapshot: GuildSettingsSnapshot
    updated_at: float


@dataclass(slots=True)
class GuildSettingsService:
    """Process-wide cache of every module's per-guild settings."""

    config_dir: Callable[[], Path]
    schemas: Mapping[str, GuildSettingsSchema]
    on_health: Callable[[str, HealthState, str], None] | None = None
    clock: Callable[[], float] = time.time
    _entries: dict[tuple[int, str], _Entry] = field(default_factory=dict)
    _callbacks: dict[str, list[ChangeCallback]] = field(default_factory=dict)
    _lock: Lock = field(default_factory=Lock)
    _reported: dict[str, str] = field(default_factory=dict)
    _refresh_versions: dict[_RefreshKey, int] = field(default_factory=dict)

    def remove_module(self, module_name: str) -> None:
        """Retire a failed optional module, including any guild activation blocks."""
        with self._lock:
            self.schemas = {
                name: schema for name, schema in self.schemas.items() if name != module_name
            }
            self._entries = {
                key: value for key, value in self._entries.items() if key[1] != module_name
            }
            self._refresh_versions = {
                key: value for key, value in self._refresh_versions.items() if key[1] != module_name
            }
            self._callbacks.pop(module_name, None)
            self._reported.pop(module_name, None)

    # ---- reading --------------------------------------------------------------

    def _read(
        self, guild_id: int, module_name: str, schema: GuildSettingsSchema
    ) -> GuildSettingsSnapshot:
        base = self.config_dir()
        namespaced = base / GUILD_MODULES_DIR / str(guild_id) / f"{module_name}.md"
        try:
            text = namespaced.read_text(encoding="utf-8")
        except FileNotFoundError:
            text = ""
        except (OSError, UnicodeError) as exc:
            return GuildSettingsSnapshot({}, False, (f"unreadable document: {exc}",), "")
        if text:
            try:
                metadata, body = split_frontmatter_strict(text)
            except FrontmatterError as exc:
                return GuildSettingsSnapshot({}, False, (str(exc),), _revision(text))
            if body.strip():
                return GuildSettingsSnapshot(
                    {}, False, ("module guild settings must be frontmatter only",), _revision(text)
                )
        else:
            metadata = {}
        values, errors = coerce_document(schema, metadata)
        return GuildSettingsSnapshot(
            values=values,
            valid=not errors,
            errors=errors,
            revision=_revision(text) if text else "",
        )

    def build_refresh(self, guild_ids: Iterable[int]) -> _RefreshBatch:
        """Read and validate snapshots without holding the cache lock."""
        guilds = tuple(dict.fromkeys(guild_ids))
        versioned_keys: list[tuple[int, str, int]] = []
        with self._lock:
            # Retirement may run while disk reads are in flight. Capture the
            # schema and reserve versions atomically so it invalidates the
            # entire old batch without readers touching retired schemas.
            schemas = dict(self.schemas)
            keys = tuple((guild_id, name) for guild_id in guilds for name in schemas)
            for guild_id, module_name in keys:
                key = (guild_id, module_name)
                version = self._refresh_versions.get(key, 0) + 1
                self._refresh_versions[key] = version
                versioned_keys.append((guild_id, module_name, version))
        return tuple(
            (
                guild_id,
                module_name,
                version,
                self._read(guild_id, module_name, schemas[module_name]),
            )
            for guild_id, module_name, version in versioned_keys
        )

    def apply_refresh(self, batch: _RefreshBatch) -> None:
        """Atomically apply snapshots, then notify observers on the calling thread."""
        changed: list[tuple[str, int]] = []
        callbacks: list[tuple[str, int, tuple[ChangeCallback, ...]]] = []
        now = self.clock()
        with self._lock:
            for guild_id, module_name, version, snapshot in batch:
                key = (guild_id, module_name)
                if self._refresh_versions.get(key) != version:
                    continue
                previous = self._entries.get(key)
                if previous is None or previous.snapshot != snapshot:
                    self._entries[key] = _Entry(snapshot, now)
                    changed.append((module_name, guild_id))
            callbacks = [
                (module_name, guild_id, tuple(self._callbacks.get(module_name, ())))
                for module_name, guild_id in changed
            ]
            health = self._health_notifications_locked()
        for module_name, guild_id, observers in callbacks:
            for callback in observers:
                try:
                    callback(guild_id)
                except Exception:
                    log.exception("Guild settings observer failed for %s", module_name)
        if self.on_health is not None:
            for module_name, state, detail in health:
                self.on_health(module_name, state, detail)

    def refresh(self, guild_ids: Iterable[int]) -> None:
        """Re-read every (guild, module) pair; notify modules whose values changed."""
        self.apply_refresh(self.build_refresh(guild_ids))

    def refresh_guild(self, guild_id: int) -> None:
        self.refresh((guild_id,))

    def _health_notifications_locked(self) -> list[tuple[str, HealthState, str]]:
        if self.on_health is None:
            return []
        notifications: list[tuple[str, HealthState, str]] = []
        for module_name in self.schemas:
            entries = [
                (guild_id, entry.snapshot)
                for (guild_id, name), entry in self._entries.items()
                if name == module_name
            ]
            bad = sorted(g for g, snap in entries if not snap.valid)
            parts: list[str] = []
            if bad:
                parts.append(f"invalid guild settings in {', '.join(map(str, bad[:10]))}")
            detail = "; ".join(parts)
            previous = self._reported.get(module_name)
            if previous == detail:
                continue
            self._reported[module_name] = detail
            if detail:
                notifications.append((module_name, "degraded", detail))
            elif previous:
                notifications.append((module_name, "healthy", ""))
        return notifications

    # ---- queries ----------------------------------------------------------------

    def get(self, guild_id: int, module_name: str) -> GuildSettingsSnapshot:
        with self._lock:
            entry = self._entries.get((guild_id, module_name))
            schema = self.schemas[module_name]
        if entry is None:
            snapshot = self._read(guild_id, module_name, schema)
            with self._lock:
                if self.schemas.get(module_name) is schema:
                    self._entries[(guild_id, module_name)] = _Entry(snapshot, self.clock())
            return snapshot
        return entry.snapshot

    def is_enabled(self, guild_id: int, module_name: str, *, guild_active: bool) -> bool:
        return guild_active and self.get(guild_id, module_name).valid

    def blocked_guilds(self) -> frozenset[int]:
        """Guilds an enforcement module (``disable_guild``) has taken offline."""
        with self._lock:
            return frozenset(
                guild_id
                for (guild_id, module_name), entry in self._entries.items()
                if not entry.snapshot.valid
                and self.schemas[module_name].invalid_policy == "disable_guild"
            )

    def subscribe(self, module_name: str, callback: ChangeCallback) -> Callable[[], None]:
        with self._lock:
            self._callbacks.setdefault(module_name, []).append(callback)

        def unsubscribe() -> None:
            with self._lock:
                callbacks = self._callbacks.get(module_name, [])
                if callback in callbacks:
                    callbacks.remove(callback)

        return unsubscribe

    def view_for(
        self, module_name: str, is_guild_active: Callable[[int], bool]
    ) -> ModuleGuildSettingsView:
        return ModuleGuildSettingsView(self, module_name, is_guild_active)


@dataclass(slots=True)
class _Registration:
    unsubscribe: Callable[[], None]
    closed: bool = False

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.unsubscribe()


@dataclass(frozen=True, slots=True)
class ModuleGuildSettingsView:
    """The ``GuildSettings`` port handed to one module."""

    service: GuildSettingsService
    module_name: str
    is_guild_active: Callable[[int], bool]

    def guild_ids(self) -> tuple[int, ...]:
        with self.service._lock:
            guild_ids = {
                guild_id
                for guild_id, module_name in self.service._entries
                if module_name == self.module_name
            }
        return tuple(sorted(guild_id for guild_id in guild_ids if self.is_guild_active(guild_id)))

    def get(self, guild_id: int) -> GuildSettingsSnapshot:
        return self.service.get(guild_id, self.module_name)

    def is_enabled(self, guild_id: int) -> bool:
        return self.service.is_enabled(
            guild_id, self.module_name, guild_active=self.is_guild_active(guild_id)
        )

    def on_change(self, callback: ChangeCallback) -> _Registration:
        return _Registration(self.service.subscribe(self.module_name, callback))


__all__ = [
    "GUILD_MODULES_DIR",
    "GuildSettingsService",
    "ModuleGuildSettingsView",
    "coerce_document",
    "coerce_value",
]
