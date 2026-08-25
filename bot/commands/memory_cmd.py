from __future__ import annotations

import contextlib
from contextlib import AbstractAsyncContextManager

import discord
from discord import app_commands
from discord.ext import commands

from utils.privacy_barrier import UserPrivacyBarrier
from memory.mutations import user_memory_mutation
from storage.preferences import PreferenceStore


class MemoryGroup(app_commands.Group):
    """Manage your bot memory settings.

    Self-service only; on-demand deletion lives on /privacy.
    """

    def __init__(
        self,
        preferences: PreferenceStore,
        privacy_barrier: UserPrivacyBarrier | None = None,
    ) -> None:
        super().__init__(name="memory", description="Manage your bot memory settings")
        self._preferences = preferences
        self._privacy_barrier = privacy_barrier

    def _activity(self, user_id: str) -> AbstractAsyncContextManager[None]:
        if self._privacy_barrier is None:
            return contextlib.nullcontext()
        return self._privacy_barrier.activity(user_id)

    @app_commands.command(name="status", description="Check your current memory setting")
    async def status(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        user_id = str(interaction.user.id)
        async with self._activity(user_id):
            enabled = await self._preferences.is_memory_enabled(user_id)
            state = "enabled" if enabled else "disabled"
            await interaction.edit_original_response(
                content=f"Your memory is currently **{state}**.",
            )

    @app_commands.command(
        name="opt-out",
        description="Disable long-term memory recall and retention",
    )
    async def opt_out(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        user_id = str(interaction.user.id)
        async with self._activity(user_id):
            async with user_memory_mutation(user_id):
                changed = await self._preferences.set_memory_enabled(user_id, False)
            if changed:
                content = (
                    "Memory **disabled**. I won't use or retain long-term Hindsight "
                    "memories for you going forward."
                )
            else:
                content = "Your memory is already disabled."
            await interaction.edit_original_response(content=content)

    @app_commands.command(
        name="opt-in",
        description="Enable long-term memory recall and retention",
    )
    async def opt_in(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        user_id = str(interaction.user.id)
        async with self._activity(user_id):
            async with user_memory_mutation(user_id):
                changed = await self._preferences.set_memory_enabled(user_id, True)
            if changed:
                content = (
                    "Memory **enabled**. I can use and retain long-term Hindsight memories for you."
                )
            else:
                content = "Your memory is already enabled."
            await interaction.edit_original_response(content=content)


def register_memory_command(
    bot: commands.Bot,
    preferences: PreferenceStore,
    *,
    privacy_barrier: UserPrivacyBarrier | None = None,
) -> None:
    bot.tree.add_command(
        MemoryGroup(preferences, privacy_barrier=privacy_barrier),
        override=True,
    )
