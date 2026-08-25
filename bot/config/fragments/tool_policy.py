"""Deployment-wide tool policy: the global ``blocked_tools`` denylist.

The per-channel (``config/fragments/channel_pins.py``) and per-guild
(``config/fragments/guild_config.py``) denylists subtract tools from one place. This is the
third and broadest scope, a single fragment at ``<config_dir>/tools.md`` whose
frontmatter removes a tool from **every** guild and channel::

    ---
    blocked_tools: [teach, build_discord_embed]
    ---

The three scopes union in ``app/turn_entry.py``, and the result rides the turn on
``MessageContext.blocked_tools``, where the registry hides the names from the
model's tool list and the browse_tools catalog and masks them as "Unknown tool"
at dispatch.

This exists because the capability gates in ``config/settings.py`` are read once
at boot: turning a tool off through ``.env`` needs a restart, and ``guild_ids``
scopes a tool *to* guilds rather than away from them. A fragment read fresh each
turn is what lets the operator toggle a tool off without a restart.

The file is trusted operator config and optional: a genuinely absent file means
"nothing blocked globally". A present file is parsed strictly. When a reload
fails, the loader retains the last valid policy for that exact path; without a
last-known-good value it raises :class:`ToolPolicyLoadError` so corruption
cannot silently widen the tool surface.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Collection
from pathlib import Path


from config.fragments.channel_pins import load_channel_blocked_tools, resolve_tristate
from config.fragments.guild_config import load_guild_blocked_tools
from config.fragments._fragment_cache import LastKnownGoodCache
from config import paths
from utils.frontmatter import split_frontmatter_strict

log = logging.getLogger(__name__)

TOOL_POLICY_FILENAME = "tools.md"

_TOOL_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_MAX_BLOCKED_TOOLS = 64

_cache: LastKnownGoodCache[frozenset[str]] = LastKnownGoodCache()


class ToolPolicyLoadError(RuntimeError):
    """A present global tool policy could not be loaded safely."""


_policy_cache_key = LastKnownGoodCache.key
_remember_policy = _cache.remember
_forget_policy = _cache.forget
_last_policy = _cache.last_good


def _parse_policy(text: str) -> frozenset[str]:
    """Parse one policy without conflating invalid content with an empty policy.

    Strict on purpose: the caller turns a raised error into last-known-good, so
    a malformed fragment must not read as "nothing blocked". Body-only and
    empty fragments are valid policies with no blocked tools.
    """
    meta, _ = split_frontmatter_strict(text)

    if "blocked_tools" not in meta:
        return frozenset()
    raw_blocked = meta["blocked_tools"]
    if not isinstance(raw_blocked, list):
        raise ValueError("blocked_tools must be a list")
    if len(raw_blocked) > _MAX_BLOCKED_TOOLS:
        raise ValueError(f"blocked_tools is capped at {_MAX_BLOCKED_TOOLS} entries")
    for entry in raw_blocked:
        if not isinstance(entry, str) or not _TOOL_NAME_RE.fullmatch(entry):
            raise ValueError(f"invalid blocked_tools entry: {entry!r}")
    return frozenset(raw_blocked)


def _retain_or_raise(
    fragment: Path,
    key: Path,
    error: BaseException,
) -> frozenset[str]:
    last_good = _last_policy(key)
    if last_good is not None:
        log.error(
            "Could not reload global tool policy %s (%s); retaining last-known-good denylist",
            fragment,
            error,
        )
        return last_good
    raise ToolPolicyLoadError(f"Could not load global tool policy {fragment}: {error}") from error


def global_tool_policy_path(config_dir: Path | None = None) -> Path:
    """Where the deployment-wide policy fragment lives."""
    return (config_dir or paths.default_config_dir()) / TOOL_POLICY_FILENAME


def load_global_blocked_tools(*, config_dir: Path | None = None) -> frozenset[str]:
    """Read the deployment-wide denylist, retaining it across failed reloads.

    A missing file is an explicit empty policy and clears any cached value for
    that path. A present but unreadable or invalid file uses that path's
    last-known-good value. If none exists, :class:`ToolPolicyLoadError` stops
    the caller rather than silently granting tools.
    """
    fragment = global_tool_policy_path(config_dir)
    key = _policy_cache_key(fragment)
    try:
        text = fragment.read_text(encoding="utf-8")
    except FileNotFoundError:
        _forget_policy(key)
        return frozenset()
    except (OSError, UnicodeError) as exc:
        return _retain_or_raise(fragment, key, exc)

    try:
        blocked = _parse_policy(text)
    except ValueError as exc:
        return _retain_or_raise(fragment, key, exc)
    _remember_policy(key, blocked)
    return blocked


def load_blocked_tools(
    guild_id: str,
    channel_id: str,
    *,
    load_global: Callable[[], frozenset[str]] = load_global_blocked_tools,
    load_guild: Callable[[str], frozenset[str]] = load_guild_blocked_tools,
    load_channel: Callable[[str], frozenset[str]] = load_channel_blocked_tools,
) -> frozenset[str]:
    """The deployment ∪ guild ∪ channel denylist for one scope.

    Both places that need the merged denylist go through here: the turn path
    (``app/turn_entry.py``, passing its injectable hooks) and the thread-creation
    gate (``app/thread_handoff_boundary.py``, using the defaults). One function is what keeps the
    two from drifting apart, and it means a test can reach the creation gate
    through the same seam the turn path uses.
    """
    return load_global() | load_guild(guild_id) | load_channel(channel_id)


def thread_handoff_creation_allowed(
    blocked_tools: Collection[str],
    *,
    channel: bool | None,
    guild: bool | None,
) -> bool:
    """Whether policy permits creating a new managed thread.

    The tri-state handoff switch keeps its channel-over-guild precedence, while
    an explicit ``move_to_thread`` deny at any loaded scope always wins. Closing
    an existing managed thread is intentionally independent: callers must not
    infer a block for ``leave_thread`` from this creation gate.
    """
    return "move_to_thread" not in blocked_tools and resolve_tristate(channel, guild)


# The thread-state tools. They are core rather than searchable, because "stop
# replying to everything" has to work on the first ask instead of waiting for a
# browse_tools round trip. This mask, not the search pool, is therefore what
# keeps them out of every turn that has no thread to act on.
THREAD_STATE_TOOLS = frozenset({"leave_thread", "pause_thread_replies", "resume_thread_replies"})


def thread_state_blocked_tools(*, managed: bool, auto_responding: bool) -> frozenset[str]:
    """Which thread-state tools this turn must hide.

    Outside a thread the bot manages, all of them: there is nothing to pause,
    resume or close. Inside one, whichever of pause/resume does not apply, so
    exactly one of the pair is ever offered and the model cannot try to resume a
    thread that was never paused.
    """
    if not managed:
        return THREAD_STATE_TOOLS
    return frozenset({"resume_thread_replies" if auto_responding else "pause_thread_replies"})
