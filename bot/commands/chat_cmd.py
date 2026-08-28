from __future__ import annotations

from collections.abc import Awaitable, Callable

import discord
from discord import app_commands
from discord.ext import commands


ChatCallback = Callable[
    [discord.Interaction, str, discord.Attachment | None, bool], Awaitable[None]
]
ResetCallback = Callable[[discord.Interaction], Awaitable[str]]
MAX_COMMAND_DESCRIPTION_LENGTH = 100
CHAT_VISIBILITY_PRIVATE = "private"
CHAT_VISIBILITY_PUBLIC = "public"


def _chat_description(bot_name: str) -> str:
    return f"Chat with {bot_name}"[:MAX_COMMAND_DESCRIPTION_LENGTH]


def register_user_app_chat_commands(
    bot: commands.Bot,
    *,
    run_chat: ChatCallback,
    reset_chat: ResetCallback,
    bot_name: str,
) -> None:
    """Register the explicitly user-installed personal chat surface."""

    @app_commands.command(name="chat", description=_chat_description(bot_name))
    @app_commands.describe(
        message="What you want to say",
        attachment="Optional image or file to include",
        visibility="Who can see the live response and result",
    )
    @app_commands.choices(
        visibility=[
            app_commands.Choice(name="Only me", value=CHAT_VISIBILITY_PRIVATE),
            app_commands.Choice(name="Everyone", value=CHAT_VISIBILITY_PUBLIC),
        ]
    )
    @app_commands.allowed_installs(guilds=False, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def chat(
        interaction: discord.Interaction,
        message: str,
        attachment: discord.Attachment | None = None,
        visibility: str = CHAT_VISIBILITY_PRIVATE,
    ) -> None:
        await run_chat(
            interaction,
            message,
            attachment,
            visibility == CHAT_VISIBILITY_PUBLIC,
        )

    @app_commands.command(
        name="chat-reset",
        description="Clear your personal /chat conversation",
    )
    @app_commands.allowed_installs(guilds=False, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def chat_reset(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        summary = await reset_chat(interaction)
        await interaction.edit_original_response(
            content=summary,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    bot.tree.add_command(chat, override=True)
    bot.tree.add_command(chat_reset, override=True)
