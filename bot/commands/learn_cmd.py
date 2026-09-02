"""The bot-name-derived teaching message context menu.

Right-clicking a message and teaching from it is the capture half of the learn
flow; ``app/learn_turn.py`` is the thinking half and ``app/learn_log.py`` the
audit half. This module stays a thin Discord boundary: check standing, reduce
the gateway message to plain data, hand off, and report back ephemerally.

Staff-only, and re-checked here rather than trusted from the caller: a context
menu carries no tier of its own, and every tool the resulting turn calls is
gated again at dispatch. A moderation block is standing too: the same
``is_blocked`` answer that silences a blocked user's messages refuses their
teach gesture, since staff standing can arrive after a block (promotion,
per-guild trust) and this path runs a full model turn.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

import discord
from discord import app_commands
from discord.ext import commands

from commands._shared import send_message as _send_message
from branding import DEFAULT_BOT_NAME
from tools.learn import LearnTarget
from trust.resolver import TrustResolver
from trust.tiers import TrustTier

log = logging.getLogger(__name__)

LEARN_MENU_PREFIX = "Teach "

_NO_STANDING = "Staff only."
_BLOCKED = "You can't use this right now."
_NO_GUILD = "I can only learn community knowledge inside a server."
_BOT_MESSAGE = "That's one of my own messages. Teach me from what a person actually said."
_EMPTY = "That message has nothing for me to learn from."
_FAILED = "Something went wrong while I was learning that. Nothing was saved."
_NO_REPORT = "I finished, but had nothing to report back."

LearnRunner = Callable[[LearnTarget, discord.Interaction], Awaitable[str]]
BlockedUserCheck = Callable[[str], Awaitable[bool]]
LearnResume = Callable[[discord.Interaction], Awaitable[None]]
ConsentRequest = Callable[[discord.Interaction, LearnResume], Awaitable[bool]]


def learn_menu_name(bot_name: str) -> str:
    """Return a Discord-safe context-menu name derived from the bot name."""

    name = bot_name.strip() or DEFAULT_BOT_NAME
    return f"{LEARN_MENU_PREFIX}{name}"[:32].rstrip()


def register_learn_command(
    bot: commands.Bot,
    trust_resolver: TrustResolver,
    *,
    run_learn: LearnRunner,
    is_blocked: BlockedUserCheck,
    request_consent: ConsentRequest,
    bot_name: str = DEFAULT_BOT_NAME,
) -> None:
    """Install the context menu.

    The turn runner and entry gates are injected so this module never reaches
    into the application: ``app/runtime.py`` binds them to the scoped turn,
    blocked-user store, and shared privacy-consent flow.
    """

    async def authorized(interaction: discord.Interaction) -> bool:
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        guild_id = str(interaction.guild_id) if interaction.guild_id else None
        tier = trust_resolver.resolve(member, str(interaction.user.id), guild_id)
        if tier < TrustTier.STAFF:
            await _send_message(interaction, _NO_STANDING)
            return False
        try:
            blocked = await is_blocked(str(interaction.user.id))
        except Exception:
            # Standing stays fail-closed, but the staff member must still get a
            # reply: an escaped exception leaves the click or the resumed consent
            # interaction hanging with no answer.
            log.exception("Blocked-user lookup failed for user %s", interaction.user.id)
            await _send_message(interaction, _BLOCKED)
            return False
        if blocked:
            await _send_message(interaction, _BLOCKED)
            return False
        if guild_id is None:
            await _send_message(interaction, _NO_GUILD)
            return False
        return True

    @app_commands.context_menu(name=learn_menu_name(bot_name))
    async def teach_from_message(
        interaction: discord.Interaction,
        message: discord.Message,
    ) -> None:
        if not await authorized(interaction):
            return
        if message.author.bot:
            await _send_message(interaction, _BOT_MESSAGE)
            return
        if not message.content.strip() and not message.attachments:
            await _send_message(interaction, _EMPTY)
            return

        async def run(resume_interaction: discord.Interaction) -> None:
            # Consent can leave this callback pending while standing or a block
            # changes, so authorize the component interaction again at use time.
            if not await authorized(resume_interaction):
                return
            target = LearnTarget(
                content=message.content or "",
                author_name=getattr(message.author, "display_name", str(message.author)),
                author_id=str(message.author.id),
                jump_url=message.jump_url,
                message_id=str(message.id),
                channel_id=str(message.channel.id),
                attachment_names=tuple(attachment.filename for attachment in message.attachments),
            )
            try:
                report = await run_learn(target, resume_interaction)
            except Exception:
                log.exception("Learn turn failed for message %s", message.id)
                await _send_message(resume_interaction, _FAILED)
                return
            await _send_message(resume_interaction, report.strip() or _NO_REPORT)

        if await request_consent(interaction, run):
            return

        # Learning runs a full model turn with tool calls; defer before it starts
        # so the interaction token does not expire mid-thought.
        await interaction.response.defer(ephemeral=True, thinking=True)
        await run(interaction)

    bot.tree.add_command(teach_from_message, override=True)
