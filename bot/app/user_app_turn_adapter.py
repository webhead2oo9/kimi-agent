from __future__ import annotations

import logging
from contextlib import AbstractContextManager, nullcontext, suppress
from dataclasses import dataclass

import discord

from agent.turn import TurnPreparationInput, TurnResult
from app.foreground_turn import (
    CommittedMessageCallback,
    DeliveredReply,
    ForegroundActivityReporter,
    TurnDeliveryReceipt,
    TurnSurfaceOutcome,
    TurnSurfaceOutcomeKind,
)
from discord_adapter.interaction_io import (
    PartialPublicDeliveryError,
    send_interaction_result,
    send_interaction_status,
)
from discord_adapter.io import InteractionActivityReporter
from tools.embeds import embed_transcript_summary

log = logging.getLogger(__name__)

_NO_RESULT_STATUS = "There wasn't anything I could process in that request."
_PARTIAL_PUBLIC_DELIVERY_STATUS = (
    "I posted the first part, but couldn't deliver the complete response."
)
_PARTIAL_PRIVATE_DELIVERY_STATUS = "I delivered part of the response, but couldn't send the rest."
_DELIVERY_FAILURE_STATUS = (
    "I finished the turn but couldn't deliver the response here. Try again privately."
)


@dataclass(frozen=True, slots=True)
class UserAppInteractionTurnAdapter:
    """Adapt a deferred personal-chat interaction to a foreground turn."""

    interaction: discord.Interaction
    requested_public: bool
    context_channel_id: str

    @property
    def activity_must_finish_before_delivery(self) -> bool:
        # Activity and the final result edit the same deferred response, so all
        # pending activity paints must be quiescent before delivery starts.
        return True

    def make_activity_reporter(
        self,
        *,
        on_committed_message: CommittedMessageCallback,
    ) -> ForegroundActivityReporter:
        # Interaction narration never creates a gateway message to map.
        _ = on_committed_message
        return InteractionActivityReporter(self.interaction)

    def bind_turn_source(
        self,
        source: TurnPreparationInput,
    ) -> AbstractContextManager[None]:
        # User-app interactions have no gateway source binding.
        _ = source
        return nullcontext()

    async def deliver(
        self,
        result: TurnResult,
        *,
        conversation_id: int,
    ) -> TurnDeliveryReceipt:
        _ = conversation_id
        delivered_content: str | None = None
        delivery_failed = False

        async def record_delivered_content(content: str) -> None:
            nonlocal delivered_content
            delivered_content = content

        try:
            await send_interaction_result(
                self.interaction,
                result.response_text,
                ephemeral=not self.requested_public,
                original_ephemeral=not self.requested_public,
                output_files=result.output_files,
                output_file_descriptions=result.output_file_descriptions,
                allowed_file_roots=result.allowed_file_roots,
                embed=result.embed,
                on_primary_delivered=record_delivered_content,
            )
            delivery_failed = delivered_content is None
        except PartialPublicDeliveryError:
            delivery_failed = True
            log.warning(
                "Personal chat public followup delivery was incomplete for user %s",
                self.interaction.user.id,
                exc_info=True,
            )
            with suppress(discord.HTTPException):
                await self.interaction.followup.send(
                    _PARTIAL_PUBLIC_DELIVERY_STATUS,
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        except discord.HTTPException:
            delivery_failed = True
            log.warning(
                "Personal chat result delivery failed for user %s",
                self.interaction.user.id,
                exc_info=True,
            )
            with suppress(discord.HTTPException):
                if delivered_content is None:
                    await self._send_status(_DELIVERY_FAILURE_STATUS)
                else:
                    # The original response is already visible and is the
                    # source of the receipt below. Never replace it with a
                    # status after a later private followup fails.
                    await self.interaction.followup.send(
                        _PARTIAL_PRIVATE_DELIVERY_STATUS,
                        ephemeral=True,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )

        replies: tuple[DeliveredReply, ...] = ()
        if delivered_content is not None:
            transcript_text = delivered_content
            if not transcript_text and result.embed is not None:
                transcript_text = embed_transcript_summary(result.embed)
            if transcript_text:
                replies = (
                    DeliveredReply(
                        discord_message_id=f"userapp:{self.interaction.id}:assistant",
                        content=transcript_text,
                        source_created_at=self.interaction.created_at.timestamp(),
                    ),
                )
        return TurnDeliveryReceipt(
            replies=replies,
            context_channel_id=self.context_channel_id,
            delivery_failed=delivery_failed,
        )

    async def finish(self, outcome: TurnSurfaceOutcome) -> None:
        if outcome.kind is TurnSurfaceOutcomeKind.NO_RESULT:
            await self._send_status(_NO_RESULT_STATUS)

    async def _send_status(self, content: str) -> None:
        await send_interaction_status(
            self.interaction,
            content,
            ephemeral=not self.requested_public,
            original_ephemeral=not self.requested_public,
        )
