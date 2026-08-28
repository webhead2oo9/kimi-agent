"""Discord implementation of the module action and trust ports.

Guild and channel operations require an active guild. Moderation always protects
the bot, actor, and bot accounts; unless overridden, targets must also rank below
the actor, or below staff for automated actions. The outer declaration wrapper
controls which actions each module may use.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable, Sequence
from typing import Any

import discord
from discord.ext import commands

from discord_adapter.module_events import member_snapshot, message_snapshot
from discord_adapter.module_interactions import build_view
from community_agent_module_api.contracts import (
    ChannelKind,
    ChannelSnapshot,
    MemberSnapshot,
    MessagePage,
    MessageRef,
    MessageSnapshot,
    ModuleContractError,
    OutgoingEmbed,
    TrustTierName,
)
from trust.resolver import TrustResolver
from trust.tiers import TrustTier

log = logging.getLogger(__name__)

_MAX_REASON = 512
_TIER_ORDER: dict[TrustTierName, int] = {"member": 0, "regular": 1, "staff": 2}


class DiscordActionError(RuntimeError):
    """A Discord operation failed; the message is safe to show staff."""


class TargetProtected(DiscordActionError):
    """The target policy refused a ban, kick, or timeout."""


class TrustLookupImpl:
    def __init__(self, bot: commands.Bot, resolver: TrustResolver) -> None:
        self._bot = bot
        self._resolver = resolver

    async def tier(self, guild_id: int, user_id: int) -> TrustTierName:
        member = await _member_or_none(self._bot, guild_id, user_id)
        return self.tier_for_member(guild_id, user_id, member)

    def tier_for_member(
        self, guild_id: int, user_id: int, member: discord.Member | None
    ) -> TrustTierName:
        """Resolve trust from a member that the caller has already fetched."""
        tier = self._resolver.resolve(member, str(user_id), str(guild_id))
        return _tier_name(tier)


def _tier_name(tier: TrustTier) -> TrustTierName:
    value = tier.value
    if value in _TIER_ORDER:
        return value  # type: ignore[return-value]
    return "member"


async def _member_or_none(bot: commands.Bot, guild_id: int, user_id: int) -> discord.Member | None:
    guild = bot.get_guild(guild_id)
    if guild is None:
        return None
    member = guild.get_member(user_id)
    if member is not None:
        return member
    try:
        return await guild.fetch_member(user_id)
    except discord.NotFound:
        return None
    except discord.HTTPException as exc:
        log.warning("fetch_member failed for %s in %s: %s", user_id, guild_id, exc)
        return None


def _build_embed(spec: OutgoingEmbed) -> discord.Embed:
    embed = discord.Embed(title=spec.title, description=spec.description, color=spec.color)
    for name, value, inline in spec.fields:
        embed.add_field(name=name, value=value, inline=inline)
    if spec.footer:
        embed.set_footer(text=spec.footer)
    if spec.timestamp:
        embed.timestamp = discord.utils.utcnow()
    return embed


def _reason(module_name: str, reason: str) -> str:
    prefix = f"[{module_name}]"
    text = " ".join(reason.split())
    if not text:
        return prefix[:_MAX_REASON]
    available = max(0, _MAX_REASON - len(prefix) - 1)
    return f"{prefix} {text[:available]}"[:_MAX_REASON]


class DiscordActionsImpl:
    """Discord adapter; the outer wrapper enforces module action declarations."""

    def __init__(
        self,
        *,
        bot: commands.Bot,
        trust: TrustLookupImpl,
        module_name: str,
        is_guild_active: Callable[[int], bool],
        override_target_policy: bool = False,
    ) -> None:
        self._bot = bot
        self._trust = trust
        self._module_name = module_name
        self._is_guild_active = is_guild_active
        self._override = override_target_policy

    def _guild(self, guild_id: int) -> discord.Guild:
        if not self._is_guild_active(guild_id):
            raise DiscordActionError(f"guild {guild_id} is not active for this bot")
        guild = self._bot.get_guild(guild_id)
        if guild is None:
            raise DiscordActionError(f"guild {guild_id} is not available")
        return guild

    async def _channel(self, channel_id: int) -> Any:
        channel = self._bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self._bot.fetch_channel(channel_id)
            except discord.HTTPException as exc:
                raise DiscordActionError(f"channel {channel_id} is not available") from exc
        guild = getattr(channel, "guild", None)
        if guild is None or not self._is_guild_active(int(guild.id)):
            raise DiscordActionError(f"channel {channel_id} is not in an active guild")
        return channel

    async def _message(self, ref: MessageRef) -> discord.Message:
        channel = await self._channel(ref.channel_id)
        if int(channel.guild.id) != ref.guild_id:
            raise DiscordActionError(f"channel {ref.channel_id} is not in guild {ref.guild_id}")
        try:
            return await channel.fetch_message(ref.message_id)
        except discord.NotFound as exc:
            raise DiscordActionError(f"message {ref.message_id} no longer exists") from exc
        except discord.HTTPException as exc:
            raise DiscordActionError(f"message {ref.message_id} is not available") from exc

    async def _check_target(
        self, guild_id: int, user_id: int, actor_id: int | None
    ) -> discord.Member:
        bot_user = self._bot.user
        if bot_user is not None and user_id == int(bot_user.id):
            raise TargetProtected("the bot cannot moderate itself")
        if actor_id is not None and user_id == actor_id:
            raise TargetProtected("a member cannot be moderated by themselves")
        member = await _member_or_none(self._bot, guild_id, user_id)
        if member is None:
            raise DiscordActionError(f"member {user_id} is not in guild {guild_id}")
        if member.bot:
            raise TargetProtected("bots cannot be moderated by modules")
        if not self._override:
            # A failed second fetch could discard this member's role-based trust.
            target = _TIER_ORDER[self._trust.tier_for_member(guild_id, user_id, member)]
            # Automated actions use staff authority.
            actor = (
                _TIER_ORDER["staff"]
                if actor_id is None
                else _TIER_ORDER[await self._trust.tier(guild_id, actor_id)]
            )
            if target >= actor:
                raise TargetProtected("target's trust tier is not below the actor's")
        return member

    async def send_message(
        self,
        channel_id: int,
        content: str | None = None,
        *,
        embed: OutgoingEmbed | None = None,
        reply_to: MessageRef | None = None,
        components: Sequence[Any] = (),
    ) -> MessageRef:
        channel = await self._channel(channel_id)
        channel_guild_id = int(channel.guild.id)
        if reply_to is not None and (
            reply_to.guild_id != channel_guild_id or reply_to.channel_id != int(channel.id)
        ):
            raise DiscordActionError("reply target is not in the destination channel")
        kwargs: dict[str, Any] = {"allowed_mentions": discord.AllowedMentions.none()}
        if embed is not None:
            kwargs["embed"] = _build_embed(embed)
        if reply_to is not None:
            kwargs["reference"] = discord.MessageReference(
                message_id=reply_to.message_id,
                channel_id=reply_to.channel_id,
                guild_id=reply_to.guild_id,
            )
        if components:
            kwargs["view"] = build_view(components, self._module_name)
        try:
            sent = await channel.send(content, **kwargs)
        except discord.HTTPException as exc:
            raise DiscordActionError("sending the message failed") from exc
        return MessageRef(int(channel.guild.id), int(channel.id), int(sent.id))

    async def send_dm(
        self, user_id: int, content: str, *, embed: OutgoingEmbed | None = None
    ) -> bool:
        user = self._bot.get_user(user_id)
        if user is None:
            try:
                user = await self._bot.fetch_user(user_id)
            except discord.HTTPException:
                return False
        kwargs: dict[str, Any] = {"allowed_mentions": discord.AllowedMentions.none()}
        if embed is not None:
            kwargs["embed"] = _build_embed(embed)
        try:
            await user.send(content, **kwargs)
        except discord.Forbidden, discord.HTTPException:
            return False
        return True

    async def edit_message(
        self, ref: MessageRef, content: str | None = None, *, embed: OutgoingEmbed | None = None
    ) -> None:
        message = await self._message(ref)
        try:
            await message.edit(
                content=content,
                embed=_build_embed(embed) if embed is not None else None,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException as exc:
            raise DiscordActionError("editing the message failed") from exc

    async def delete_message(self, ref: MessageRef, *, reason: str = "") -> None:
        message = await self._message(ref)
        # discord.py's Message.delete carries no audit reason; keep it in our log.
        log.info("Deleting message %s: %s", ref.message_id, _reason(self._module_name, reason))
        try:
            await message.delete()
        except discord.NotFound:
            return
        except discord.HTTPException as exc:
            raise DiscordActionError("deleting the message failed") from exc

    async def ban(
        self,
        guild_id: int,
        user_id: int,
        *,
        actor_id: int | None,
        reason: str,
        delete_message_seconds: int = 0,
    ) -> None:
        guild = self._guild(guild_id)
        member = await self._check_target(guild_id, user_id, actor_id)
        try:
            await guild.ban(
                member,
                reason=_reason(self._module_name, reason),
                delete_message_seconds=max(0, min(int(delete_message_seconds), 604_800)),
            )
        except discord.HTTPException as exc:
            raise DiscordActionError("ban failed") from exc

    async def kick(self, guild_id: int, user_id: int, *, actor_id: int | None, reason: str) -> None:
        guild = self._guild(guild_id)
        member = await self._check_target(guild_id, user_id, actor_id)
        try:
            await guild.kick(member, reason=_reason(self._module_name, reason))
        except discord.HTTPException as exc:
            raise DiscordActionError("kick failed") from exc

    async def timeout(
        self,
        guild_id: int,
        user_id: int,
        *,
        actor_id: int | None,
        reason: str,
        duration_seconds: int,
    ) -> None:
        self._guild(guild_id)
        if duration_seconds <= 0:
            raise ModuleContractError("timeout duration must be positive")
        member = await self._check_target(guild_id, user_id, actor_id)
        until = discord.utils.utcnow() + dt.timedelta(
            seconds=min(int(duration_seconds), 28 * 86_400)
        )
        try:
            await member.timeout(until, reason=_reason(self._module_name, reason))
        except discord.HTTPException as exc:
            raise DiscordActionError("timeout failed") from exc

    async def fetch_message(self, ref: MessageRef) -> MessageSnapshot | None:
        try:
            message = await self._message(ref)
        except DiscordActionError:
            return None
        return message_snapshot(message)

    async def fetch_member(self, guild_id: int, user_id: int) -> MemberSnapshot | None:
        if not self._is_guild_active(guild_id):
            return None
        member = await _member_or_none(self._bot, guild_id, user_id)
        return member_snapshot(member) if member is not None else None

    async def fetch_channel(self, guild_id: int, channel_id: int) -> ChannelSnapshot | None:
        try:
            channel = await self._channel(channel_id)
        except DiscordActionError:
            return None
        if int(channel.guild.id) != guild_id:
            return None
        kind: ChannelKind
        if isinstance(channel, discord.Thread):
            kind = "thread"
        elif isinstance(channel, discord.ForumChannel):
            kind = "forum"
        elif isinstance(channel, discord.TextChannel):
            kind = "text"
        else:
            return None
        return ChannelSnapshot(
            guild_id=guild_id,
            channel_id=int(channel.id),
            kind=kind,
            name=str(getattr(channel, "name", "") or ""),
            parent_channel_id=(
                int(parent_id) if (parent_id := getattr(channel, "parent_id", None)) else None
            ),
            topic=str(getattr(channel, "topic", "") or ""),
            archived=bool(getattr(channel, "archived", False)),
            private=bool(
                isinstance(channel, discord.Thread)
                and channel.type is discord.ChannelType.private_thread
            ),
            applied_tags=tuple(
                str(tag.name)
                for tag in (getattr(channel, "applied_tags", ()) or ())
                if getattr(tag, "name", None)
            ),
        )

    async def fetch_messages(
        self,
        guild_id: int,
        channel_id: int,
        *,
        after_message_id: int | None = None,
        before_message_id: int | None = None,
        limit: int = 100,
    ) -> MessagePage:
        if after_message_id is not None and before_message_id is not None:
            raise DiscordActionError("message history accepts either after or before, not both")
        channel = await self._channel(channel_id)
        if int(channel.guild.id) != guild_id or not hasattr(channel, "history"):
            raise DiscordActionError(f"channel {channel_id} is not readable in guild {guild_id}")
        page_limit = max(1, min(int(limit), 100))
        kwargs: dict[str, Any] = {"limit": page_limit + 1}
        if after_message_id is not None:
            kwargs.update(after=discord.Object(id=after_message_id), oldest_first=True)
        else:
            if before_message_id is not None:
                kwargs["before"] = discord.Object(id=before_message_id)
            kwargs["oldest_first"] = False
        try:
            raw = [message async for message in channel.history(**kwargs)]
        except discord.HTTPException as exc:
            raise DiscordActionError(f"history for channel {channel_id} is unavailable") from exc
        has_more = len(raw) > page_limit
        snapshots = tuple(
            sorted(
                (message_snapshot(message) for message in raw[:page_limit]),
                key=lambda message: message.ref.message_id,
            )
        )
        cursor = None
        if snapshots:
            cursor = (
                snapshots[-1].ref.message_id
                if after_message_id is not None
                else snapshots[0].ref.message_id
            )
        return MessagePage(snapshots, cursor, has_more)

    async def fetch_pins(self, guild_id: int, channel_id: int) -> tuple[MessageSnapshot, ...]:
        channel = await self._channel(channel_id)
        if int(channel.guild.id) != guild_id or not hasattr(channel, "pins"):
            return ()
        try:
            pins = await channel.pins()
        except discord.HTTPException as exc:
            raise DiscordActionError(f"pins for channel {channel_id} are unavailable") from exc
        return tuple(message_snapshot(message) for message in pins)

    async def fetch_public_threads(
        self, guild_id: int, parent_channel_id: int
    ) -> tuple[ChannelSnapshot, ...]:
        parent = await self._channel(parent_channel_id)
        if int(parent.guild.id) != guild_id or not hasattr(parent, "archived_threads"):
            return ()
        found: dict[int, discord.Thread] = {
            int(thread.id): thread
            for thread in parent.guild.threads
            if int(getattr(thread, "parent_id", 0) or 0) == parent_channel_id
            and thread.type is not discord.ChannelType.private_thread
        }
        try:
            async for thread in parent.archived_threads(limit=None, private=False):
                found[int(thread.id)] = thread
        except discord.HTTPException as exc:
            raise DiscordActionError(
                f"public threads for channel {parent_channel_id} are unavailable"
            ) from exc
        snapshots: list[ChannelSnapshot] = []
        for thread_id in sorted(found):
            snapshot = await self.fetch_channel(guild_id, thread_id)
            if snapshot is not None:
                snapshots.append(snapshot)
        return tuple(snapshots)

    async def can_view_channel(self, guild_id: int, user_id: int, channel_id: int) -> bool:
        if not self._is_guild_active(guild_id):
            return False
        try:
            channel = await self._channel(channel_id)
        except DiscordActionError:
            return False
        if int(channel.guild.id) != guild_id:
            return False
        member = await _member_or_none(self._bot, guild_id, user_id)
        if member is None or not hasattr(channel, "permissions_for"):
            return False
        permissions = channel.permissions_for(member)
        if not (permissions.view_channel and permissions.read_message_history):
            return False
        if (
            isinstance(channel, discord.Thread)
            and channel.type is discord.ChannelType.private_thread
        ):
            if any(int(item.id) == user_id for item in getattr(channel, "members", ())):
                return True
            try:
                members = await channel.fetch_members()
            except discord.HTTPException:
                return False
            return any(int(item.id) == user_id for item in members)
        return True


__all__ = ["DiscordActionError", "DiscordActionsImpl", "TargetProtected", "TrustLookupImpl"]
