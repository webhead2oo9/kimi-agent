"""Run Activity chats through the same foreground pipeline as guild messages."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from aiohttp import web

from agent.activity import ActivityUpdate, tool_display_label
from agent.turn import TurnPreparationInput, TurnResult
from app.admission import TURN_ADMISSION_BUSY_MESSAGE, TurnAdmissionController
from app.cancellation import ActiveOperationRegistry
from app.coding_delivery import CodingTaskController
from app.dashboard_access import DashboardAccess
from app.dashboard_files import DashboardAttachment, DashboardFiles
from app.foreground_turn import (
    CommittedMessageCallback,
    DeliveredReply,
    ForegroundTurnInvocation,
    ForegroundTurnRunner,
    TurnDeliveryReceipt,
    TurnSurfaceOutcome,
)
from app.root_locks import RootLockPool
from app.turn_entry import TurnEntryHooks
from config.settings import Settings
from discord_adapter.gateway import DiscordGateway
from storage.conversations import ChannelMessageRecord
from storage.dashboard import DashboardBusyError, DashboardConversation, DashboardStore
from tools.embeds import embed_transcript_summary
from tools.registry import TaskPreviewRequest
from utils.asyncio import await_uncancellable
from utils.privacy_barrier import PrivacyDeletionPendingError, UserPrivacyBarrier
from workspace import workspace_owner_key

log = logging.getLogger(__name__)
PreviewBuilder = Callable[[DashboardConversation, TaskPreviewRequest], Awaitable[dict[str, Any]]]
_MESSAGE_BOUND_TOOLS = frozenset(
    {
        "move_to_thread",
        "leave_thread",
        "pause_thread_replies",
        "resume_thread_replies",
    }
)


@dataclass(frozen=True, slots=True)
class DashboardMessageSource:
    # An attachment-cache identifier, never a Discord message or snowflake.
    id: str
    content: str
    author: Any
    channel: Any
    guild: Any
    attachments: list[DashboardAttachment]
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    reference: None = None


class DashboardActivityReporter:
    committed_message_id: int | None = None

    def __init__(self, store: DashboardStore, chat_id: str, turn_id: str) -> None:
        self.store, self.chat_id, self.turn_id = store, chat_id, turn_id
        self._count = 0
        self._last = ""

    async def __call__(self, update: ActivityUpdate, /) -> None:
        label = tool_display_label(update.tool) if update.tool else update.label
        if label == self._last or self._count >= 250:
            return
        self._last = label
        await self._event("activity", {"label": label[:240]})

    async def commit_step(self, narration: str, tool_names: list[str]) -> None:
        await self._event("activity", {"label": narration[:1500]})

    async def update_plan(self, steps: list[dict[str, str]]) -> None:
        await self._event(
            "plan",
            {
                "steps": [
                    {
                        "content": str(step.get("content", ""))[:500],
                        "status": str(step.get("status", "pending")),
                    }
                    for step in steps[:30]
                ]
            },
        )

    async def _event(self, kind: str, payload: dict[str, Any]) -> None:
        if self._count >= 250:
            return
        self._count += 1
        await self.store.event(self.chat_id, kind, {**payload, "turn_id": self.turn_id})

    async def finish(self) -> None:
        pass


@dataclass(slots=True)
class DashboardTurnAdapter:
    store: DashboardStore
    files: DashboardFiles
    gateway: DiscordGateway
    coding: CodingTaskController
    preview: PreviewBuilder
    chat: DashboardConversation
    turn_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    handoff_id: str | None = None
    delivered: bool = False
    activity_must_finish_before_delivery: bool = False

    def make_activity_reporter(
        self, *, on_committed_message: CommittedMessageCallback
    ) -> DashboardActivityReporter:
        return DashboardActivityReporter(self.store, self.chat.id, self.turn_id)

    def bind_turn_source(self, source: TurnPreparationInput) -> AbstractContextManager[None]:
        @contextmanager
        def bound() -> Iterator[None]:
            binding = self.gateway.bind_turn_source(
                source.conversation_key, "", source.source_message
            )
            try:
                yield
            finally:
                self.gateway.unbind_turn_source(binding)

        return bound()

    async def deliver(self, result: TurnResult, *, conversation_id: int) -> TurnDeliveryReceipt:
        text = result.response_text
        safe = not result.blocked_by_moderation and result.termination_reason != "attachment_error"
        if safe and result.outbox.embed:
            text = "\n\n".join(filter(None, (text, embed_transcript_summary(result.outbox.embed))))
        self.payload = {"text": text, "files": []}
        if safe:
            self.payload["files"] = await self.files.snapshot(
                self.chat, result.outbox.output_files, workspace_guard_held=True
            )
            if result.outbox.task_preview:
                self.payload["task_preview"] = await self.preview(
                    self.chat, result.outbox.task_preview
                )
            handoff = result.outbox.terminal_handoff
            if handoff and handoff.reason == "coding_task" and handoff.task_id:
                self.handoff_id = handoff.task_id
                if not await self.coding.prepare_handoff(handoff.task_id):
                    raise RuntimeError("Coding handoff is no longer available")
                self.payload["coding_task_id"] = handoff.task_id
        return TurnDeliveryReceipt(
            replies=(DeliveredReply(None, text, source_id=f"dashboard:{self.turn_id}:assistant"),),
            context_channel_id=self.chat.channel_id,
            requires_persistence=True,
            persist_replies=self.persist_replies,
        )

    async def persist_replies(self, replies: list[ChannelMessageRecord]) -> None:
        # The transcript, visible acknowledgement and coding claimability share
        # one commit. The coding claim loop also polls, so no wakeup is required
        # to recover a lost notification after this transaction.
        async def commit() -> None:
            await self.store.finish_turn(
                self.chat.id,
                self.turn_id,
                "completed",
                self.payload,
                replies=replies,
                handoff_id=self.handoff_id,
            )
            self.delivered = True

        await await_uncancellable(commit())

    async def finish(self, outcome: TurnSurfaceOutcome) -> None:
        if self.handoff_id and not self.delivered and self.coding.running:
            await self.coding.cancel_task(
                self.handoff_id, reason="Dashboard acknowledgement was interrupted"
            )


class DashboardTurns:
    def __init__(
        self,
        *,
        store: DashboardStore,
        files: DashboardFiles,
        access: DashboardAccess,
        runner: ForegroundTurnRunner,
        gateway: DiscordGateway,
        coding: CodingTaskController,
        preview: PreviewBuilder,
        operations: ActiveOperationRegistry,
        privacy: UserPrivacyBarrier,
        admission: TurnAdmissionController,
        roots: RootLockPool,
        hooks: TurnEntryHooks,
        settings: Settings,
    ) -> None:
        self.store, self.files, self.access, self.runner = store, files, access, runner
        self.gateway, self.coding, self.preview = gateway, coding, preview
        self.operations, self.privacy, self.admission, self.roots = (
            operations,
            privacy,
            admission,
            roots,
        )
        self.hooks, self.settings = hooks, settings
        self._tasks: set[asyncio.Task[None]] = set()
        self._generation: dict[str, int] = {}
        self._deleting: set[str] = set()
        self._closed = False

    async def delete_user(self, user_id: str) -> None:
        self._generation[user_id] = self._generation.get(user_id, 0) + 1

    async def submit(
        self,
        chat: DashboardConversation,
        *,
        request_id: str,
        text: str,
        file_ids: list[str],
    ) -> str:
        if self._closed:
            raise web.HTTPServiceUnavailable(reason="The bot is shutting down")
        if chat.id in self._deleting:
            raise web.HTTPGone(reason="This conversation is being deleted")
        if (
            not request_id
            or len(request_id) > 100
            or len(text) > self.settings.dashboard_max_message_chars
        ):
            raise web.HTTPBadRequest(reason="Message or request identifier is too long")
        if not text.strip() and not file_ids:
            raise web.HTTPBadRequest(reason="Add a message or attachment first")
        # The worker owns acceptance and all subsequent work. Disconnecting the
        # HTTP request cannot cancel an accepted turn or lose an admission lease.
        accepted: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        generation = self._generation.get(chat.user_id, 0)
        task = asyncio.create_task(
            self._run(chat, request_id, text, file_ids, generation, accepted),
            name=f"dashboard:{chat.id}",
        )
        self._tasks.add(task)

        def done(completed: asyncio.Task[None]) -> None:
            self._tasks.discard(completed)
            if not completed.cancelled():
                completed.exception()
            if accepted.done() and not accepted.cancelled():
                accepted.exception()

        task.add_done_callback(done)
        return await asyncio.shield(accepted)

    async def _run(
        self,
        chat: DashboardConversation,
        request_id: str,
        text: str,
        file_ids: list[str],
        generation: int,
        accepted: asyncio.Future[str],
    ) -> None:
        stop = asyncio.Event()
        try:
            with self.operations.register_provisional(
                user_id=chat.user_id, channel_id=chat.channel_id, stop_event=stop
            ):
                self.operations.bind_current_provisional(chat.key)
                async with self.privacy.activity(chat.user_id):
                    await self._run_leased(
                        chat, request_id, text, file_ids, generation, accepted, stop
                    )
        except BaseException:
            if not accepted.done():
                accepted.set_exception(
                    web.HTTPServiceUnavailable(reason="This request expired. Reopen the dashboard")
                )
            raise

    async def _run_leased(
        self,
        chat: DashboardConversation,
        request_id: str,
        text: str,
        file_ids: list[str],
        generation: int,
        accepted: asyncio.Future[str],
        stop: asyncio.Event,
    ) -> None:
        turn_id: str | None = None
        try:
            if generation != self._generation.get(chat.user_id, 0):
                raise web.HTTPGone(reason="This request expired after data deletion")
            duplicate = await self.store.accepted(chat.id, request_id)
            if duplicate:
                accepted.set_result(duplicate)
                return
            decision = await self.admission.try_acquire(chat.user_id)
            if decision.lease is None:
                raise web.HTTPTooManyRequests(reason=TURN_ADMISSION_BUSY_MESSAGE)
            async with decision.lease:
                async with asyncio.timeout(self.settings.dashboard_turn_timeout_seconds):
                    async with self.roots.hold(chat.key):
                        current = await self.store.get(
                            chat.id, user_id=chat.user_id, guild_id=chat.guild_id
                        )
                        if current is None or chat.id in self._deleting:
                            raise web.HTTPNotFound(reason="Conversation no longer exists")
                        ctx = await self.access.resolve(
                            user_id=chat.user_id,
                            guild_id=chat.guild_id,
                            channel_id=chat.channel_id,
                            continuing=True,
                        )
                        if await self.access.consent_required(chat.user_id):
                            raise web.HTTPForbidden(
                                reason="Accept the privacy notice before chatting"
                            )
                        attachments, records = await self.files.attachments(chat, file_ids)
                        turn_id, fresh = await self.store.accept(
                            chat, request_id=request_id, text=text, files=records
                        )
                        accepted.set_result(turn_id)
                        if not fresh:
                            return
                        await self.store.start_turn(turn_id)
                        source = DashboardMessageSource(
                            turn_id,
                            text,
                            ctx.member,
                            ctx.channel,
                            ctx.member.guild,
                            attachments,
                        )
                        turn = TurnPreparationInput(
                            trigger_source_id=f"dashboard:{turn_id}:user",
                            raw_content=text,
                            source_message=source,
                            bot_user=self.access.bot.user,
                            guild_id=chat.guild_id,
                            guild_name=ctx.member.guild.name,
                            channel_id=chat.channel_id,
                            parent_channel_id=chat.parent_channel_id,
                            thread_id=chat.channel_id
                            if chat.parent_channel_id != chat.channel_id
                            else None,
                            channel_name=chat.channel_name,
                            user_id=chat.user_id,
                            user_name=ctx.member.display_name,
                            trust_tier=ctx.tier,
                            conversation_key=chat.key,
                            conversation_owner_user_id=chat.user_id,
                            conversation_access_scope="owner_only",
                            workspace_key=workspace_owner_key(chat.user_id, chat.guild_id),
                        )
                        adapter = DashboardTurnAdapter(
                            self.store,
                            self.files,
                            self.gateway,
                            self.coding,
                            self.preview,
                            chat,
                            turn_id,
                        )
                        await self.runner.run(
                            ForegroundTurnInvocation(
                                source=turn,
                                prepared_user_discord_message_id=None,
                                prepared_user_source_id=f"dashboard:{turn_id}:user",
                                prepared_user_source_created_at=source.created_at.timestamp(),
                                prepared_user_context_channel_id=chat.channel_id,
                                collect_reply_context=self.hooks.collect_reply_context,
                                strip_mention=lambda content, **_kwargs: content.strip(),
                                stop_event=stop,
                                existing_conversation_id=chat.conversation_id,
                                hooks=self.hooks,
                                command_template="dashboard",
                                extra_blocked_tools=_MESSAGE_BOUND_TOOLS,
                                recent_image_lookback=0,
                                timeout_seconds=self.settings.dashboard_turn_timeout_seconds,
                            ),
                            adapter=adapter,
                        )
                        if not adapter.delivered:
                            await self.store.finish_turn(
                                chat.id,
                                turn_id,
                                "failed",
                                {
                                    "text": "No response was produced. Send another message to continue."
                                },
                            )
        except BaseException as exc:
            if not accepted.done():
                if isinstance(exc, DashboardBusyError):
                    error: BaseException = web.HTTPConflict(reason=str(exc))
                elif isinstance(exc, web.HTTPException):
                    error = exc
                else:
                    error = web.HTTPServiceUnavailable(
                        reason="The response could not start. Try again shortly"
                    )
                accepted.set_exception(error)
            if turn_id:
                status = "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed"
                message = (
                    "Response stopped. Partial file changes were kept."
                    if status == "cancelled"
                    else "The response was interrupted. Send a new message to continue."
                )
                await await_uncancellable(
                    self.store.finish_turn(chat.id, turn_id, status, {"text": message})
                )
            if not isinstance(
                exc,
                web.HTTPException
                | DashboardBusyError
                | PrivacyDeletionPendingError
                | asyncio.CancelledError
                | TimeoutError,
            ):
                log.exception("Dashboard turn failed")
            if isinstance(exc, asyncio.CancelledError):
                raise

    async def stop(self, chat: DashboardConversation) -> bool:
        _, clean = await self.operations.cancel(
            user_id=chat.user_id,
            root_key=chat.key,
            channel_id=chat.channel_id,
            all_operations=False,
            wait_seconds=10,
        )
        return clean

    async def delete(self, chat: DashboardConversation) -> None:
        if chat.id in self._deleting:
            raise web.HTTPConflict(reason="This conversation is already being deleted")
        self._deleting.add(chat.id)
        try:
            if not await self.stop(chat):
                raise web.HTTPConflict(reason="Work is still stopping. Try deleting again shortly")
            # A coding finalizer publishes from a child task under this root.
            # Drain it before taking the root; new foreground handoffs are
            # fenced by _deleting while cancellation and deletion run.
            if self.coding.running:
                _, clean = await self.coding.cancel_for_conversations([chat.conversation_id])
                if not clean:
                    raise web.HTTPConflict(
                        reason="Coding work is still stopping. Try again shortly"
                    )
            async with self.roots.hold(chat.key):
                await self.files.delete_conversation(chat)
        finally:
            self._deleting.discard(chat.id)

    async def close(self) -> None:
        self._closed = True
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
