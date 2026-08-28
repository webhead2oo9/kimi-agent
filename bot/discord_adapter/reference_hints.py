"""Parse and permission-check Discord references from one triggering message."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Literal

import discord

from agent.discord_references import (
    DiscordReferenceHint,
    ResolvedDiscordReferenceHint,
    UnresolvedDiscordReferenceHint,
)

log = logging.getLogger(__name__)

MAX_DISCORD_REFERENCES_PER_TURN = 5

_LINK_RE = re.compile(
    r"https?://(?:(?:canary|ptb)\.)?discord(?:app)?\.com/channels/"
    r"(?P<guild>@me|[0-9]{1,20})/(?P<channel>[0-9]{1,20})"
    r"(?:/(?P<message>[0-9]{1,20})(?![0-9]))?(?![0-9])",
    re.IGNORECASE,
)
_CHANNEL_MENTION_RE = re.compile(r"<\#(?P<channel>[0-9]{1,20})>")
_OTHER_DISCORD_MARKUP_RE = re.compile(
    r"<(?:@!?|@&)[0-9]{1,20}>"
    r"|<(?:a?):[^>\r\n]{1,100}:[0-9]{1,20}>"
    r"|</[^>\r\n]{1,100}:[0-9]{1,20}>"
)
_BARE_CHANNEL_ID_RE = re.compile(r"(?<![0-9])(?P<channel>[0-9]{15,20})(?![0-9])")

_THREAD_TYPES = frozenset(
    {
        discord.ChannelType.news_thread,
        discord.ChannelType.public_thread,
        discord.ChannelType.private_thread,
    }
)


@dataclass(frozen=True)
class _Reference:
    source: Literal["message_link", "channel_link", "channel_mention", "channel_id"]
    channel_id: str
    start: int
    end: int
    guild_id: str | None = None
    message_id: str | None = None

    @property
    def explicit(self) -> bool:
        return self.source != "channel_id"


def parse_discord_references(content: str) -> tuple[_Reference, ...]:
    """Parse supported references in source order without double-counting link IDs."""

    references: list[_Reference] = []
    occupied: list[tuple[int, int]] = []
    for match in _LINK_RE.finditer(content):
        message_id = match.group("message")
        references.append(
            _Reference(
                source="message_link" if message_id else "channel_link",
                guild_id=match.group("guild"),
                channel_id=match.group("channel"),
                message_id=message_id,
                start=match.start(),
                end=match.end(),
            )
        )
        occupied.append(match.span())

    for match in _CHANNEL_MENTION_RE.finditer(content):
        if _overlaps(match.span(), occupied):
            continue
        references.append(
            _Reference(
                source="channel_mention",
                channel_id=match.group("channel"),
                start=match.start(),
                end=match.end(),
            )
        )
        occupied.append(match.span())

    occupied.extend(match.span() for match in _OTHER_DISCORD_MARKUP_RE.finditer(content))
    for match in _BARE_CHANNEL_ID_RE.finditer(content):
        if _overlaps(match.span(), occupied):
            continue
        references.append(
            _Reference(
                source="channel_id",
                channel_id=match.group("channel"),
                start=match.start(),
                end=match.end(),
            )
        )

    references.sort(key=lambda item: item.start)
    deduped: list[_Reference] = []
    seen: set[tuple[str, str, str | None, str | None]] = set()
    for reference in references:
        key = (
            reference.source,
            reference.channel_id,
            reference.guild_id,
            reference.message_id,
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(reference)

    # Explicit links and channel mentions must not be starved by unrelated bare
    # snowflakes earlier in the message. Fill any remaining budget with bare IDs,
    # then restore source order for model-facing hints.
    explicit = [reference for reference in deduped if reference.explicit]
    bare = [reference for reference in deduped if not reference.explicit]
    selected = explicit[:MAX_DISCORD_REFERENCES_PER_TURN]
    selected.extend(bare[: MAX_DISCORD_REFERENCES_PER_TURN - len(selected)])
    selected.sort(key=lambda item: item.start)
    return tuple(selected)


async def resolve_discord_reference_hints(
    source_message: object,
    content: str,
    *,
    excluded_channel_ids: frozenset[str] = frozenset(),
) -> tuple[ResolvedDiscordReferenceHint, ...]:
    """Resolve references against the source guild while revealing no denied metadata."""

    references = parse_discord_references(content)
    if not references:
        return ()
    guild = getattr(source_message, "guild", None)
    member = getattr(source_message, "author", None)
    bot_member = getattr(guild, "me", None)
    guild_id = str(getattr(guild, "id", "") or "")
    if not guild_id or member is None or bot_member is None:
        return ()

    hints: list[ResolvedDiscordReferenceHint] = []
    unresolved_added = False
    for reference in references:
        try:
            hint = await _resolve_reference(
                reference,
                guild=guild,
                guild_id=guild_id,
                member=member,
                bot_member=bot_member,
                excluded_channel_ids=excluded_channel_ids,
            )
        except Exception:
            log.debug("Could not resolve Discord reference hint", exc_info=True)
            hint = None
        if hint is not None:
            hints.append(hint)
        elif reference.explicit and not unresolved_added:
            hints.append(UnresolvedDiscordReferenceHint())
            unresolved_added = True
    return tuple(hints)


async def _resolve_reference(
    reference: _Reference,
    *,
    guild: object,
    guild_id: str,
    member: object,
    bot_member: object,
    excluded_channel_ids: frozenset[str],
) -> DiscordReferenceHint | None:
    if reference.guild_id is not None and reference.guild_id != guild_id:
        return None
    channel = await _resolve_channel(guild, reference)
    if (
        channel is None
        or str(getattr(channel, "id", "") or "") != reference.channel_id
        or str(getattr(getattr(channel, "guild", None), "id", "") or "") != guild_id
        or _channel_is_excluded(channel, excluded_channel_ids)
    ):
        return None

    require_history = reference.source == "message_link"
    if not await _channel_accessible(
        channel,
        member,
        bot_member,
        require_history=require_history,
    ):
        return None

    parent_name, category_name, has_category = await _visible_location_metadata(
        channel,
        member,
        bot_member,
    )
    channel_type = getattr(channel, "type", None)
    channel_kind: Literal["channel", "thread", "category"] = "channel"
    if channel_type in _THREAD_TYPES:
        channel_kind = "thread"
    elif channel_type is discord.ChannelType.category:
        channel_kind = "category"

    author_name: str | None = None
    message_text: str | None = None
    if reference.message_id is not None:
        fetch_message = getattr(channel, "fetch_message", None)
        if not callable(fetch_message):
            return None
        try:
            linked_message = await fetch_message(int(reference.message_id))
        except discord.Forbidden, discord.NotFound, discord.HTTPException:
            return None
        if str(getattr(linked_message, "id", "") or "") != reference.message_id:
            return None
        author = getattr(linked_message, "author", None)
        author_name = str(
            getattr(author, "display_name", None) or getattr(author, "name", None) or "Unknown"
        )
        message_text = str(getattr(linked_message, "content", "") or "")

    return DiscordReferenceHint(
        source=reference.source,
        channel_id=reference.channel_id,
        channel_name=str(getattr(channel, "name", "") or "unknown"),
        channel_kind=channel_kind,
        parent_channel_name=parent_name,
        category_name=category_name,
        has_category=has_category,
        author_name=author_name,
        message_text=message_text,
    )


async def _resolve_channel(guild: object, reference: _Reference) -> object | None:
    getter = getattr(guild, "get_channel_or_thread", None)
    channel = getter(int(reference.channel_id)) if callable(getter) else None
    if channel is not None or reference.source == "channel_id":
        return channel
    fetch_channel = getattr(guild, "fetch_channel", None)
    if not callable(fetch_channel):
        return None
    try:
        return await fetch_channel(int(reference.channel_id))
    except discord.Forbidden, discord.NotFound, discord.InvalidData, discord.HTTPException:
        return None


async def _channel_accessible(
    channel: object,
    member: object,
    bot_member: object,
    *,
    require_history: bool,
) -> bool:
    permissions_for = getattr(channel, "permissions_for", None)
    if not callable(permissions_for):
        return False
    try:
        member_permissions = permissions_for(member)
        bot_permissions = permissions_for(bot_member)
    except AttributeError, TypeError:
        return False
    for permissions in (member_permissions, bot_permissions):
        if not bool(getattr(permissions, "view_channel", False)):
            return False
        if require_history and not bool(getattr(permissions, "read_message_history", False)):
            return False

    if getattr(channel, "type", None) is not discord.ChannelType.private_thread:
        return True
    for actor, permissions in (
        (member, member_permissions),
        (bot_member, bot_permissions),
    ):
        if bool(getattr(permissions, "manage_threads", False)):
            continue
        if not await _private_thread_has_member(channel, actor):
            return False
    return True


async def _private_thread_has_member(channel: object, member: object) -> bool:
    member_id = getattr(member, "id", None)
    fetch_member = getattr(channel, "fetch_member", None)
    if member_id is None or not callable(fetch_member):
        return False
    try:
        await fetch_member(int(member_id))
        return True
    except discord.Forbidden, discord.NotFound, discord.HTTPException:
        return False


async def _visible_location_metadata(
    channel: object,
    member: object,
    bot_member: object,
) -> tuple[str | None, str | None, bool]:
    parent = (
        getattr(channel, "parent", None)
        if getattr(channel, "type", None) in _THREAD_TYPES
        else None
    )
    parent_name = None
    parent_visible = parent is not None and await _channel_accessible(
        parent, member, bot_member, require_history=False
    )
    if parent_visible:
        parent_name = str(getattr(parent, "name", "") or "") or None

    parent_category = getattr(parent, "category", None) if parent is not None else None
    direct_category = getattr(channel, "category", None) if parent is None else None
    has_category = parent_category is not None or direct_category is not None
    category = parent_category if parent_visible else direct_category
    category_name = None
    if category is not None and await _channel_accessible(
        category,
        member,
        bot_member,
        require_history=False,
    ):
        category_name = str(getattr(category, "name", "") or "") or None
    return parent_name, category_name, has_category


def _channel_is_excluded(channel: object, excluded_channel_ids: frozenset[str]) -> bool:
    channel_id = str(getattr(channel, "id", "") or "")
    parent_id = str(getattr(channel, "parent_id", "") or "")
    return channel_id in excluded_channel_ids or (
        getattr(channel, "type", None) in _THREAD_TYPES and parent_id in excluded_channel_ids
    )


def _overlaps(span: tuple[int, int], occupied: list[tuple[int, int]]) -> bool:
    start, end = span
    return any(start < other_end and end > other_start for other_start, other_end in occupied)
