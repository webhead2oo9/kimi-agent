"""Discord SDK implementation of the module ``DiscordActions`` and ``TrustLookup`` ports.

Every operation takes stable IDs, resolves live objects through the bot, and
returns public snapshots. Targeted actions (ban, kick, timeout) run the core
target policy: never the bot, never the acting user, never a member whose
trust tier is at or above the actor's, unless the module declared
``override_target_policy``. Guilds the core does not consider active are
refused.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable, Sequence
from typing import Any

import discord
from discord.ext import commands

from discord_adapter.module_events import member_snapshot, message_snapshot
from kimi_agent_module_api.contracts import (
    MemberSnapshot,
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
    text = " ".join(reason.split())[:_MAX_REASON]
    return f"[{module_name}] {text}" if text else f"[{module_name}]"


class DiscordActionsImpl:
    """Unchecked implementation; ``modules.actions`` wraps it with the declaration gate."""

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

    # ---- helpers --------------------------------------------------------

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
            target = _TIER_ORDER[await self._trust.tier(guild_id, user_id)]
            # An automated module acts with staff authority: it may touch
            # anyone below staff, never staff.
            actor = (
                _TIER_ORDER["staff"]
                if actor_id is None
                else _TIER_ORDER[await self._trust.tier(guild_id, actor_id)]
            )
            if target >= actor:
                raise TargetProtected("target's trust tier is not below the actor's")
        return member

    # ---- actions --------------------------------------------------------

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
            kwargs["view"] = components[0]
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
        log.info("Module %s deleting message %s: %s", self._module_name, ref.message_id, reason)
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


__all__ = ["DiscordActionError", "DiscordActionsImpl", "TargetProtected", "TrustLookupImpl"]
