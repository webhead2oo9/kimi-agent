"""Settings loaded from channel-fragment frontmatter.

Includes tool pins and blocks, automatic thread thresholds, and thread-mode
overrides. Pins remain subject to registry permissions; blocked tools fail closed.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from utils.frontmatter import split_frontmatter, split_frontmatter_strict
from config.fragments._fragment_cache import LastKnownGoodCache
from config import paths

if TYPE_CHECKING:
    from tools.registry import ToolRegistry
    from trust.tiers import TrustTier

log = logging.getLogger(__name__)

_ID_RE = re.compile(r"[0-9]+")  # Discord snowflakes
_TOOL_NAME_RE = re.compile(r"[a-zA-Z0-9_-]{1,64}")
_MAX_PINS = 16
_MAX_BLOCKED = 64
_blocked_cache: LastKnownGoodCache[frozenset[str]] = LastKnownGoodCache(max_entries=None)


class ChannelBlockedToolsLoadError(RuntimeError):
    """A present channel denylist could not be loaded safely."""


def _read_channel_frontmatter(
    channel_id: str,
    *,
    config_dir: Path | None = None,
) -> tuple[dict, str] | None:
    """Read a channel fragment for non-policy settings."""
    if not channel_id or not _ID_RE.fullmatch(channel_id):
        return None
    fragment = (config_dir or paths.default_config_dir()) / "channels" / f"{channel_id}.md"
    try:
        text = fragment.read_text(encoding="utf-8")
    except OSError, UnicodeError:
        return None
    meta, _body = split_frontmatter(text)
    return meta, str(fragment)


def parse_pinned_tools(raw: object, *, source: str) -> frozenset[str]:
    """Return valid pinned tool names, capped at ``_MAX_PINS``."""
    if not isinstance(raw, list):
        if raw is not None:
            log.warning("Ignoring non-list pinned_tools in %s", source)
        return frozenset()
    names = [name for name in raw if isinstance(name, str) and _TOOL_NAME_RE.fullmatch(name)]
    if len(names) > _MAX_PINS:
        log.warning(
            "%s lists %d pinned_tools; keeping the first %d",
            source,
            len(names),
            _MAX_PINS,
        )
        names = names[:_MAX_PINS]
    return frozenset(names)


def load_channel_pinned_tools(
    channel_id: str,
    *,
    config_dir: Path | None = None,
) -> frozenset[str]:
    """Read pinned tool names from a channel fragment."""
    result = _read_channel_frontmatter(channel_id, config_dir=config_dir)
    if result is None:
        return frozenset()
    meta, source = result
    return parse_pinned_tools(meta.get("pinned_tools"), source=source)


def load_channel_blocked_tools(
    channel_id: str,
    *,
    config_dir: Path | None = None,
) -> frozenset[str]:
    """Read ``blocked_tools`` from a channel fragment's frontmatter.

    Invalid reloads retain the last valid value. A missing initial policy is
    empty, and ``blocked_tools: []`` explicitly clears it.
    """
    if not channel_id or not _ID_RE.fullmatch(channel_id):
        return frozenset()
    fragment = (config_dir or paths.default_config_dir()) / "channels" / f"{channel_id}.md"
    key = _blocked_cache.key(fragment)
    try:
        text = fragment.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        if _blocked_cache.last_good(key) is None:
            return frozenset()
        return _retain_channel_blocked_tools(fragment, key, exc)
    except (OSError, UnicodeError) as exc:
        return _retain_channel_blocked_tools(fragment, key, exc)

    try:
        meta, _body = split_frontmatter_strict(text)
        if "blocked_tools" not in meta:
            if _blocked_cache.last_good(key) is None:
                return frozenset()
            return _retain_channel_blocked_tools(
                fragment, key, ValueError("blocked_tools is absent")
            )
        raw = meta["blocked_tools"]
        if not isinstance(raw, list):
            raise ValueError("blocked_tools must be a list")
        if len(raw) > _MAX_BLOCKED:
            raise ValueError(f"blocked_tools is capped at {_MAX_BLOCKED} entries")
        for entry in raw:
            if not isinstance(entry, str) or not _TOOL_NAME_RE.fullmatch(entry):
                raise ValueError(f"invalid blocked_tools entry: {entry!r}")
        blocked = frozenset(raw)
    except ValueError as exc:
        return _retain_channel_blocked_tools(fragment, key, exc)
    _blocked_cache.remember(key, blocked)
    return blocked


def _retain_channel_blocked_tools(
    fragment: Path,
    key: Path,
    error: BaseException,
) -> frozenset[str]:
    last_good = _blocked_cache.last_good(key)
    if last_good is not None:
        log.error(
            "Could not reload channel tool policy %s (%s); retaining last-known-good denylist",
            fragment,
            error,
        )
        return last_good
    raise ChannelBlockedToolsLoadError(
        f"Could not load channel tool policy {fragment}: {error}"
    ) from error


@dataclass(frozen=True)
class ChannelAutoThread:
    """Per-channel automatic thread-handoff settings."""

    min_lines: int | None
    min_chars: int | None
    always: bool = False


def _coerce_positive_int(value: object) -> int | None:
    if isinstance(value, bool):  # bool is an int subclass; reject true/false
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str):
        try:
            parsed = int(value.strip())
        except ValueError:
            return None
        return parsed if parsed > 0 else None
    return None


def load_channel_auto_thread(
    channel_id: str,
    *,
    config_dir: Path | None = None,
) -> ChannelAutoThread | None:
    """Read automatic thread-handoff settings from a channel fragment."""
    result = _read_channel_frontmatter(channel_id, config_dir=config_dir)
    if result is None:
        return None
    meta, _source = result
    always = meta.get("auto_thread_always") is True
    min_lines = _coerce_positive_int(meta.get("auto_thread_min_lines"))
    min_chars = _coerce_positive_int(meta.get("auto_thread_min_chars"))
    if not always and min_lines is None and min_chars is None:
        return None
    return ChannelAutoThread(min_lines=min_lines, min_chars=min_chars, always=always)


def parse_tristate(raw: object) -> bool | None:
    """Accept literal booleans; return ``None`` to inherit otherwise."""
    return raw if isinstance(raw, bool) else None


def load_channel_thread_handoff(
    channel_id: str,
    *,
    config_dir: Path | None = None,
) -> bool | None:
    """Read ``thread_handoff``; ``None`` inherits the wider setting."""
    result = _read_channel_frontmatter(channel_id, config_dir=config_dir)
    if result is None:
        return None
    meta, _source = result
    return parse_tristate(meta.get("thread_handoff"))


def load_channel_thread_auto_respond(
    channel_id: str,
    *,
    config_dir: Path | None = None,
) -> bool | None:
    """Read the default response mode for new threads; ``None`` inherits."""
    result = _read_channel_frontmatter(channel_id, config_dir=config_dir)
    if result is None:
        return None
    meta, _source = result
    return parse_tristate(meta.get("thread_auto_respond"))


def resolve_tristate(channel: bool | None, guild: bool | None) -> bool:
    """Most specific scope wins: channel, then guild, then on by default."""
    if channel is not None:
        return channel
    if guild is not None:
        return guild
    return True


def filter_pins_to_searchable(
    pins: frozenset[str],
    registry: ToolRegistry,
    tier: TrustTier,
    guild_id: str | None = None,
) -> frozenset[str]:
    """Keep pins searchable and visible at this tier in this guild."""
    available: set[str] = set()
    for name in sorted(pins):
        if registry.get_searchable_entry(name, tier, guild_id) is not None:
            available.add(name)
        else:
            log.debug("Dropping configured pin %r: not searchable at tier %s", name, tier)
    return frozenset(available)
