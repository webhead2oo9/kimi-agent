"""Per-guild operator config read from the guild fragment frontmatter.

``config/servers/<guild_id>.md`` frontmatter carries guild-wide settings that
mirror the per-channel fragment vocabulary (``config/fragments/channel_pins.py``)::

    ---
    pinned_tools: [discord_text_search, build_discord_embed]
    staff_user_ids: [700000000000000101]
    staff_role_ids: [700000000000000102]
    regular_role_ids: [700000000000000103, 700000000000000104]
    ---
    You are helping the <Guild Name> community. House rules: ...

The body is the ``<server_instructions>`` slot (see ``config/fragments/prompt.py``'s
``load_fragment``, which strips this frontmatter). Several guild-wide
affordances ride the frontmatter:

* ``pinned_tools``: guild-wide searchable-tool pins that form the *base* set
  channel pins union onto, so a guild grants a tool surface once instead of
  editing every channel fragment. Privilege is never widened: a pin that is not
  a registered searchable tool or sits above the speaker's tier is dropped, and
  the registry re-checks tier at dispatch.
* ``blocked_tools``: a guild-wide denylist (the counterpart of ``pinned_tools``)
  that *removes* otherwise-available tools in this guild. Channel ``blocked_tools``
  union onto it. Listed tools are hidden from the model's tool list and the
  browse_tools catalog and rejected (masked as "Unknown tool") at dispatch. It is
  the only way to subtract a globally-registered tool from one guild; ``guild_ids``
  only scopes a tool *to* guilds.
* ``staff_user_ids`` / ``staff_role_ids`` / ``regular_role_ids``: per-guild
  trust lists that ``TrustResolver`` merges (OR) with the global allowlists.
  They only *add* local standing; the global lists stay the bot-wide backstop.
* ``thread_targets``: the channels ``move_to_thread`` may open a thread in
  besides the one it was asked in. Opt-in per guild; see
  :func:`load_guild_thread_targets` and ``docs/thread-handoff.md``.
* ``learn_log_channel_id``: where staff-taught knowledge (``teach``,
  ``skill_create``, ``skill_edit``) is announced, so an ephemeral confirmation
  still leaves a shared audit trail. Absent means no learn logging; see
  :func:`load_learn_log_channel_id` and ``app/learn_log.py``.
* ``proposal_channel_id``: where module-authored configuration proposals are
  reviewed by staff. Absent means the invoking channel is used; see
  :func:`load_proposal_channel_id` and ``app/proposals.py``.
* Application modules may own additional keys. Their validators join
  :func:`server_setup_activation` while the module is active, so a malformed
  module security boundary cannot activate the guild.

Read fresh each turn like the channel fragment, so operator edits take effect on
the next turn without a restart. This is trusted operator config; the
filesystem is the trust line.

Extension packages that keep their own keys in the same frontmatter read it through the public
:func:`read_guild_frontmatter` helper and own their parsing, keeping this
module feature-agnostic.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from config.fragments.channel_pins import (
    parse_blocked_tools,
    parse_pinned_tools,
    parse_tristate,
)
from utils.frontmatter import split_frontmatter
from config import paths
from trust.resolver import EMPTY_GUILD_TRUST, GuildTrust

log = logging.getLogger(__name__)

_ID_RE = re.compile(r"^[0-9]+$")  # Discord snowflakes


GuildConfigValidator = Callable[[Mapping[str, object]], bool]


def server_setup_activation(
    content: str,
    *,
    validators: Sequence[GuildConfigValidator] = (),
) -> bool | None:
    """Return a guild fragment's explicit activation decision, or fail closed.

    Activation is granted only when the fragment carries a literal boolean
    ``bot_active`` frontmatter key AND every known trust/list key is well-formed
    (a typo'd staff list must not silently activate a bot with the wrong trust
    boundaries). Anything else is ``None`` (no opinion / invalid): missing key,
    wrong type, malformed frontmatter, invalid sibling keys.
    """
    meta, _body = split_frontmatter(content)
    decision = meta.get("bot_active")
    if not isinstance(decision, bool):
        return None
    for key in (
        "staff_user_ids",
        "staff_role_ids",
        "regular_role_ids",
        "thread_targets",
    ):
        raw = meta.get(key)
        if raw is None:
            continue
        if not isinstance(raw, list) or any(not _ID_RE.match(str(entry).strip()) for entry in raw):
            return None
    for key in ("pinned_tools", "blocked_tools"):
        raw = meta.get(key)
        if raw is not None and not isinstance(raw, list):
            return None
    for key in ("learn_log_channel_id", "proposal_channel_id"):
        raw = meta.get(key)
        if raw is not None:
            token = str(raw).strip()
            if not _ID_RE.match(token) or int(token) <= 0:
                return None
    if any(not validator(meta) for validator in validators):
        return None
    return decision


def read_guild_frontmatter(
    guild_id: str,
    *,
    config_dir: Path | None = None,
) -> tuple[dict, str] | None:
    """Return ``(frontmatter, fragment_path)`` for a guild, or ``None``.

    ``None`` for a missing/invalid guild id or an unreadable file; the
    frontmatter dict is empty when the fragment has none. The generic read
    behind every loader here, public so feature packages can hot-read their own
    frontmatter keys without this module knowing about them.
    """
    if not guild_id or not _ID_RE.match(guild_id):
        return None
    fragment = (config_dir or paths.default_config_dir()) / "servers" / f"{guild_id}.md"
    try:
        text = fragment.read_text(encoding="utf-8")
    except FileNotFoundError, OSError:
        return None
    meta, _body = split_frontmatter(text)
    return meta, str(fragment)


def parse_id_list(raw: object, *, field: str, source: str) -> frozenset[str]:
    """Validate a frontmatter list of numeric Discord IDs into a string set."""
    if raw is None:
        return frozenset()
    if not isinstance(raw, list):
        log.warning("Ignoring non-list %s in %s", field, source)
        return frozenset()
    ids: set[str] = set()
    for entry in raw:
        token = str(entry).strip()
        if _ID_RE.match(token):
            ids.add(token)
        else:
            log.warning("Dropping non-numeric %s entry %r in %s", field, entry, source)
    return frozenset(ids)


def load_guild_pinned_tools(
    guild_id: str,
    *,
    config_dir: Path | None = None,
) -> frozenset[str]:
    """Read guild-wide ``pinned_tools`` from the guild fragment's frontmatter.

    Returns an empty set for a missing/invalid guild id, an unreadable file, or
    absent/malformed frontmatter. These pins are the base set channel pins union
    onto during turn preparation.
    """
    result = read_guild_frontmatter(guild_id, config_dir=config_dir)
    if result is None:
        return frozenset()
    meta, source = result
    return parse_pinned_tools(meta.get("pinned_tools"), source=source)


def load_guild_blocked_tools(
    guild_id: str,
    *,
    config_dir: Path | None = None,
) -> frozenset[str]:
    """Read guild-wide ``blocked_tools`` from the guild fragment's frontmatter.

    The denylist counterpart of :func:`load_guild_pinned_tools`. These names are
    the base denylist that channel ``blocked_tools`` union onto during turn
    preparation; the registry hides and rejects them this turn. Returns an empty
    set for a missing/invalid guild id, an unreadable file, or absent/malformed
    frontmatter.
    """
    result = read_guild_frontmatter(guild_id, config_dir=config_dir)
    if result is None:
        return frozenset()
    meta, source = result
    return parse_blocked_tools(meta.get("blocked_tools"), source=source)


def load_guild_thread_handoff(
    guild_id: str,
    *,
    config_dir: Path | None = None,
) -> bool | None:
    """Read the guild-wide ``thread_handoff`` default from the guild fragment.

    ``False`` turns thread handoff off across the guild unless a channel
    fragment sets ``thread_handoff: true``; ``None`` (absent/malformed) keeps
    the deployment default (on). See ``config/fragments/channel_pins.py:resolve_tristate``
    for the precedence.
    """
    result = read_guild_frontmatter(guild_id, config_dir=config_dir)
    if result is None:
        return None
    meta, _source = result
    return parse_tristate(meta.get("thread_handoff"))


def load_guild_thread_auto_respond(
    guild_id: str,
    *,
    config_dir: Path | None = None,
) -> bool | None:
    """Read the guild-wide ``thread_auto_respond`` default from the guild fragment.

    ``False`` makes threads the bot opens anywhere in the guild start paused
    (mention-only) unless a channel fragment sets ``thread_auto_respond: true``;
    ``None`` (absent/malformed) keeps the deployment default (on).
    """
    result = read_guild_frontmatter(guild_id, config_dir=config_dir)
    if result is None:
        return None
    meta, _source = result
    return parse_tristate(meta.get("thread_auto_respond"))


def load_guild_thread_targets(
    guild_id: str,
    *,
    config_dir: Path | None = None,
) -> frozenset[str]:
    """Read the guild's ``thread_targets`` allowlist from the guild fragment.

    The channels ``move_to_thread`` may open a thread in *other than the one it
    was asked in*. Absent or empty means cross-channel thread creation is off
    here: it is opt-in per community rather than a deployment-wide capability,
    because it is the one thread affordance that puts the bot's voice in a
    channel nobody in this conversation is looking at.

    This loader only parses ids. Whether a listed channel can actually be used
    is decided at resolution time (``app/thread_handoff_boundary.py``), which drops forums,
    channels the asker or the bot cannot post in, and channels whose own
    ``thread_handoff`` is off.
    """
    result = read_guild_frontmatter(guild_id, config_dir=config_dir)
    if result is None:
        return frozenset()
    meta, source = result
    return parse_id_list(meta.get("thread_targets"), field="thread_targets", source=source)


def load_learn_log_channel_id(
    guild_id: str,
    *,
    config_dir: Path | None = None,
) -> str | None:
    """Read the guild's ``learn_log_channel_id`` from the guild fragment.

    Fails closed: ``None`` for a missing/invalid guild id, an unreadable file,
    or an absent/malformed value means this guild simply gets no learn log.
    Staff-taught knowledge is confirmed ephemerally, so this channel is the only
    shared record that something entered community memory or a skill.
    """
    result = read_guild_frontmatter(guild_id, config_dir=config_dir)
    if result is None:
        return None
    meta, source = result
    raw = meta.get("learn_log_channel_id")
    if raw is None:
        return None
    token = str(raw).strip()
    if not _ID_RE.match(token):
        log.warning("Ignoring non-numeric learn_log_channel_id in %s", source)
        return None
    return token


def load_proposal_channel_id(
    guild_id: str,
    *,
    config_dir: Path | None = None,
) -> str | None:
    """Read the guild's optional staff proposal-review channel.

    Channel ownership and sendability are checked by the proposal service at
    use time. This loader only accepts a positive numeric Discord ID.
    """
    result = read_guild_frontmatter(guild_id, config_dir=config_dir)
    if result is None:
        return None
    meta, source = result
    raw = meta.get("proposal_channel_id")
    if raw is None:
        return None
    token = str(raw).strip()
    if not _ID_RE.match(token) or int(token) <= 0:
        log.warning("Ignoring non-numeric proposal_channel_id in %s", source)
        return None
    return token


def proposal_channel_id_is_configured(
    guild_id: str,
    *,
    config_dir: Path | None = None,
) -> bool:
    """Return whether the guild fragment declares ``proposal_channel_id``.

    This deliberately distinguishes an absent key from a present but malformed
    value so proposal routing can fail closed instead of silently falling back
    to the invoking channel.
    """
    result = read_guild_frontmatter(guild_id, config_dir=config_dir)
    return result is not None and "proposal_channel_id" in result[0]


def load_guild_trust(
    guild_id: str,
    *,
    config_dir: Path | None = None,
) -> GuildTrust:
    """Read per-guild trust lists from the guild fragment's frontmatter.

    Returns ``EMPTY_GUILD_TRUST`` for a missing/invalid guild id, an unreadable
    file, or absent/malformed frontmatter. The lists are additive: see
    ``trust/resolver.py``.
    """
    result = read_guild_frontmatter(guild_id, config_dir=config_dir)
    if result is None:
        return EMPTY_GUILD_TRUST
    meta, source = result
    trust = GuildTrust(
        staff_user_ids=parse_id_list(
            meta.get("staff_user_ids"), field="staff_user_ids", source=source
        ),
        staff_role_ids=parse_id_list(
            meta.get("staff_role_ids"), field="staff_role_ids", source=source
        ),
        regular_role_ids=parse_id_list(
            meta.get("regular_role_ids"), field="regular_role_ids", source=source
        ),
    )
    return EMPTY_GUILD_TRUST if trust.is_empty else trust
