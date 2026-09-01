from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, replace
from typing import Protocol

import discord

from agent.auto_handoff import build_auto_handoff_request
from agent.backfill import message_source_timestamp, strip_chunk_marker
from agent.turn import TurnPreparationInput, TurnResult
from app.foreground_turn import (
    CommittedMessageCallback,
    DeliveredReply,
    ForegroundActivityReporter,
    TurnDeliveryReceipt,
    TurnSurfaceOutcome,
)
from app.coding_delivery import CodingHandoffControl, MessageInvocationStripper
from app.response_delivery import DiscordResponseSender
from app.thread_handoff_boundary import THREAD_HANDOFF_REACTION, ThreadHandoffBoundary
from app.threads import ThreadHandoffManager
from config.fragments.channel_pins import load_channel_auto_thread
from discord_adapter.gateway import DiscordGateway
from discord_adapter.io import DiscordActivityReporter
from tools.embeds import embed_transcript_summary

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class GuildTurnDeliveryConfig:
    thread_auto_handoff_enabled: bool
    thread_handoff_enabled: bool
    bot_name: str


class BotUserProvider(Protocol):
    def __call__(self) -> discord.ClientUser | None: ...


@dataclass(frozen=True, slots=True)
class GuildTurnCollaborators:
    config: GuildTurnDeliveryConfig
    gateway: DiscordGateway
    threads: ThreadHandoffBoundary
    thread_handoff: ThreadHandoffManager | None
    coding: CodingHandoffControl
    responses: DiscordResponseSender
    bot_user: BotUserProvider
    strip_invocation: MessageInvocationStripper


@dataclass(frozen=True, slots=True)
class GuildMessageTurnAdapter:
    """Adapt a gateway message to guild and personal-DM turn delivery."""

    collaborators: GuildTurnCollaborators
    message: discord.Message
    context_channel_id: str
    personal_chat: bool

    @property
    def activity_must_finish_before_delivery(self) -> bool:
        return False

    def make_activity_reporter(
        self,
        *,
        on_committed_message: CommittedMessageCallback,
    ) -> ForegroundActivityReporter:
        return DiscordActivityReporter(
            self.message.channel,
            reference=self.message,
            on_committed_message=on_committed_message,
        )

    def bind_turn_source(
        self,
        source: TurnPreparationInput,
    ) -> AbstractContextManager[None]:
        @contextmanager
        def bound_source() -> Iterator[None]:
            source_binding = self.collaborators.gateway.bind_turn_source(
                source.conversation_key,
                source.trigger_discord_message_id,
                source.source_message,
            )
            try:
                yield
            finally:
                self.collaborators.gateway.unbind_turn_source(source_binding)

        return bound_source()

    async def deliver(
        self,
        result: TurnResult,
        *,
        conversation_id: int,
    ) -> TurnDeliveryReceipt:
        collaborators = self.collaborators
        message = self.message
        target_channel: discord.abc.Messageable = message.channel
        original_target_channel = target_channel
        delivery_result = result
        coding_handoff_task_id: str | None = None
        coding_handoff_prepared = False
        coding_handoff_finalized = False

        if result.terminal_handoff is not None and result.terminal_handoff.reason == "coding_task":
            coding_handoff_task_id = result.terminal_handoff.task_id

        try:
            reply_reference: discord.Message | None = message
            # Model and deterministic handoffs share one creation path so live
            # policy, enrollment, and failure cleanup cannot drift.
            thread_request = delivery_result.thread_request
            if (
                delivery_result.blocked_by_moderation
                or delivery_result.termination_reason == "attachment_error"
            ):
                thread_request = None
            elif (
                thread_request is None
                and collaborators.config.thread_auto_handoff_enabled
                and collaborators.config.thread_handoff_enabled
                and collaborators.thread_handoff is not None
                and not isinstance(message.channel, discord.Thread)
            ):
                channel_id = str(message.channel.id)
                auto_cfg = (
                    load_channel_auto_thread(channel_id)
                    if collaborators.threads.thread_handoff_creation_allowed(message)
                    else None
                )
                if auto_cfg is not None:
                    thread_request = build_auto_handoff_request(
                        response_text=delivery_result.response_text,
                        question_text=collaborators.strip_invocation(
                            message.content,
                            bot_user=collaborators.bot_user(),
                        ),
                        bot_name=collaborators.config.bot_name,
                        min_lines=auto_cfg.min_lines,
                        min_chars=auto_cfg.min_chars,
                        always=auto_cfg.always,
                    )

            handoff_thread: discord.Thread | None = None
            cross_channel = False
            if thread_request is not None:
                handoff_thread = await collaborators.threads.create_handoff_thread(
                    message,
                    thread_request,
                    conversation_id,
                )
                if handoff_thread is not None:
                    cross_channel = thread_request.target_channel_id is not None
                    target_channel = handoff_thread
                    reply_reference = None

            if coding_handoff_task_id is not None:
                if isinstance(target_channel, discord.Thread):
                    parent_id = getattr(target_channel, "parent_id", None)
                    route_channel_id = (
                        str(parent_id) if parent_id is not None else self.context_channel_id
                    )
                    route_thread_id = str(target_channel.id)
                else:
                    route_channel_id = str(getattr(target_channel, "id", self.context_channel_id))
                    route_thread_id = None
                coding_handoff_prepared = await collaborators.coding.prepare_handoff(
                    coding_handoff_task_id,
                    channel_id=route_channel_id,
                    thread_id=route_thread_id,
                )
                if not coding_handoff_prepared:
                    delivery_result = replace(
                        delivery_result,
                        response_text=(
                            f"Coding task `{coding_handoff_task_id[:8]}` was cancelled "
                            "before it started."
                        ),
                    )

            if handoff_thread is not None:
                await collaborators.gateway.add_status_reaction(
                    message,
                    THREAD_HANDOFF_REACTION,
                )

            # ForegroundTurnRunner already holds the workspace activity guard.
            # Leaving the key unset here avoids reacquiring that same lock while
            # preserving the app-instance send seam used by routing tests.
            sent_messages = await collaborators.responses.send(
                target_channel,
                delivery_result.response_text,
                reference=reply_reference,
                output_files=list(delivery_result.output_files),
                output_file_descriptions=dict(delivery_result.output_file_descriptions),
                allowed_file_roots=list(delivery_result.allowed_file_roots),
                embed=delivery_result.embed,
                mention_author=True,
            )

            initial_handoff_delivery_failed = bool(
                not sent_messages or getattr(sent_messages, "delivery_failed", False)
            )
            if (
                coding_handoff_task_id is not None
                and coding_handoff_prepared
                and handoff_thread is not None
                and initial_handoff_delivery_failed
            ):
                task = await collaborators.coding.failed_handoff_task(coding_handoff_task_id)
                if task is not None:
                    await collaborators.coding.delete_status_message(
                        handoff_thread,
                        task,
                        collaborators.coding.task_marker(task.id),
                    )
                if collaborators.thread_handoff is not None:
                    await collaborators.thread_handoff.prune(handoff_thread.id)
                if cross_channel:
                    await collaborators.threads.discard_cross_channel_thread(handoff_thread)

                target_channel = original_target_channel
                reply_reference = message
                handoff_thread = None
                cross_channel = False
                fallback_channel_id = str(
                    getattr(original_target_channel, "id", self.context_channel_id)
                )
                fallback_thread_id = (
                    str(original_target_channel.id)
                    if isinstance(original_target_channel, discord.Thread)
                    else None
                )
                coding_handoff_prepared = await collaborators.coding.prepare_handoff(
                    coding_handoff_task_id,
                    channel_id=fallback_channel_id,
                    thread_id=fallback_thread_id,
                )
                if coding_handoff_prepared:
                    sent_messages = await collaborators.responses.send(
                        target_channel,
                        delivery_result.response_text,
                        reference=reply_reference,
                        output_files=list(delivery_result.output_files),
                        output_file_descriptions=dict(delivery_result.output_file_descriptions),
                        allowed_file_roots=list(delivery_result.allowed_file_roots),
                        embed=delivery_result.embed,
                        mention_author=True,
                    )

            if coding_handoff_task_id is not None and coding_handoff_prepared:
                coding_handoff_finalized = await collaborators.coding.release_handoff(
                    coding_handoff_task_id
                )

            expected_delivery = bool(
                delivery_result.response_text.strip()
                or delivery_result.embed is not None
                or delivery_result.output_files
            )
            if (
                collaborators.thread_handoff is not None
                and isinstance(target_channel, discord.Thread)
                and collaborators.thread_handoff.is_managed(target_channel.id)
                and not sent_messages
                and expected_delivery
            ):
                await collaborators.thread_handoff.prune(target_channel.id)
                if cross_channel:
                    await collaborators.threads.discard_cross_channel_thread(target_channel)
            elif cross_channel and handoff_thread is not None and sent_messages:
                await collaborators.threads.send_cross_channel_pointer(message, handoff_thread)

            replies: list[DeliveredReply] = []
            embed_summary = (
                embed_transcript_summary(delivery_result.embed)
                if delivery_result.embed is not None
                else ""
            )
            for index, sent in enumerate(sent_messages):
                content = strip_chunk_marker(sent.content)
                if index == 0 and not content and embed_summary:
                    content = embed_summary
                replies.append(
                    DeliveredReply(
                        discord_message_id=str(sent.id),
                        content=content,
                        source_created_at=message_source_timestamp(sent),
                    )
                )

            sent_channel = getattr(sent_messages[0], "channel", None) if sent_messages else None
            persist_channel_id = (
                self.context_channel_id
                if self.personal_chat or sent_channel is None
                else str(sent_channel.id)
            )
            if delivery_result.thread_close_request is not None:
                try:
                    await collaborators.threads.close_handoff_thread(
                        target_channel,
                        delivery_result.thread_close_request,
                    )
                except Exception:
                    # The reply is already visible. Preserve its receipt so the
                    # transcript matches Discord even if thread cleanup fails.
                    log.exception(
                        "Could not close handoff thread %s after delivering conversation %s",
                        delivery_result.thread_close_request.thread_id,
                        conversation_id,
                    )
            partial_delivery_failed = bool(getattr(sent_messages, "delivery_failed", False))
            delivery_failed = bool(
                expected_delivery
                and (not sent_messages or partial_delivery_failed)
                and not delivery_result.blocked_by_moderation
            )
            return TurnDeliveryReceipt(
                replies=tuple(replies),
                context_channel_id=persist_channel_id,
                delivery_failed=delivery_failed,
                delivered_result=(delivery_result if delivery_result is not result else None),
            )
        finally:
            if coding_handoff_task_id is not None and not coding_handoff_finalized:
                try:
                    await collaborators.coding.finalize_handoff(coding_handoff_task_id)
                except Exception:
                    log.exception(
                        "Could not release coding task %s after foreground routing failed",
                        coding_handoff_task_id,
                    )

    async def finish(self, outcome: TurnSurfaceOutcome) -> None:
        # Outcome reactions are owned by _on_message_for_user while it still
        # holds the root lock; moving them here would change that boundary.
        _ = outcome
