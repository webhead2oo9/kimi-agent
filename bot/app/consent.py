"""First-interaction privacy consent gate.

On a user's first interaction the bot posts a one-time privacy notice as an embed
with Accept / Decline buttons and holds the message *before* the model turn runs,
so nothing reaches the third-party LLM provider (or the local transcript) until the
user accepts. Accept records consent and re-dispatches the original message through
the normal response path; Decline drops it (the gate reappears next mention).

This module is the Discord-`View` boundary, so importing `discord` here is expected.
All decision logic lives in `PrivacyConsentGate`, which depends only on the small
`ConsentPreferenceStore` protocol and a redispatch callback, so it is unit-testable
without a live Discord connection.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Protocol

import discord

log = logging.getLogger(__name__)

RedispatchCallback = Callable[[discord.Message], Awaitable[None]]


class ConsentPreferenceStore(Protocol):
    async def has_consented(self, user_id: str) -> bool: ...

    async def set_consent(self, user_id: str, granted: bool) -> bool: ...


def build_consent_embed(*, title: str, text: str) -> discord.Embed:
    return discord.Embed(title=title, description=text, color=discord.Color.blurple())


def _accepted_embed(title: str) -> discord.Embed:
    return discord.Embed(
        title=title,
        description="✅ Thanks, you're all set. I won't show this again.",
        color=discord.Color.green(),
    )


def _declined_embed(title: str) -> discord.Embed:
    return discord.Embed(
        title=title,
        description=(
            "No problem. I won't process that message. "
            "Mention me again anytime if you change your mind."
        ),
        color=discord.Color.light_grey(),
    )


class PrivacyConsentView(discord.ui.View):
    """Two-button consent prompt. Thin: it only guards on author and delegates."""

    def __init__(
        self,
        *,
        author_id: int,
        on_accept: Callable[[discord.Interaction], Awaitable[None]],
        on_decline: Callable[[discord.Interaction], Awaitable[None]],
        on_close: Callable[[], Awaitable[None]],
        timeout: float,
    ) -> None:
        super().__init__(timeout=timeout)
        self._author_id = author_id
        self._on_accept = on_accept
        self._on_decline = on_decline
        self._on_close = on_close
        self._resolved = False

    async def _is_author(self, interaction: discord.Interaction) -> bool:
        if interaction.user is not None and interaction.user.id == self._author_id:
            return True
        await interaction.response.send_message(
            "This privacy prompt isn't for you.", ephemeral=True
        )
        return False

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success, emoji="✅")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self._is_author(interaction):
            return
        self._resolved = True
        self.stop()
        await self._on_accept(interaction)

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.secondary, emoji="✖️")
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self._is_author(interaction):
            return
        self._resolved = True
        self.stop()
        await self._on_decline(interaction)

    async def on_timeout(self) -> None:
        if self._resolved:
            return
        await self._on_close()


class PrivacyConsentGate:
    """Decides whether an inbound message must be gated, and drives the prompt."""

    def __init__(
        self,
        *,
        enabled: bool,
        title: str,
        text: str,
        timeout: float,
        preference_store: ConsentPreferenceStore,
        redispatch: RedispatchCallback,
    ) -> None:
        self._enabled = enabled
        self._title = title
        self._text = text
        self._timeout = timeout
        self._store = preference_store
        self._redispatch = redispatch
        # Users with an open prompt, so a burst of mentions posts only one notice.
        self._pending: set[str] = set()

    async def maybe_prompt(self, message: discord.Message) -> bool:
        """Return True when the message is gated and the caller must stop.

        Returns False to let the normal response flow continue, either because the
        feature is off or because the user has already consented.
        """
        if not self._enabled:
            return False
        user_id = str(message.author.id)
        if user_id in self._pending:
            return True
        # Reserve synchronously so two near-simultaneous messages can't both prompt.
        self._pending.add(user_id)
        try:
            if await self._store.has_consented(user_id):
                self._pending.discard(user_id)
                return False
            await self._post(message, user_id)
            return True
        except Exception:
            self._pending.discard(user_id)
            log.exception("Privacy consent gate failed for user %s", user_id)
            # Fail closed: never fall through to the provider when we couldn't gate.
            return True

    async def _post(self, message: discord.Message, user_id: str) -> None:
        sent: dict[str, discord.Message] = {}

        async def on_accept(interaction: discord.Interaction) -> None:
            await self._accept(message, user_id, interaction)

        async def on_decline(interaction: discord.Interaction) -> None:
            await self._decline(user_id, interaction)

        async def on_close() -> None:
            self._pending.discard(user_id)
            prompt = sent.get("message")
            if prompt is not None:
                try:
                    await prompt.edit(view=None)
                except discord.HTTPException:
                    log.debug("Could not disable expired consent prompt", exc_info=True)

        view = PrivacyConsentView(
            author_id=message.author.id,
            on_accept=on_accept,
            on_decline=on_decline,
            on_close=on_close,
            timeout=self._timeout,
        )
        sent["message"] = await message.reply(
            embed=build_consent_embed(title=self._title, text=self._text),
            view=view,
        )

    async def _accept(
        self, message: discord.Message, user_id: str, interaction: discord.Interaction
    ) -> None:
        try:
            await self._store.set_consent(user_id, True)
        finally:
            # Always release the reservation: a failed consent write must not
            # leave the user stuck in _pending, silently dropping every later
            # message until restart. The gate simply reappears on next mention.
            self._pending.discard(user_id)
        try:
            await interaction.response.edit_message(embed=_accepted_embed(self._title), view=None)
        except discord.HTTPException:
            # Cosmetic only (e.g. expired interaction token); consent is
            # recorded, so still answer the original message below.
            log.warning("Could not edit consent prompt after accept", exc_info=True)
        # Consent is now recorded, so re-running the normal path answers the
        # original message instead of re-prompting.
        await self._redispatch(message)

    async def _decline(self, user_id: str, interaction: discord.Interaction) -> None:
        # Decline is not a permanent block: leave consent unset so the gate
        # reappears on the user's next mention.
        self._pending.discard(user_id)
        await interaction.response.edit_message(embed=_declined_embed(self._title), view=None)
