"""Helpers shared by the slash-command modules.

``send_message``/``defer_if_needed`` originated in ``commands/memory_cmd.py`` and
``normalize_user_id`` in ``commands/moderation_cmd.py``; this module is their
single canonical home so sibling command modules stop importing each other's
private helpers.
"""

from __future__ import annotations

from typing import Any

import discord

from discord_adapter.io import CHUNK_THRESHOLD


async def defer_if_needed(interaction: discord.Interaction) -> None:
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True, thinking=True)


async def send_message(interaction: discord.Interaction, content: str) -> None:
    send_kwargs: dict[str, Any] = {
        "ephemeral": True,
        "allowed_mentions": discord.AllowedMentions.none(),
    }
    fitted = _fit_discord_message(content)
    if interaction.response.is_done():
        await interaction.followup.send(fitted, **send_kwargs)
        return
    await interaction.response.send_message(fitted, **send_kwargs)


def normalize_user_id(user_id: str) -> str:
    cleaned = user_id.strip()
    if cleaned.startswith("<@") and cleaned.endswith(">"):
        cleaned = cleaned[2:-1]
        if cleaned.startswith("!"):
            cleaned = cleaned[1:]
    return cleaned


_TRUNCATION_MARKER = "\n... [truncated]"


def _fit_discord_message(content: str) -> str:
    if len(content) <= CHUNK_THRESHOLD:
        return content
    return content[: CHUNK_THRESHOLD - len(_TRUNCATION_MARKER)].rstrip() + _TRUNCATION_MARKER
