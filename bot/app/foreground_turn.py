from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import AbstractContextManager, asynccontextmanager
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Protocol

from agent.activity import ActivityUpdate
from agent.turn import (
    CollectReplyContext,
    CollectTurnAttachments,
    CountUserPriorMessages,
    PersistPreparedUserMessage,
    StripMention,
    TurnDependencies,
    TurnExecutionConfig,
    TurnPreparationConfig,
    TurnPreparationInput,
    TurnRequest,
    TurnResult,
    handle_turn,
)
from app.cancellation import ActiveOperationRegistry
from app.turn_entry import (
    TurnDependencyFactory,
    TurnEntryHooks,
    build_turn_preparation_config,
)
from config.settings import Settings
from providers.types import ContentPart
from storage.conversations import (
    CHANNEL_SHARED,
    ChannelMessageRecord,
    ConversationAccessScope,
    ConversationStore,
)
from tools.workspace.common import UserLocks
from utils.format import sanitize_author_name
from utils.privacy_barrier import UserPrivacyBarrier
from workspace import WorkspaceKey

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TurnConversationSpec:
    key: str
    channel_name: str
    guild_id: str | None
    channel_id: str
    thread_id: str | None
    root_discord_message_id: str
    owner_user_id: str | None = None
    access_scope: ConversationAccessScope = CHANNEL_SHARED
    existing_conversation_id: int | None = None


@dataclass(frozen=True, slots=True)
class ForegroundTurnInvocation:
    conversation: TurnConversationSpec
    source: TurnPreparationInput
    prepared_user_discord_message_id: str
    prepared_user_source_created_at: float | None
    prepared_user_context_channel_id: str
    collect_reply_context: CollectReplyContext
    strip_mention: StripMention
    stop_event: asyncio.Event
    hooks: TurnEntryHooks = field(default_factory=TurnEntryHooks)
    collect_turn_attachments: CollectTurnAttachments | None = None
    count_user_prior_messages: CountUserPriorMessages | None = None
    command_template: str | None = None
    new_user_onboarding_turns: int = 0
    timeout_seconds: float | None = None
    thread_handoff_suggest_after_tool_calls: int = 0
    extra_blocked_tools: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class DeliveredReply:
    discord_message_id: str
    content: str
    source_created_at: float | None = None


@dataclass(frozen=True, slots=True)
class TurnDeliveryReceipt:
    replies: tuple[DeliveredReply, ...] = ()
    context_channel_id: str = ""
    delivery_failed: bool = False
    # Set only when the surface had to deliver something other than the
    # model's result (a cancelled coding handoff acknowledgement, for
    # instance). The runner then returns and reports that result; the
    # transcript already comes from `replies`, so this is not a persistence
    # hook.
    delivered_result: TurnResult | None = None


class TurnSurfaceOutcomeKind(StrEnum):
    NO_RESULT = "no_result"
    DELIVERED = "delivered"
    DELIVERY_FAILED = "delivery_failed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class TurnSurfaceOutcome:
    kind: TurnSurfaceOutcomeKind
    conversation_id: int | None
    result: TurnResult | None = None
    receipt: TurnDeliveryReceipt | None = None


CommittedMessageCallback = Callable[[int], Awaitable[None]]


class ForegroundActivityReporter(Protocol):
    @property
    def committed_message_id(self) -> int | None: ...

    async def __call__(self, update: ActivityUpdate, /) -> None: ...

    async def finish(self) -> None: ...


class ForegroundTurnAdapter(Protocol):
    @property
    def activity_must_finish_before_delivery(self) -> bool: ...

    def make_activity_reporter(
        self,
        *,
        on_committed_message: CommittedMessageCallback,
    ) -> ForegroundActivityReporter: ...

    def bind_turn_source(
        self,
        source: TurnPreparationInput,
    ) -> AbstractContextManager[None]: ...

    async def deliver(
        self,
        result: TurnResult,
        *,
        conversation_id: int,
    ) -> TurnDeliveryReceipt: ...

    async def finish(self, outcome: TurnSurfaceOutcome) -> None: ...


class HandleTurn(Protocol):
    async def __call__(
        self,
        source: TurnPreparationInput,
        /,
        *,
        dependencies: TurnDependencies,
        preparation_config: TurnPreparationConfig,
        execution_config: TurnExecutionConfig,
    ) -> TurnResult | None: ...


async def deliver_with_workspace_guard[T](
    *,
    workspace_locks: UserLocks,
    workspace_key: WorkspaceKey | None,
    output_files: Sequence[str] | None,
    deliver: Callable[[], Awaitable[T]],
) -> T:
    # Only local attachments need their workspace protected until the delivery
    # surface has consumed them; text delivery must not wait on a durable writer.
    if workspace_key and output_files:
        async with workspace_locks.activity(workspace_key):
            return await deliver()
    return await deliver()


class ForegroundTurnRunner:
    """Run the shared foreground turn lifecycle behind a surface adapter.

    The caller holds the root lock, admission lease, privacy activity lease,
    and a bound provisional active-operation registration. Consent also belongs
    to the caller. This runner owns only the work inside those established gates.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        conversation_store: ConversationStore,
        dependency_factory: TurnDependencyFactory,
        active_operations: ActiveOperationRegistry,
        privacy_barrier: UserPrivacyBarrier,
        workspace_locks: UserLocks,
        handle_turn_hook: HandleTurn = handle_turn,
    ) -> None:
        self._settings = settings
        self._conversation_store = conversation_store
        self._dependency_factory = dependency_factory
        self._active_operations = active_operations
        self._privacy_barrier = privacy_barrier
        self._workspace_locks = workspace_locks
        self._handle_turn = handle_turn_hook

    async def run(
        self,
        invocation: ForegroundTurnInvocation,
        *,
        adapter: ForegroundTurnAdapter,
    ) -> TurnResult | None:
        conversation_id = await self._touch_or_recreate_conversation(invocation.conversation)
        mapped_message_ids: set[str] = set()

        async def map_committed_message(message_id: int) -> None:
            normalized_id = str(message_id)
            if normalized_id in mapped_message_ids:
                return
            try:
                await self._conversation_store.map_message_context(
                    normalized_id,
                    conversation_id,
                    invocation.prepared_user_context_channel_id,
                )
            except Exception:
                log.debug("Could not map foreground activity log route", exc_info=True)
                return
            mapped_message_ids.add(normalized_id)

        activity_reporter = adapter.make_activity_reporter(
            on_committed_message=map_committed_message
        )
        reporter_finished = False
        surface_finished = False

        async def finish_reporter() -> None:
            nonlocal reporter_finished
            if reporter_finished:
                return
            reporter_finished = True
            await activity_reporter.finish()

        async def finish_surface(outcome: TurnSurfaceOutcome) -> None:
            nonlocal surface_finished
            surface_finished = True
            await adapter.finish(outcome)

        async def persist_prepared_user_message(
            _source: TurnPreparationInput,
            turn: TurnRequest,
        ) -> None:
            await self._conversation_store.save_channel_messages(
                conversation_id,
                [
                    ChannelMessageRecord(
                        discord_message_id=invocation.prepared_user_discord_message_id,
                        role="user",
                        author_id=invocation.source.user_id,
                        author_name=sanitize_author_name(invocation.source.user_name),
                        content=turn.content,
                        source_created_at=invocation.prepared_user_source_created_at,
                        content_parts=[
                            ContentPart.from_text(turn.content),
                            *list(turn.input_parts),
                        ],
                    )
                ],
                context_channel_id=invocation.prepared_user_context_channel_id,
            )

        try:
            guarded_dependencies = await self._build_guarded_dependencies(
                invocation,
                activity_reporter=activity_reporter,
                persist_prepared_user_message=persist_prepared_user_message,
            )
            with adapter.bind_turn_source(invocation.source):
                result = await self._handle_turn(
                    invocation.source,
                    dependencies=guarded_dependencies,
                    preparation_config=build_turn_preparation_config(
                        self._settings,
                        recent_image_lookback=self._settings.recent_image_lookback,
                        new_user_onboarding_turns=invocation.new_user_onboarding_turns,
                    ),
                    execution_config=TurnExecutionConfig(
                        max_iterations=self._settings.react_max_iterations,
                        max_tokens=self._settings.react_max_tokens,
                        temperature=self._settings.react_temperature,
                        bot_name=self._settings.bot_name,
                        command_template=invocation.command_template,
                        timeout_seconds=invocation.timeout_seconds,
                        thread_handoff_suggest_after_tool_calls=(
                            invocation.thread_handoff_suggest_after_tool_calls
                        ),
                    ),
                )

            if adapter.activity_must_finish_before_delivery:
                await finish_reporter()
            if result is None:
                await finish_surface(
                    TurnSurfaceOutcome(
                        kind=TurnSurfaceOutcomeKind.NO_RESULT,
                        conversation_id=conversation_id,
                    )
                )
                return None

            turn_result = result
            receipt = await deliver_with_workspace_guard(
                workspace_locks=self._workspace_locks,
                workspace_key=turn_result.workspace_key,
                output_files=turn_result.output_files,
                deliver=lambda: adapter.deliver(turn_result, conversation_id=conversation_id),
            )
            result = receipt.delivered_result or turn_result
            await self._persist_assistant_replies(conversation_id, result, receipt)
            if receipt.delivery_failed and not result.delivery_failed:
                result = replace(result, delivery_failed=True)
            outcome_kind = (
                TurnSurfaceOutcomeKind.DELIVERY_FAILED
                if result.delivery_failed
                else TurnSurfaceOutcomeKind.DELIVERED
            )
            await finish_surface(
                TurnSurfaceOutcome(
                    kind=outcome_kind,
                    conversation_id=conversation_id,
                    result=result,
                    receipt=receipt,
                )
            )
            return result
        except BaseException:
            if not surface_finished:
                try:
                    await finish_surface(
                        TurnSurfaceOutcome(
                            kind=TurnSurfaceOutcomeKind.FAILED,
                            conversation_id=conversation_id,
                        )
                    )
                except Exception:
                    log.exception("Foreground turn adapter could not finish a failed outcome")
            raise
        finally:
            try:
                await finish_reporter()
            finally:
                committed_message_id = activity_reporter.committed_message_id
                if committed_message_id is not None:
                    await map_committed_message(committed_message_id)

    async def _build_guarded_dependencies(
        self,
        invocation: ForegroundTurnInvocation,
        *,
        activity_reporter: ForegroundActivityReporter,
        persist_prepared_user_message: PersistPreparedUserMessage,
    ) -> TurnDependencies:
        dependencies = await self._dependency_factory.build(
            invocation.source,
            collect_reply_context_func=invocation.collect_reply_context,
            strip_mention_func=invocation.strip_mention,
            persist_prepared_user_message=persist_prepared_user_message,
            hooks=invocation.hooks,
            command_template=invocation.command_template,
            collect_turn_attachments_func=invocation.collect_turn_attachments,
            count_user_prior_messages=invocation.count_user_prior_messages,
            activity_reporter=activity_reporter,
            extra_blocked_tools=invocation.extra_blocked_tools,
        )

        @asynccontextmanager
        async def child_activity(activity_user_id: str) -> AsyncIterator[None]:
            async with self._active_operations.register(
                user_id=activity_user_id,
                root_key=invocation.conversation.key,
                channel_id=invocation.prepared_user_context_channel_id,
                cancel_on_stop=False,
                stop_event=invocation.stop_event,
            ):
                async with self._privacy_barrier.activity(activity_user_id):
                    yield

        return replace(
            dependencies,
            user_activity=child_activity,
            stop_event=invocation.stop_event,
        )

    async def _touch_or_recreate_conversation(self, spec: TurnConversationSpec) -> int:
        conversation_id = spec.existing_conversation_id
        if conversation_id is not None and not await self._conversation_store.touch(
            conversation_id
        ):
            # Retention may remove a resolved row before the root lock is entered;
            # recreate the logical root instead of carrying a dead foreign key.
            conversation_id = None
        if conversation_id is not None:
            return conversation_id
        return await self._conversation_store.get_or_create(
            spec.key,
            spec.channel_name,
            guild_id=spec.guild_id,
            channel_id=spec.channel_id,
            thread_id=spec.thread_id,
            root_discord_message_id=spec.root_discord_message_id,
            owner_user_id=spec.owner_user_id,
            access_scope=spec.access_scope,
        )

    async def _persist_assistant_replies(
        self,
        conversation_id: int,
        result: TurnResult,
        receipt: TurnDeliveryReceipt,
    ) -> None:
        if (
            not receipt.replies
            or result.blocked_by_moderation
            or result.termination_reason == "attachment_error"
        ):
            return
        await self._conversation_store.save_channel_messages(
            conversation_id,
            [
                ChannelMessageRecord(
                    discord_message_id=reply.discord_message_id,
                    role="assistant",
                    author_id=None,
                    author_name=None,
                    content=reply.content,
                    source_created_at=reply.source_created_at,
                )
                for reply in receipt.replies
            ],
            context_channel_id=receipt.context_channel_id,
        )
