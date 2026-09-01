from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress

import discord

from storage.preferences import PreferenceStore

log = logging.getLogger(__name__)


class UserAppConsentView(discord.ui.View):
    """Ephemeral consent prompt that resumes a retained interaction request."""

    def __init__(
        self,
        *,
        author_id: int,
        store: PreferenceStore,
        on_accept: Callable[[discord.Interaction], Awaitable[None]],
        timeout: float,
        public_response: bool = False,
    ) -> None:
        super().__init__(timeout=timeout)
        self._author_id = author_id
        self._store = store
        self._on_accept = on_accept
        self._public_response = public_response
        self._claimed = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self._author_id:
            return True
        await interaction.response.send_message(
            "This privacy prompt belongs to someone else.", ephemeral=True
        )
        return False

    @discord.ui.button(label="Accept and continue", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if self._claimed:
            await interaction.response.send_message("This prompt was already used.", ephemeral=True)
            return
        self._claimed = True
        self.stop()
        try:
            await self._store.set_consent(str(self._author_id), True)
            # thinking=True creates a fresh deferred command-style response for the
            # component interaction. Its visibility carries through live activity,
            # the final result, and any post-defer failure.
            await interaction.response.defer(ephemeral=not self._public_response, thinking=True)
        except Exception:
            log.exception("User-app privacy consent acceptance failed for user %s", self._author_id)
            with suppress(discord.HTTPException):
                await interaction.response.edit_message(
                    content="I couldn't save your privacy choice. Please try again.",
                    embed=None,
                    view=None,
                )
            return
        await self._on_accept(interaction)

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.secondary)
    async def decline(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if self._claimed:
            await interaction.response.send_message("This prompt was already used.", ephemeral=True)
            return
        self._claimed = True
        self.stop()
        await interaction.response.edit_message(
            content="No problem. I didn't run or store that chat request.",
            embed=None,
            view=None,
        )
