"""Per-channel pinned searchable tools.

Operators can pre-activate searchable tools in a channel by declaring them in
YAML frontmatter at the top of that channel's prompt fragment
(``config/channels/<channel_id>.md``)::

    ---
    pinned_tools: [discord_text_search]
    ---
    You are in #off-topic, for casual chat (not support).

Pinned names are merged into the turn's activated-tool set during turn
preparation, so the tools are visible and dispatchable without ``browse_tools``.
They are never written to ``conversation_activated_tools``: the fragment file
stays the single source of truth and unpinning takes effect on the next turn.
Pinning never widens privileges: a pinned name that is not a registered
searchable tool (for example, behind an unset config gate) or that sits above
the speaker's trust tier is dropped at lookup time, and the registry re-checks
tier at dispatch regardless.

The fragment file is trusted operator config, read fresh each turn like the
prompt templates. The same frontmatter also carries per-channel auto-handoff
enrollment (``auto_thread_always`` or the ``auto_thread_min_lines`` /
``auto_thread_min_chars`` thresholds) and the two tri-state thread switches
(``thread_handoff``, ``thread_auto_respond``); see ``docs/thread-handoff.md``.
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

_ID_RE = re.compile(r"^[0-9]+$")  # Discord snowflakes
_TOOL_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_MAX_PINS = 16
_MAX_BLOCKED = 64
_blocked_cache: LastKnownGoodCache[frozenset[str]] = LastKnownGoodCache()


class ChannelBlockedToolsLoadError(RuntimeError):
    """A present channel denylist could not be loaded safely."""


def _read_channel_frontmatter(
    channel_id: str,
    *,
    config_dir: Path | None = None,
) -> tuple[dict, str] | None:
    """Return ``(frontmatter, fragment_path)`` for a channel, or ``None``.

    ``None`` for a missing/invalid channel id or an unreadable file; the
    frontmatter dict is empty when the fragment has none. The generic read behind
    every loader here, mirroring ``config/fragments/guild_config.py:read_guild_frontmatter``
    so a new channel-scoped key is three lines rather than another copy of the
    id-check/read/parse sequence.
    """
    if not channel_id or not _ID_RE.match(channel_id):
        return None
    fragment = (config_dir or paths.default_config_dir()) / "channels" / f"{channel_id}.md"
    try:
        text = fragment.read_text(encoding="utf-8")
    except FileNotFoundError, OSError:
        return None
    meta, _body = split_frontmatter(text)
    return meta, str(fragment)


def _parse_tool_name_list(raw: object, *, source: str, field: str, limit: int) -> frozenset[str]:
    """Validate a frontmatter list of tool names into a name set.

    Returns an empty set when ``raw`` is absent or not a list; drops entries
    that are not plausible tool names; caps the result at ``limit``. ``field``
    and ``source`` label the frontmatter key and fragment in warnings.
    """
    if not isinstance(raw, list):
        if raw is not None:
            log.warning("Ignoring non-list %s in %s", field, source)
        return frozenset()
    names = [name for name in raw if isinstance(name, str) and _TOOL_NAME_RE.match(name)]
    if len(names) > limit:
        log.warning("%s lists %d %s; keeping the first %d", source, len(names), field, limit)
        names = names[:limit]
    return frozenset(names)


def parse_pinned_tools(raw: object, *, source: str) -> frozenset[str]:
    """Validate a frontmatter ``pinned_tools`` value into a name set.

    Shared by the channel and guild fragment loaders. Returns an empty set when
    ``raw`` is absent or not a list; drops entries that are not plausible tool
    names; caps the result at ``_MAX_PINS``. ``source`` labels the fragment in
    warnings.
    """
    return _parse_tool_name_list(raw, source=source, field="pinned_tools", limit=_MAX_PINS)


def parse_blocked_tools(raw: object, *, source: str) -> frozenset[str]:
    """Validate a frontmatter ``blocked_tools`` value into a name set.

    The denylist counterpart of :func:`parse_pinned_tools`, shared by the
    channel and guild fragment loaders. Same validation, a larger cap
    (``_MAX_BLOCKED``) so an operator can pare back a broad tool surface.
    """
    return _parse_tool_name_list(raw, source=source, field="blocked_tools", limit=_MAX_BLOCKED)


def load_channel_pinned_tools(
    channel_id: str,
    *,
    config_dir: Path | None = None,
) -> frozenset[str]:
    """Read ``pinned_tools`` from a channel fragment's frontmatter.

    Returns an empty set for a missing/invalid channel id, an unreadable file,
    absent or malformed frontmatter, or a ``pinned_tools`` value that is not a
    list. Entries that are not plausible tool names are dropped.
    """
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

    The denylist counterpart of :func:`load_channel_pinned_tools`. Names listed
    here are masked in this channel. A missing file explicitly clears the
    policy. A present but malformed or unreadable file retains the last valid
    value for that path; without one it raises instead of silently granting
    tools.
    """
    if not channel_id or not _ID_RE.match(channel_id):
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
        raw = meta.get("blocked_tools")
        if raw is None:
            blocked: frozenset[str] = frozenset()
        else:
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
    """Per-channel auto-handoff enrollment read from frontmatter.

    A ``None`` threshold means that dimension is not checked; the channel is
    enrolled in auto-handoff as long as at least one threshold is set or
    ``always`` is true (every reply moves, no length check).
    """

    min_lines: int | None
    min_chars: int | None
    always: bool = False


def _coerce_positive_int(value: object) -> int | None:
    """Return a positive int from a frontmatter scalar, else ``None``."""
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
    """Read auto-handoff enrollment from a channel fragment's frontmatter.

    ``auto_thread_always: true`` enrolls the channel with no length check;
    every reply moves to a thread. Otherwise enrollment needs at least one of
    ``auto_thread_min_lines`` / ``auto_thread_min_chars`` as a positive int.
    Returns ``None`` (channel not enrolled) for a missing/invalid channel id,
    an unreadable file, absent/malformed frontmatter, or when no key is set
    (a non-bool ``auto_thread_always`` is ignored, fail-closed).
    """
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
    """Tri-state frontmatter value: ``True``, ``False``, or "not set here".

    Only the literal booleans are honored; anything else (absent, strings,
    ints) is ``None``, so a typo falls back to the wider scope instead of
    silently flipping the channel. Shared by every tri-state fragment key
    (``thread_handoff``, ``thread_auto_respond``) at both channel and guild
    scope.
    """
    return raw if isinstance(raw, bool) else None


def load_channel_thread_handoff(
    channel_id: str,
    *,
    config_dir: Path | None = None,
) -> bool | None:
    """Read the tri-state ``thread_handoff`` key from a channel fragment.

    ``False`` disables thread handoff in this channel (the ``move_to_thread``
    tool is masked and auto-enrollment is ignored); ``True`` re-enables it over
    a guild-wide ``thread_handoff: false``; ``None`` (absent/malformed/no file)
    defers to the guild value, then the default (on). An explicit
    ``blocked_tools`` entry still wins over ``True``, because this key never
    removes names from the denylist.
    """
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
    """Read the tri-state ``thread_auto_respond`` key from a channel fragment.

    The *default mode* for threads the bot opens from this channel, not a switch
    over existing ones: ``False`` means a new thread starts paused (mention-only)
    instead of answering every message. ``None`` defers to the guild value, then
    the default (on). A model-supplied ``auto_reply`` on ``move_to_thread`` wins
    over this, and anyone in a thread can change its mode afterwards.
    """
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
    """Keep only pins that are registered searchable tools visible at ``tier``
    in ``guild_id`` (a guild-scoped tool pinned outside its guild is dropped)."""
    available: set[str] = set()
    for name in sorted(pins):
        if registry.get_searchable_entry(name, tier, guild_id) is not None:
            available.add(name)
        else:
            log.debug("Dropping channel pin %r: not a searchable tool at tier %s", name, tier)
    return frozenset(available)
