from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from commands._shared import normalize_user_id, send_message as _send_message
from storage.blocked_users import BlockedUserRecord, BlockedUserStore
from trust.resolver import TrustResolver
from trust.tiers import TrustTier


class ModerationGroup(app_commands.Group):
    def __init__(self, store: BlockedUserStore, trust_resolver: TrustResolver) -> None:
        super().__init__(name="moderation", description="Staff bot moderation controls")
        self._store = store
        self._trust_resolver = trust_resolver

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        guild_id = str(interaction.guild_id) if interaction.guild_id else None
        tier = self._trust_resolver.resolve(member, str(interaction.user.id), guild_id)
        if tier >= TrustTier.STAFF:
            return True
        await _send_message(interaction, "Staff only.")
        return False

    @app_commands.command(name="block", description="Block a user from using the bot")
    @app_commands.describe(
        user="User to block (may have already left the server)",
        reason="Optional internal reason",
    )
    async def block(
        self,
        interaction: discord.Interaction,
        user: discord.Member | discord.User,
        reason: str | None = None,
    ) -> None:
        actor_id = str(interaction.user.id)
        user_id = str(user.id)
        if user_id == actor_id:
            await _send_message(interaction, "You cannot block yourself.")
            return
        # A target who left the guild resolves with no Member (no roles): staff-ID
        # allowlist protection still applies; role-based staff who left degrade to
        # blockable MEMBER, which is correct since their roles are gone with them.
        target_member = user if isinstance(user, discord.Member) else None
        guild_id = str(interaction.guild_id) if interaction.guild_id else None
        target_tier = self._trust_resolver.resolve(target_member, user_id, guild_id)
        if target_tier >= TrustTier.STAFF:
            await _send_message(interaction, "Staff users cannot be blocked.")
            return
        created = await self._store.block_user(
            user_id,
            blocked_by=actor_id,
            reason=(reason or "").strip(),
        )
        state = "Blocked" if created else "Updated block for"
        await _send_message(interaction, f"{state} `{user_id}`.")

    @app_commands.command(name="unblock", description="Unblock a user from using the bot")
    @app_commands.describe(user_id="Discord user ID or mention to unblock")
    async def unblock(self, interaction: discord.Interaction, user_id: str) -> None:
        normalized = normalize_user_id(user_id)
        if not normalized.isdigit():
            await _send_message(interaction, "User ID must be an exact Discord user ID.")
            return
        removed = await self._store.unblock_user(normalized)
        if removed:
            await _send_message(interaction, f"Unblocked `{normalized}`.")
            return
        await _send_message(interaction, f"`{normalized}` was not blocked.")

    @app_commands.command(name="status", description="Check whether a user is blocked")
    @app_commands.describe(user_id="Discord user ID or mention to check")
    async def status(self, interaction: discord.Interaction, user_id: str) -> None:
        normalized = normalize_user_id(user_id)
        if not normalized.isdigit():
            await _send_message(interaction, "User ID must be an exact Discord user ID.")
            return
        record = await self._store.get_block(normalized)
        await _send_message(interaction, format_block_status(normalized, record))


def format_block_status(user_id: str, record: BlockedUserRecord | None) -> str:
    if record is None:
        return f"`{user_id}` is not blocked."
    reason = record.reason or "No reason recorded."
    return f"`{user_id}` is blocked.\nBlocked by: `{record.blocked_by}`\nReason: {reason}"


def register_moderation_command(
    bot: commands.Bot,
    store: BlockedUserStore,
    trust_resolver: TrustResolver,
) -> None:
    bot.tree.add_command(ModerationGroup(store, trust_resolver), override=True)
