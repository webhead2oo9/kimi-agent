"""Settings loaded from channel-fragment frontmatter.

Includes user/role admission, tool pins and blocks, automatic thread thresholds,
and thread-mode overrides. Admission and blocked tools fail closed; pins remain
subject to registry permissions.
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
_blocked_cache: LastKnownGoodCache[frozenset[str]] = LastKnownGoodCache()


class ChannelBlockedToolsLoadError(RuntimeError):
    """A present channel denylist could not be loaded safely."""


@dataclass(frozen=True, slots=True)
class ChannelAccessPolicy:
    """A channel's optional user/role admission allowlist.

    ``configured`` records key presence independently from parsed entries. That
    distinction makes an explicit empty or malformed allowlist deny everyone,
    while a fragment with neither key preserves the existing unrestricted
    behavior.
    """

    configured: bool = False
    allowed_user_ids: frozenset[str] = frozenset()
    allowed_role_ids: frozenset[str] = frozenset()

    def allows(self, user: object) -> bool:
        if not self.configured:
            return True
        user_id = getattr(user, "id", None)
        if user_id is not None and str(user_id) in self.allowed_user_ids:
            return True
        role_ids = {
            str(role_id)
            for role in (getattr(user, "roles", None) or ())
            if (role_id := getattr(role, "id", None)) is not None
        }
        return bool(role_ids & self.allowed_role_ids)


def _parse_numeric_id_list(raw: object, *, source: str, field: str) -> frozenset[str]:
    """Keep numeric Discord IDs and drop malformed entries, matching legacy semantics."""
    if not isinstance(raw, list):
        log.warning("Ignoring non-list %s in %s", field, source)
        return frozenset()
    ids: set[str] = set()
    for entry in raw:
        token = str(entry).strip()
        if not isinstance(entry, bool) and _ID_RE.fullmatch(token):
            ids.add(token)
        else:
            log.warning("Dropping non-numeric %s entry %r in %s", field, entry, source)
    return frozenset(ids)


def load_channel_access_policy(
    channel_id: str,
    *,
    config_dir: Path | None = None,
) -> ChannelAccessPolicy:
    """Hot-read a channel policy, denying when an existing fragment is invalid.

    Valid mixed lists retain their valid IDs and drop invalid entries, as the
    legacy parser did. A genuinely missing fragment follows the existing
    unrestricted convention. Existing fragments are parsed only by the shared
    strict safe parser; unreadable, invalid, nonmapping, and multiple-document
    policies deny all for the current invocation and are never reconstructed.
    """
    if not channel_id or not _ID_RE.fullmatch(channel_id):
        return ChannelAccessPolicy(configured=True)
    fragment = (config_dir or paths.default_config_dir()) / "channels" / f"{channel_id}.md"
    try:
        text = fragment.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ChannelAccessPolicy()
    except (OSError, UnicodeError) as exc:
        log.error("Could not load channel admission policy %s (%s); denying access", fragment, exc)
        return ChannelAccessPolicy(configured=True)

    try:
        meta, _body = split_frontmatter_strict(text)
    except ValueError as exc:
        log.error("Could not parse channel admission policy %s (%s); denying access", fragment, exc)
        return ChannelAccessPolicy(configured=True)

    configured = "allowed_user_ids" in meta or "allowed_role_ids" in meta
    return ChannelAccessPolicy(
        configured=configured,
        allowed_user_ids=(
            _parse_numeric_id_list(
                meta["allowed_user_ids"], source=str(fragment), field="allowed_user_ids"
            )
            if "allowed_user_ids" in meta
            else frozenset()
        ),
        allowed_role_ids=(
            _parse_numeric_id_list(
                meta["allowed_role_ids"], source=str(fragment), field="allowed_role_ids"
            )
            if "allowed_role_ids" in meta
            else frozenset()
        ),
    )


def channel_access_scope_id(channel: object) -> str:
    """Return a thread's parent ID, or a non-thread channel's own ID.

    A present-but-empty ``parent_id`` is treated as missing thread evidence and
    fails closed at :func:`channel_access_allowed` instead of falling back to
    the thread ID.
    """
    missing = object()
    parent_id = getattr(channel, "parent_id", missing)
    if parent_id is not missing:
        return str(parent_id) if parent_id else ""
    channel_id = getattr(channel, "id", None)
    return str(channel_id) if channel_id is not None else ""


def channel_access_allowed(
    channel: object,
    user: object,
    *,
    config_dir: Path | None = None,
) -> bool:
    """Apply the reusable parent-channel user/role admission check."""
    channel_id = channel_access_scope_id(channel)
    if not channel_id:
        return False
    return load_channel_access_policy(channel_id, config_dir=config_dir).allows(user)


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
    except FileNotFoundError:
        _blocked_cache.forget(key)
        return frozenset()
    except (OSError, UnicodeError) as exc:
        return _retain_channel_blocked_tools(fragment, key, exc)

    try:
        meta, _body = split_frontmatter_strict(text)
        if "blocked_tools" not in meta:
            blocked: frozenset[str] = frozenset()
            _blocked_cache.remember(key, blocked)
            return blocked
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
