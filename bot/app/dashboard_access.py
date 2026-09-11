"""Fresh server policy and channel access for the private Activity surface."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import aiohttp
import discord
from aiohttp import web
from discord.ext import commands

from app.consent import ConsentPreferenceStore
from app.message_runtime import BlockedUserCheck
from config.settings import Settings
from discord_adapter.gateway import _private_thread_has_member, _search_channel_accessible
from trust.resolver import TrustResolver
from trust.tiers import TrustTier
from utils.frontmatter import split_frontmatter_strict


@dataclass(frozen=True, slots=True)
class DashboardContext:
    member: discord.Member
    channel: discord.TextChannel | discord.Thread
    tier: TrustTier

    @property
    def parent_id(self) -> str:
        return str(
            self.channel.parent_id if isinstance(self.channel, discord.Thread) else self.channel.id
        )


@dataclass(frozen=True, slots=True)
class DashboardGuildPolicy:
    enabled: bool = False
    role_allowlist_configured: bool = False
    allowed_role_ids: frozenset[str] = frozenset()


class DashboardAccess:
    def __init__(
        self,
        *,
        bot: commands.Bot,
        settings: Settings,
        trust: TrustResolver,
        active_guilds: Callable[[], set[int]],
        user_blocked: BlockedUserCheck,
        channel_access_allowed: Callable[[object, object], bool],
        preferences: ConsentPreferenceStore,
    ) -> None:
        self.bot, self.settings, self.trust = bot, settings, trust
        self.active_guilds, self.user_blocked = active_guilds, user_blocked
        self.channel_access_allowed, self.preferences = channel_access_allowed, preferences

    def _guild_policy(self, guild_id: str) -> DashboardGuildPolicy:
        path = Path(self.settings.config_dir) / "servers" / f"{guild_id}.md"
        try:
            meta, _ = split_frontmatter_strict(path.read_text(encoding="utf-8"))
            policy = meta.get("dashboard", {})
            if not isinstance(policy, dict) or policy.get("enabled") is not True:
                return DashboardGuildPolicy()
            if "allowed_role_ids" not in policy:
                return DashboardGuildPolicy(enabled=True)
            raw_roles = policy["allowed_role_ids"]
            if not isinstance(raw_roles, list) or len(raw_roles) > 100:
                raise ValueError("dashboard.allowed_role_ids must be a list of at most 100 IDs")
            role_ids = frozenset(
                str(role).strip()
                for role in raw_roles
                if not isinstance(role, bool)
                and str(role).strip().isascii()
                and str(role).strip().isdecimal()
            )
            if len(role_ids) != len(raw_roles):
                raise ValueError("dashboard.allowed_role_ids must contain unique numeric IDs")
            return DashboardGuildPolicy(
                enabled=True,
                role_allowlist_configured=True,
                allowed_role_ids=role_ids,
            )
        except OSError, UnicodeError, ValueError:
            return DashboardGuildPolicy()

    async def resolve(
        self,
        *,
        user_id: str,
        guild_id: str,
        channel_id: str,
        continuing: bool = False,
    ) -> DashboardContext:
        identifiers_valid = all(
            value.isascii() and value.isdecimal() for value in (user_id, guild_id, channel_id)
        )
        guild_policy = (
            await asyncio.to_thread(self._guild_policy, guild_id)
            if identifiers_valid
            else DashboardGuildPolicy()
        )
        if (
            not self.settings.dashboard_enabled
            or not identifiers_valid
            or int(guild_id) not in self.active_guilds()
            or not guild_policy.enabled
        ):
            raise web.HTTPForbidden(reason="The dashboard is disabled in this server")
        allowed_users = self.settings.dashboard_allowed_user_id_set
        if (
            allowed_users
            and not guild_policy.role_allowlist_configured
            and user_id not in allowed_users
        ):
            raise web.HTTPForbidden(reason="Dashboard access is currently limited to invited users")
        if await self.user_blocked(user_id):
            raise web.HTTPForbidden(reason="You cannot use the dashboard right now")
        guild = self.bot.get_guild(int(guild_id))
        if guild is None:
            raise web.HTTPForbidden(reason="This server is unavailable")
        try:
            member = await guild.fetch_member(int(user_id))
            if guild_policy.role_allowlist_configured and user_id not in allowed_users:
                member_role_ids = {
                    str(role.id) for role in member.roles if getattr(role, "id", None) is not None
                }
                if not member_role_ids & guild_policy.allowed_role_ids:
                    raise web.HTTPForbidden(
                        reason="Dashboard access is currently limited to invited users or roles"
                    )
            # Fetch the channel as well as the member: stale overwrites must not
            # expose saved private content after access is removed.
            channel = await guild.fetch_channel(int(channel_id))
            if not isinstance(channel, discord.TextChannel | discord.Thread):
                raise web.HTTPForbidden(reason="Launch in a text channel or thread")
            if str(channel.guild.id) != guild_id:
                raise web.HTTPForbidden(reason="This channel belongs to another server")
            if not await asyncio.to_thread(self.channel_access_allowed, channel, member):
                raise web.HTTPForbidden(reason="This channel is outside the bot's boundaries")
            permission_channel: discord.TextChannel | discord.Thread | discord.ForumChannel = (
                channel
            )
            if isinstance(channel, discord.Thread):
                parent = await guild.fetch_channel(channel.parent_id)
                if not isinstance(parent, discord.TextChannel | discord.ForumChannel):
                    raise web.HTTPForbidden(reason="This thread's parent is unavailable")
                permission_channel = parent
                if channel.type is discord.ChannelType.private_thread:
                    for actor in (member, guild.me):
                        if actor is None or (
                            not parent.permissions_for(actor).manage_threads
                            and not await _private_thread_has_member(
                                channel, actor, propagate_http_errors=True
                            )
                        ):
                            raise web.HTTPForbidden(reason="Private thread membership is required")
            if not await _search_channel_accessible(
                permission_channel, member, guild.me, propagate_http_errors=True
            ):
                raise web.HTTPForbidden(reason="This channel is unavailable to you or the bot")
            if continuing:
                for actor in (member, guild.me):
                    if actor is None:
                        raise web.HTTPForbidden(reason="Bot membership is unavailable")
                    permissions = permission_channel.permissions_for(actor)
                    permitted = (
                        permissions.send_messages_in_threads
                        if isinstance(channel, discord.Thread)
                        else permissions.send_messages
                    )
                    if not permitted:
                        raise web.HTTPForbidden(reason="You and the bot must be able to chat here")
                if isinstance(channel, discord.Thread) and (channel.archived or channel.locked):
                    raise web.HTTPForbidden(reason="This thread is archived or locked")
        except discord.HTTPException as exc:
            if exc.status == 429 or exc.status >= 500:
                raise web.HTTPServiceUnavailable(
                    reason="Discord access verification is temporarily unavailable"
                ) from None
            raise web.HTTPForbidden(
                reason="Your current channel access could not be verified"
            ) from None
        except aiohttp.ClientError, TimeoutError:
            raise web.HTTPServiceUnavailable(
                reason="Discord access verification is temporarily unavailable"
            ) from None
        tier = await asyncio.to_thread(self.trust.resolve, member, user_id, guild_id)
        if tier < TrustTier(self.settings.dashboard_min_tier):
            raise web.HTTPForbidden(reason="Your server trust tier does not have dashboard access")
        return DashboardContext(member, channel, tier)

    async def consent_required(self, user_id: str) -> bool:
        return self.settings.privacy_consent_enabled and not await self.preferences.has_consented(
            user_id
        )
