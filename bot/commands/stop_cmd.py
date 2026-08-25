from __future__ import annotations

from collections.abc import Awaitable, Callable

import discord
from discord import app_commands
from discord.ext import commands

StopCallback = Callable[[discord.Interaction, bool, str | None], Awaitable[str]]


def register_stop_command(bot: commands.Bot, callback: StopCallback) -> None:
    @app_commands.command(name="stop", description="Stop an active response or coding task")
    @app_commands.describe(
        scope="Use all to stop all of your active work; current is the default",
        task_id="Optional coding task ID to stop",
    )
    @app_commands.choices(
        scope=[
            app_commands.Choice(name="current", value="current"),
            app_commands.Choice(name="all", value="all"),
        ]
    )
    async def stop(
        interaction: discord.Interaction,
        scope: app_commands.Choice[str] | None = None,
        task_id: str | None = None,
    ) -> None:
        if interaction.guild_id is None or interaction.channel_id is None:
            await interaction.response.send_message(
                "Stop is only available in a server channel.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        summary = await callback(
            interaction,
            bool(scope is not None and scope.value == "all"),
            task_id.strip() if task_id and task_id.strip() else None,
        )
        await interaction.followup.send(summary, ephemeral=True)

    bot.tree.add_command(stop, override=True)
