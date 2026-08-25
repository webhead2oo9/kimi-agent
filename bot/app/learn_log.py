"""The learn audit log: staff-taught knowledge in, log-channel cards out.

Teaching is confirmed to the staff member who did it (ephemerally, when it came
from the bot-name-derived teaching context menu), so without this feed a community would
have no shared record of what its bot was told. Every write into shared guild
knowledge (``teach`` into community memory, ``skill_create``/``skill_edit`` into
a skill document) emits a :class:`~tools.learn.LearnEvent`; this is the only
place that turns one into a Discord post.

Fails closed and quietly: a guild with no ``learn_log_channel_id`` gets no log,
and a posting failure is swallowed rather than propagated back into the tool
call that already committed the knowledge.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

import discord

from config.fragments.guild_config import load_learn_log_channel_id
from discord_adapter.io import send_response
from tools.embeds import DESCRIPTION_MAX, FIELD_VALUE_MAX, TOTAL_MAX, EmbedSpec
from tools.learn import SINK_COMMUNITY_MEMORY, SINK_SKILL, LearnEvent

log = logging.getLogger(__name__)

_SUMMARY_LIMIT = 900
_SUBJECT_LIMIT = 200

_COLOR_SKILL = 0x5865F2
_COLOR_MEMORY = 0x57F287

_TITLES = {
    (SINK_SKILL, "created"): "Skill created",
    (SINK_SKILL, "updated"): "Skill updated",
    (SINK_COMMUNITY_MEMORY, "taught"): "Community knowledge taught",
}


class LearnLogFeed:
    def __init__(
        self,
        *,
        get_bot: Callable[[], discord.Client | None],
        is_guild_active: Callable[[int], bool] | None = None,
    ) -> None:
        self._get_bot = get_bot
        self._is_guild_active = is_guild_active

    async def record(self, event: LearnEvent) -> None:
        """Post one learn event to its guild's log channel, if configured."""
        if event.guild_id is None:
            return
        try:
            guild_id = int(event.guild_id)
        except ValueError:
            return
        if self._is_guild_active is not None and not self._is_guild_active(guild_id):
            return
        channel_id = load_learn_log_channel_id(event.guild_id)
        if channel_id is None:
            return
        await self._post(channel_id, build_learn_log_embed(event))

    async def _post(self, channel_id: str, spec: EmbedSpec) -> None:
        bot = self._get_bot()
        if bot is None:
            return
        channel = bot.get_channel(int(channel_id))
        if channel is None:
            try:
                channel = await bot.fetch_channel(int(channel_id))
            except discord.NotFound, discord.Forbidden, discord.HTTPException:
                log.warning("Learn log channel %s is unreachable", channel_id)
                return
        if not isinstance(channel, discord.abc.Messageable):
            log.warning("Learn log channel %s is not messageable", channel_id)
            return
        try:
            await send_response(channel, "", embed=spec)
        except discord.Forbidden:
            log.warning("Cannot post to learn log channel %s", channel_id)
        except discord.HTTPException:
            log.exception("Failed to post a learn log card")


def build_learn_log_embed(event: LearnEvent) -> EmbedSpec:
    """Render a learn event as a log card.

    The taught content is quoted so a reviewer can judge it without opening the
    skill or querying memory, which is the point of the audit trail.

    Every part is bounded, and the whole card is bounded again at the end.
    ``discord_adapter.io.build_embed`` assigns these strings straight onto a
    ``discord.Embed`` with no truncation of its own, so an oversized card is
    rejected by Discord and swallowed by :meth:`LearnLogFeed._post`, losing
    exactly the audit record that a suspiciously large write most needs.
    """
    fields: list[tuple[str, str, bool]] = [
        ("Taught by", _truncate(f"<@{event.user_id}>", FIELD_VALUE_MAX), True),
    ]
    if event.scope:
        fields.append(("Shared with", _truncate(event.scope, FIELD_VALUE_MAX), True))
    if event.source_url:
        fields.append(("Source", _truncate(event.source_url, FIELD_VALUE_MAX), False))

    title = _TITLES.get((event.sink, event.action), "Knowledge updated")
    subject = _truncate(event.subject, _SUBJECT_LIMIT)
    if event.sink == SINK_SKILL:
        heading = f"**{subject}**"
        color = _COLOR_SKILL
    else:
        heading = f"Topic: **{subject}**"
        color = _COLOR_MEMORY

    description = heading
    if event.summary:
        description = f"{heading}\n\n{_truncate(event.summary, _SUMMARY_LIMIT)}"
    description = _truncate(description, DESCRIPTION_MAX)

    # Discord also caps the sum of every text part; trim the description last,
    # since the fields carry the who/where a reviewer needs to act.
    fixed = len(title) + sum(len(name) + len(value) for name, value, _inline in fields)
    if fixed + len(description) > TOTAL_MAX:
        description = _truncate(description, max(0, TOTAL_MAX - fixed))

    return EmbedSpec(
        title=title,
        description=description,
        color=color,
        fields=tuple(fields),
        timestamp=True,
    )


def _truncate(text: str, limit: int) -> str:
    collapsed = text.strip()
    if len(collapsed) <= limit:
        return collapsed
    if limit <= 1:
        return collapsed[:limit]
    return collapsed[: limit - 1].rstrip() + "…"


def build_learn_log_feed(
    *,
    get_bot: Callable[[], discord.Client | None],
    is_guild_active: Callable[[int], bool] | None = None,
) -> LearnLogFeed:
    return LearnLogFeed(get_bot=get_bot, is_guild_active=is_guild_active)
