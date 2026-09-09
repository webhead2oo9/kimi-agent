"""Fresh Discord authority for task owners and cross-channel publication."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

import discord
from discord.ext import commands
from pydantic import BaseModel, ConfigDict, Field

from config.fragments.tool_policy import load_blocked_tools
from config.settings import Settings
from discord_adapter.gateway import _search_channel_accessible
from tools.registry import MessageContext
from trust.resolver import TrustResolver
from trust.tiers import TrustTier, trust_tier_from_value
from utils.frontmatter import split_frontmatter_strict


class TaskPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool = False
    min_tier: Literal["member", "regular", "staff"] = "staff"
    timezone: str | None = None
    destinations: list[int] = Field(default_factory=list)


class TaskAccess:
    def __init__(
        self,
        bot: commands.Bot,
        settings: Settings,
        trust: TrustResolver,
        active_guilds: Callable[[], set[int]],
    ) -> None:
        self.bot, self.settings, self.trust = bot, settings, trust
        self.active_guilds = active_guilds

    async def policy(self, guild_id: str) -> TaskPolicy:
        if not guild_id.isdigit() or int(guild_id) not in self.active_guilds():
            raise ValueError("This server is not active")

        def read() -> TaskPolicy:
            path = Path(self.settings.config_dir) / "servers" / f"{guild_id}.md"
            meta, _ = split_frontmatter_strict(path.read_text(encoding="utf-8"))
            return TaskPolicy.model_validate(meta.get("scheduled_tasks", {}))

        return await asyncio.to_thread(read)

    async def context(
        self,
        guild_id: str,
        owner_id: str,
        channel_id: str,
        *,
        run_id: str = "",
    ) -> MessageContext:
        if int(guild_id) not in self.active_guilds():
            raise ValueError("This server is not active")
        guild = self.bot.get_guild(int(guild_id))
        if guild is None:
            raise ValueError("Server unavailable")
        member = await guild.fetch_member(int(owner_id))
        return MessageContext(
            user_id=owner_id,
            user_name=member.display_name,
            guild_id=guild_id,
            channel_id=channel_id,
            thread_id=None,
            platform_member=member,
            trust_tier=self.trust.resolve(member, owner_id, guild_id),
            scheduled_run_id=run_id,
        )

    async def owner_allowed(self, ctx: MessageContext) -> TaskPolicy:
        policy = await self.policy(ctx.guild_id or "")
        if not policy.enabled:
            raise ValueError("Scheduled tasks are disabled in this server")
        if ctx.trust_tier < trust_tier_from_value(policy.min_tier):
            raise ValueError("Your current server tier cannot create or run tasks")
        await self.channel(ctx, ctx.channel_id, posting=False)
        return policy

    async def channel(
        self,
        ctx: MessageContext,
        channel_id: str,
        *,
        posting: bool,
    ) -> Any:
        if not channel_id.isdigit():
            raise ValueError("Channel must be a numeric ID")
        policy = await self.policy(ctx.guild_id or "")
        member = ctx.platform_member
        guild = getattr(member, "guild", None)
        if (
            member is None
            or guild is None
            or str(guild.id) != ctx.guild_id
            or str(member.id) != ctx.user_id
        ):
            raise ValueError("Discord identity is unavailable")
        channel = guild.get_channel_or_thread(int(channel_id))
        if channel is None:
            channel = await guild.fetch_channel(int(channel_id))
        if str(getattr(getattr(channel, "guild", None), "id", "")) != ctx.guild_id:
            raise ValueError("Cross-server access is not supported")
        if not isinstance(channel, discord.TextChannel | discord.Thread):
            raise ValueError("Choose a text channel or existing thread")
        parent = getattr(channel, "parent_id", None) or channel.id
        if self.settings.allowed_channels and parent not in self.settings.allowed_channels:
            raise ValueError("Channel is outside the bot's operating boundaries")
        if not await _search_channel_accessible(channel, member, guild.me):
            raise ValueError("Channel is unavailable to you or the bot")
        if posting:
            if channel.id not in policy.destinations and parent not in policy.destinations:
                raise ValueError("Channel is not a configured posting destination")
            blocked = await asyncio.to_thread(
                load_blocked_tools,
                ctx.guild_id or "",
                str(parent),
            )
            if "discord_post" in blocked:
                raise ValueError("Posting is disabled by the destination's tool policy")
            for actor in (member, guild.me):
                if actor is None:
                    raise ValueError("Bot membership unavailable")
                permissions = channel.permissions_for(actor)
                allowed = (
                    permissions.send_messages_in_threads
                    if isinstance(channel, discord.Thread)
                    else permissions.send_messages
                )
                if not allowed:
                    raise ValueError("Both you and the bot must be able to post here")
            if isinstance(channel, discord.Thread) and (channel.archived or channel.locked):
                raise ValueError("Destination thread is archived or locked")
        return channel

    async def mentions(
        self,
        ctx: MessageContext,
        channel: Any,
        users: list[str],
        roles: list[str],
    ) -> discord.AllowedMentions:
        if len(users) > 20 or len(roles) > 20:
            raise ValueError("At most 20 user and 20 role recipients are allowed")
        for user in users:
            if not user.isdigit():
                raise ValueError("User recipients must be numeric IDs")
            member = await channel.guild.fetch_member(int(user))
            if not await _search_channel_accessible(channel, member, channel.guild.me):
                raise ValueError("A recipient cannot access this destination")
        selected: list[discord.Role] = []
        for token in roles:
            if not token.isdigit():
                raise ValueError("Role recipients must be numeric IDs")
            role = channel.guild.get_role(int(token))
            if role is None or role.is_default():
                raise ValueError("Role is unavailable; everyone/here notifications are forbidden")
            if not role.mentionable and not all(
                channel.permissions_for(actor).mention_everyone
                for actor in (ctx.platform_member, channel.guild.me)
            ):
                raise ValueError("You and the bot must be allowed to mention that role")
            selected.append(role)
        return discord.AllowedMentions(
            everyone=False,
            users=[discord.Object(int(user)) for user in users],
            roles=selected,
            replied_user=False,
        )


def may_manage(ctx: MessageContext, task: dict[str, Any]) -> bool:
    return ctx.guild_id == task["guild_id"] and (
        ctx.user_id == task["owner_id"] or ctx.trust_tier >= TrustTier.STAFF
    )
