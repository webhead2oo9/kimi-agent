"""Process-wide default for the operator config directory.

Stdlib-only on purpose: this module must NEVER import config.settings (or any
bot runtime module). It is what *supplies* the config directory to readers that
run before (or entirely without) the settings singleton, so importing settings
here would be a cycle and would make the default unsettable. Enforced by
tests/test_import_isolation.py.

The composition root (app/runtime.py:build_app) points the default at
``settings.config_dir`` once at startup; every reader that accepts an optional
``config_dir=`` keyword keeps that parameter as the explicit-injection seam for
tests.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Literal

_REPO_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
_default_config_dir = _REPO_CONFIG_DIR


def default_config_dir() -> Path:
    """The config directory used when a caller passes no explicit config_dir."""
    return _default_config_dir


def set_default_config_dir(path: Path) -> None:
    """Re-point the process-wide default (called once from the composition root)."""
    global _default_config_dir
    _default_config_dir = path


ActivationParser = Callable[[str], bool | None]
_MAX_SERVER_CONFIG_CHARS = 100_000
_MAX_SERVER_CONFIG_BYTES = _MAX_SERVER_CONFIG_CHARS * 4


@dataclass(frozen=True)
class GuildActivationSnapshot:
    """Validated activation decisions from ``config/servers``.

    ``invalid`` includes files that exist but are unreadable, are symlinks, do
    not validate, or do not contain an explicit activation decision. Keeping
    the sets immutable lets the message gate take a cheap, coherent snapshot.
    """

    active: frozenset[int] = frozenset()
    deactivated: frozenset[int] = frozenset()
    invalid: frozenset[int] = frozenset()


def read_regular_utf8(path: Path) -> str | None:
    """Read one bounded regular file without following a final symlink."""
    try:
        before = path.lstat()
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            return None
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                return None
            # O_NOFOLLOW is not available on every supported platform. Verify
            # that the object opened is still the one inspected above.
            if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                return None
            chunks: list[bytes] = []
            remaining = _MAX_SERVER_CONFIG_BYTES + 1
            while remaining:
                chunk = os.read(descriptor, min(65_536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
        finally:
            os.close(descriptor)
    except OSError, ValueError:
        return None
    if len(raw) > _MAX_SERVER_CONFIG_BYTES:
        return None
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return content if len(content) <= _MAX_SERVER_CONFIG_CHARS else None


def _candidate_guild_id(path: Path) -> int | None:
    if path.suffix != ".md" or not path.stem.isascii() or not path.stem.isdigit():
        return None
    guild_id = int(path.stem)
    # Guild files use the canonical decimal id as their stem. Reject aliases
    # such as 0001.md so a full scan and a one-guild refresh cannot disagree.
    return guild_id if guild_id > 0 and str(guild_id) == path.stem else None


def _classify_guild_file(
    path: Path,
    parser: ActivationParser,
) -> tuple[int, Literal["active", "deactivated", "invalid"]] | None:
    guild_id = _candidate_guild_id(path)
    if guild_id is None:
        return None
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        return guild_id, "invalid"
    content = read_regular_utf8(path)
    if content is None:
        return guild_id, "invalid"
    try:
        decision = parser(content)
    except Exception:
        decision = None
    if decision is True:
        return guild_id, "active"
    if decision is False:
        return guild_id, "deactivated"
    return guild_id, "invalid"


def _scan_guild_activations(
    config_dir: Path,
    parser: ActivationParser,
) -> GuildActivationSnapshot | None:
    directory = config_dir / "servers"
    try:
        directory_stat = directory.lstat()
        if stat.S_ISLNK(directory_stat.st_mode) or not stat.S_ISDIR(directory_stat.st_mode):
            return None
        paths = list(directory.iterdir())
    except FileNotFoundError:
        return None
    except OSError:
        return None

    buckets: dict[str, set[int]] = {
        "active": set(),
        "deactivated": set(),
        "invalid": set(),
    }
    for path in paths:
        classified = _classify_guild_file(path, parser)
        if classified is not None:
            guild_id, state = classified
            buckets[state].add(guild_id)
    return GuildActivationSnapshot(
        active=frozenset(buckets["active"]),
        deactivated=frozenset(buckets["deactivated"]),
        invalid=frozenset(buckets["invalid"]),
    )


def _is_real_directory(path: Path) -> bool:
    try:
        directory_stat = path.lstat()
    except OSError:
        return False
    return stat.S_ISDIR(directory_stat.st_mode) and not stat.S_ISLNK(directory_stat.st_mode)


class GuildActivationCache:
    """Thread-safe, refreshable activation snapshot for the message gate."""

    def __init__(self, config_dir: Path, parser: ActivationParser) -> None:
        self._config_dir = config_dir
        self._parser = parser
        self._snapshot = GuildActivationSnapshot()
        self._state_lock = Lock()
        self._refresh_lock = Lock()

    def snapshot(self) -> GuildActivationSnapshot:
        with self._state_lock:
            return self._snapshot

    def refresh(self) -> GuildActivationSnapshot:
        with self._refresh_lock:
            scanned = _scan_guild_activations(self._config_dir, self._parser)
            with self._state_lock:
                current = self._snapshot
                if scanned is None:
                    # Losing access to the directory must not erase an explicit
                    # deactivation and fall back to the environment allowlist.
                    updated = GuildActivationSnapshot(deactivated=current.deactivated)
                else:
                    preserved = current.deactivated.intersection(scanned.invalid)
                    updated = GuildActivationSnapshot(
                        active=scanned.active,
                        deactivated=scanned.deactivated.union(preserved),
                        invalid=scanned.invalid.difference(preserved),
                    )
                self._snapshot = updated
            return updated

    def refresh_guild(self, guild_id: int) -> GuildActivationSnapshot:
        """Refresh exactly one guild after its config fragment changed."""
        directory = self._config_dir / "servers"
        path = directory / f"{guild_id}.md"
        with self._refresh_lock:
            if not _is_real_directory(directory):
                with self._state_lock:
                    updated = GuildActivationSnapshot(deactivated=self._snapshot.deactivated)
                    self._snapshot = updated
                return updated
            classified = _classify_guild_file(path, self._parser)
            with self._state_lock:
                current = self._snapshot
                active = set(current.active)
                deactivated = set(current.deactivated)
                invalid = set(current.invalid)
                active.discard(guild_id)
                deactivated.discard(guild_id)
                invalid.discard(guild_id)
                if classified is not None:
                    _found_id, state = classified
                    if state == "invalid" and guild_id in current.deactivated:
                        state = "deactivated"
                    {"active": active, "deactivated": deactivated, "invalid": invalid}[state].add(
                        guild_id
                    )
                updated = GuildActivationSnapshot(
                    active=frozenset(active),
                    deactivated=frozenset(deactivated),
                    invalid=frozenset(invalid),
                )
                self._snapshot = updated
                return updated
