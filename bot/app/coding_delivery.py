from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Collection
from contextlib import AsyncExitStack, suppress
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import discord
from discord.ext import commands

from agent.auto_handoff import build_auto_handoff_request
from agent.backfill import message_source_timestamp, strip_chunk_marker
from agent.context import ConversationContext
from app.coding_jobs import CodingJobManager, stop_recovered_coding_units
from app.coding_tasks import CodingTaskRuntime, CodingTaskService
from app.root_locks import RootLockPool
from app.thread_handoff_boundary import THREAD_HANDOFF_REACTION, ThreadHandoffBoundary
from config.fragments.channel_pins import load_channel_auto_thread
from config.fragments.tool_config import load_tool_configs
from config.fragments.tool_policy import load_blocked_tools
from discord_adapter.gateway import DiscordGateway
from discord_adapter.io import (
    AttachmentDeliveryPlan,
    apply_attachment_delivery_notice,
    attachment_delivery_notice,
    chunk_message,
    suppress_link_previews,
)
from moderation.types import Direction
from storage.coding_tasks import CodingTask, CodingTaskStatus, CodingTaskStore
from storage.conversations import ChannelMessageRecord, ConversationStore
from storage.usage import UsageStore
from tools.coding_tasks import CODING_CONTROL_TOOLS, init_coding_control_tools
from tools.registry import ToolRegistry
from tools.workspace.common import UserLocks
from trust.tiers import TrustTier
from utils.privacy_barrier import UserPrivacyBarrier
from workspace import WorkspaceKey

if TYPE_CHECKING:
    from app.providers import ProviderManager
    from app.tools import RuntimeTools
    from config.settings import Settings
    from moderation.service import ModerationService

log = logging.getLogger(__name__)


class MessageInvocationStripper(Protocol):
    def __call__(
        self,
        content: str,
        /,
        *,
        bot_user: discord.ClientUser | None,
    ) -> str: ...


class CodingHandoffControl(Protocol):
    async def prepare_handoff(
        self,
        task_id: str,
        /,
        *,
        channel_id: str | None = None,
        thread_id: str | None = None,
    ) -> bool: ...

    async def release_handoff(self, task_id: str, /) -> bool: ...

    async def finalize_handoff(
        self,
        task_id: str,
        /,
        *,
        channel_id: str | None = None,
        thread_id: str | None = None,
    ) -> bool: ...

    async def delete_status_message(
        self,
        channel: discord.TextChannel | discord.Thread,
        task: CodingTask,
        marker: str,
        /,
        *,
        message: discord.Message | None = None,
    ) -> None: ...

    def task_marker(self, task_id: str, /) -> str: ...

    async def failed_handoff_task(self, task_id: str, /) -> CodingTask | None: ...


@dataclass(frozen=True, slots=True)
class CodingDeliveryConfig:
    thread_handoff_enabled: bool
    thread_auto_handoff_enabled: bool
    bot_name: str


@dataclass(frozen=True, slots=True)
class ModeratedCodingText:
    text: str
    blocked: bool = False


class CodingDelivery:
    def __init__(
        self,
        *,
        bot: commands.Bot,
        store: CodingTaskStore,
        conversation_store: ConversationStore,
        discord_gateway: DiscordGateway,
        workspace_locks: UserLocks,
        root_locks: RootLockPool,
        threads: ThreadHandoffBoundary,
        moderation_service: ModerationService | None,
        config: CodingDeliveryConfig,
        strip_message_invocation: MessageInvocationStripper,
    ) -> None:
        self._bot = bot
        self._store = store
        self._conversation_store = conversation_store
        self._discord_gateway = discord_gateway
        self._workspace_locks = workspace_locks
        self._root_locks = root_locks
        self._threads = threads
        self._moderation_service = moderation_service
        self._config = config
        self._strip_message_invocation = strip_message_invocation

    async def publish(self, task: CodingTask, context: ConversationContext | None) -> None:
        """Project durable task state onto one edited status and one final reply."""

        # A worker completion, debounced milestone, and delivery retry can become
        # ready together. Serialize them per task and refresh the durable row so
        # a stale retry cannot send a second final response.
        async with self._root_locks.hold(f"coding-delivery:{task.id}"):
            refreshed = await self._store.get_task(task.id)
            if refreshed is None:
                return
            await self._publish_locked(refreshed, context)

    async def _publish_locked(
        self,
        task: CodingTask,
        context: ConversationContext | None,
    ) -> None:
        # Fast path: skip obviously-gone rows before any Discord IO. The
        # authoritative re-check runs under the root lock before the final
        # send below; between the two only cosmetic status IO can occur, and
        # its durable writes are no-ops for a deleted row.
        if await self._store.get_task(task.id) is None:
            return
        target_id = task.thread_id or task.channel_id
        try:
            channel = self._bot.get_channel(int(target_id))
            if channel is None:
                channel = await self._bot.fetch_channel(int(target_id))
        except ValueError:
            await self._mark_permanent_failure(task, "Invalid Discord channel id")
            log.warning("Invalid Discord channel for coding task %s", task.id)
            return
        except (discord.NotFound, discord.Forbidden) as exc:
            await self._mark_permanent_failure(
                task,
                f"Discord channel is unavailable ({type(exc).__name__})",
            )
            log.warning("Discord channel is unavailable for coding task %s", task.id)
            return
        except discord.HTTPException:
            log.warning("Could not resolve Discord channel for coding task %s", task.id)
            return
        if not isinstance(channel, discord.TextChannel | discord.Thread):
            await self._mark_permanent_failure(
                task,
                "Discord delivery target is not a text channel or thread",
            )
            return
        status_marker = self.task_marker(task.id)
        terminal = task.status in {
            CodingTaskStatus.COMPLETED,
            CodingTaskStatus.FAILED,
            CodingTaskStatus.CANCELLED,
            CodingTaskStatus.TIMED_OUT,
        }
        if terminal and task.final_discord_message_id is not None:
            await self.delete_status_message(channel, task, status_marker)
            return
        status_text = self.status_text(task)
        status_text = (await self.moderate_text(task, status_text, status=True)).text
        status_text = self.status_wire_text(status_text)[:2000]
        status_message: discord.Message | None = None
        if task.status_discord_message_id:
            try:
                status_message = await channel.fetch_message(int(task.status_discord_message_id))
                await status_message.edit(content=status_text)
            except ValueError, discord.HTTPException:
                status_message = None
        if status_message is None:
            status_message = await self._find_delivery(channel, status_marker)
            if status_message is not None:
                with suppress(discord.HTTPException):
                    await status_message.edit(content=status_text)
                if task.conversation_id is not None:
                    await self._conversation_store.map_message_context(
                        str(status_message.id), task.conversation_id, str(channel.id)
                    )
                await self._store.mark_status_message(task.id, str(status_message.id))
        if status_message is None:
            try:
                status_message = await channel.send(
                    status_text,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except (discord.NotFound, discord.Forbidden) as exc:
                await self._mark_permanent_failure(
                    task,
                    f"Cannot send to Discord channel ({type(exc).__name__})",
                )
                log.warning("Cannot send coding status for task %s", task.id, exc_info=True)
                return
            except discord.HTTPException:
                log.warning("Could not send coding status for task %s", task.id, exc_info=True)
                return
            if task.conversation_id is not None:
                await self._conversation_store.map_message_context(
                    str(status_message.id), task.conversation_id, str(channel.id)
                )
            await self._store.mark_status_message(task.id, str(status_message.id))

        if terminal:
            final_text = task.result_text.strip() or task.error_text.strip()
            if not final_text:
                final_text = f"Coding task `{task.id[:8]}` ended as **{task.status.value}**."
            moderated_final = await self.moderate_text(task, final_text, status=False)
            final_text = self.result_delivery_text(task.id, moderated_final.text)
            delivery_channel = await self.result_channel(task, channel, final_text)
            delivery = task.checkpoint.get("delivery")
            durable_output_files = (
                delivery.get("output_files", []) if isinstance(delivery, dict) else []
            )
            durable_allowed_roots = (
                delivery.get("allowed_file_roots", []) if isinstance(delivery, dict) else []
            )
            output_files = (
                list(context.pending_outbox.output_files)
                if context is not None
                else [str(value) for value in durable_output_files]
            )
            allowed_roots: list[str | Path] = (
                list(context.pending_outbox.allowed_file_roots)
                if context is not None
                else [str(value) for value in durable_allowed_roots]
            )
            if moderated_final.blocked:
                output_files = []
                allowed_roots = []
            async with self._root_locks.hold(task.root_key):
                # The row was refreshed before this lock was acquired; a privacy
                # or retention delete can land in between and cascade the task
                # row away. Re-check under the same lock the deleter holds so a
                # final reply is never sent for a task whose state is gone.
                current = await self._store.get_task(task.id)
                if current is None:
                    return
                task = current
                async with AsyncExitStack() as delivery_stack:
                    if output_files:
                        await delivery_stack.enter_async_context(
                            self._workspace_locks.activity(WorkspaceKey(task.workspace_key))
                        )
                    attachment_plan = await self.prepare_attachment_delivery(
                        task,
                        delivery_channel,
                        output_files=output_files,
                        allowed_roots=allowed_roots,
                    )
                    prepared_text = apply_attachment_delivery_notice(
                        final_text,
                        attachment_plan,
                        after_first_line=True,
                    )
                    recovered_final = await self.find_result_delivery(
                        delivery_channel,
                        prepared_text,
                    )
                    if recovered_final:
                        await self._commit_final_delivery(
                            task,
                            recovered_final,
                            delivery_channel_id=str(delivery_channel.id),
                            status_channel=channel,
                            status_marker=status_marker,
                            status_message=status_message,
                        )
                        return
                    sent = await self._discord_gateway.send_prepared_response(
                        delivery_channel,
                        prepared_text,
                        attachment_plan,
                    )
                    if not sent or sent.delivery_failed:
                        if sent:
                            await self._delete_messages(list(sent))
                        if sent.delivery_permanent:
                            error = sent.delivery_error or "Discord permission failure"
                            await self._mark_permanent_failure(task, error)
                        return
                    await self._commit_final_delivery(
                        task,
                        list(sent),
                        delivery_channel_id=str(delivery_channel.id),
                        status_channel=channel,
                        status_marker=status_marker,
                        status_message=status_message,
                    )

    async def _commit_final_delivery(
        self,
        task: CodingTask,
        messages: list[discord.Message],
        *,
        delivery_channel_id: str,
        status_channel: discord.TextChannel | discord.Thread,
        status_marker: str,
        status_message: discord.Message | None,
    ) -> None:
        """Persist a final result before disabling retry and removing its status."""

        await self.persist_final_messages(
            task,
            messages,
            channel_id=delivery_channel_id,
        )
        await self._store.mark_delivered(task.id, str(messages[0].id))
        await self.delete_status_message(
            status_channel,
            task,
            status_marker,
            message=status_message,
        )

    async def prepare_attachment_delivery(
        self,
        task: CodingTask,
        channel: discord.TextChannel | discord.Thread,
        *,
        output_files: list[str],
        allowed_roots: list[str | Path],
    ) -> AttachmentDeliveryPlan:
        delivery = task.checkpoint.get("delivery")
        persisted = delivery.get("attachment_plan") if isinstance(delivery, dict) else None
        limit, notice = self._attachment_plan_overrides(persisted)
        plan = self._discord_gateway.prepare_attachment_delivery(
            channel,
            output_files=output_files,
            allowed_file_roots=allowed_roots,
            embed=None,
            effective_limit_bytes=limit,
            notice_text=notice,
        )
        if not output_files or persisted is not None:
            return plan

        frozen = self._serialize_attachment_plan(plan)
        stored = await self._store.set_delivery_attachment_plan_if_absent(
            task.id,
            frozen,
        )
        if stored is None or stored == frozen:
            return plan
        stored_limit, stored_notice = self._attachment_plan_overrides(stored)
        return self._discord_gateway.prepare_attachment_delivery(
            channel,
            output_files=output_files,
            allowed_file_roots=allowed_roots,
            embed=None,
            effective_limit_bytes=stored_limit,
            notice_text=stored_notice,
        )

    @staticmethod
    def _serialize_attachment_plan(plan: AttachmentDeliveryPlan) -> dict[str, Any]:
        return {
            "effective_limit_bytes": plan.effective_limit_bytes,
            "notice_text": attachment_delivery_notice(plan),
            "omitted": [
                {
                    "path": omitted.path,
                    "filename": omitted.filename,
                    "size_bytes": omitted.size_bytes,
                    "reason": omitted.reason,
                }
                for omitted in plan.omitted
            ],
        }

    @staticmethod
    def _attachment_plan_overrides(raw: object) -> tuple[int | None, str | None]:
        if not isinstance(raw, dict):
            return None, None
        limit = raw.get("effective_limit_bytes")
        notice = raw.get("notice_text")
        valid_limit = (
            limit if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0 else None
        )
        return valid_limit, notice if isinstance(notice, str) else None

    async def _mark_permanent_failure(self, task: CodingTask, reason: str) -> None:
        await self._store.record_delivery_failure(
            task.id,
            reason,
            permanent=True,
        )

    async def result_channel(
        self,
        task: CodingTask,
        fallback: discord.TextChannel | discord.Thread,
        final_text: str,
    ) -> discord.TextChannel | discord.Thread:
        """Keep an existing thread or apply the ordinary auto-handoff policy."""

        if task.thread_id is not None:
            return fallback

        delivery = task.checkpoint.get("delivery")
        saved_thread_id = delivery.get("thread_id") if isinstance(delivery, dict) else None
        if isinstance(saved_thread_id, str):
            try:
                saved_channel = self._bot.get_channel(int(saved_thread_id))
                if saved_channel is None:
                    saved_channel = await self._bot.fetch_channel(int(saved_thread_id))
            except ValueError, discord.HTTPException:
                saved_channel = None
            if isinstance(saved_channel, discord.Thread):
                return saved_channel

        if (
            not isinstance(fallback, discord.TextChannel)
            or not self._config.thread_handoff_enabled
            or self._threads.thread_handoff is None
        ):
            return fallback

        try:
            trigger = await fallback.fetch_message(int(task.trigger_discord_message_id))
        except ValueError, discord.HTTPException:
            return fallback
        existing_thread = await self._threads.adopt_managed_handoff_thread(trigger)
        if existing_thread is not None:
            await self._save_delivery_thread(task, existing_thread.id)
            return existing_thread
        if not self._config.thread_auto_handoff_enabled:
            return fallback
        auto_cfg = (
            load_channel_auto_thread(task.channel_id)
            if self._threads.thread_handoff_creation_allowed(trigger)
            else None
        )
        if auto_cfg is None:
            return fallback
        request = build_auto_handoff_request(
            response_text=final_text,
            question_text=self._strip_message_invocation(
                trigger.content,
                bot_user=self._bot.user,
            ),
            bot_name=self._config.bot_name,
            min_lines=auto_cfg.min_lines,
            min_chars=auto_cfg.min_chars,
            always=auto_cfg.always,
        )
        if request is None:
            return fallback
        thread = await self._threads.create_handoff_thread(
            trigger,
            request,
            task.conversation_id,
            creator_user_id=task.user_id,
        )
        if thread is None:
            return fallback
        await self._save_delivery_thread(task, thread.id)
        await self._discord_gateway.add_status_reaction(trigger, THREAD_HANDOFF_REACTION)
        return thread

    async def _save_delivery_thread(self, task: CodingTask, thread_id: int) -> None:
        current = await self._store.get_task(task.id)
        checkpoint = dict(current.checkpoint if current is not None else task.checkpoint)
        persisted_delivery = checkpoint.get("delivery")
        persisted_delivery = (
            dict(persisted_delivery) if isinstance(persisted_delivery, dict) else {}
        )
        persisted_delivery["thread_id"] = str(thread_id)
        checkpoint["delivery"] = persisted_delivery
        await self._store.set_checkpoint(task.id, checkpoint)

    async def find_result_delivery(
        self,
        channel: discord.TextChannel | discord.Thread,
        expected_text: str,
    ) -> list[discord.Message]:
        """Recover only a complete multi-message result after a process crash."""

        bot_user = self._bot.user
        expected_chunks = chunk_message(suppress_link_previews(expected_text))
        if bot_user is None or not expected_chunks:
            return []
        try:
            newest_first = [
                message
                async for message in channel.history(limit=max(100, len(expected_chunks) * 2))
            ]
        except discord.HTTPException:
            log.warning("Could not reconcile coding result", exc_info=True)
            return []

        bot_messages = [
            message for message in reversed(newest_first) if message.author.id == bot_user.id
        ]
        for start, message in enumerate(bot_messages):
            if message.content != expected_chunks[0]:
                continue
            matched = [message]
            expected_index = 1
            for candidate in bot_messages[start + 1 :]:
                if expected_index >= len(expected_chunks):
                    break
                if candidate.content == expected_chunks[expected_index]:
                    matched.append(candidate)
                    expected_index += 1
            if expected_index == len(expected_chunks):
                return matched

        return []

    async def delete_status_message(
        self,
        channel: discord.TextChannel | discord.Thread,
        task: CodingTask,
        marker: str,
        *,
        message: discord.Message | None = None,
    ) -> None:
        if message is None and task.status_discord_message_id:
            try:
                message = await channel.fetch_message(int(task.status_discord_message_id))
            except ValueError, discord.HTTPException:
                message = None
        if message is None:
            message = await self._find_delivery(channel, marker)
        if message is None:
            return
        try:
            await message.delete()
        except discord.HTTPException:
            log.warning("Could not delete coding status for task %s", task.id, exc_info=True)

    @staticmethod
    async def _delete_messages(messages: list[discord.Message]) -> None:
        for message in reversed(messages):
            try:
                await message.delete()
            except discord.HTTPException:
                log.warning("Could not clean up partial coding result", exc_info=True)

    async def persist_final_messages(
        self,
        task: CodingTask,
        messages: list[discord.Message],
        *,
        channel_id: str,
    ) -> None:
        """Persist the final transcript before committing its durable delivery state."""

        if task.conversation_id is None:
            return
        records = [
            ChannelMessageRecord(
                discord_message_id=str(message.id),
                role="assistant",
                author_id=None,
                author_name=None,
                content=self.strip_delivery_marker(
                    strip_chunk_marker(message.content),
                    task_ref=task.id[:8],
                ),
                source_created_at=message_source_timestamp(message),
            )
            for message in messages
        ]
        await self._conversation_store.save_channel_messages(
            task.conversation_id,
            records,
            context_channel_id=channel_id,
        )

    async def _find_delivery(
        self,
        channel: discord.TextChannel | discord.Thread,
        marker: str,
    ) -> discord.Message | None:
        """Reconcile a send that may have committed just before a process crash."""

        bot_user = self._bot.user
        if bot_user is None:
            return None
        try:
            async for message in channel.history(limit=100):
                if message.author.id == bot_user.id and marker in message.content:
                    return message
        except discord.HTTPException:
            log.warning("Could not reconcile coding delivery marker %s", marker, exc_info=True)
        return None

    @staticmethod
    def strip_delivery_marker(text: str, *, task_ref: str) -> str:
        visible_result_marker = f"**Coding result `{task_ref}`**"
        lines = [line for line in text.splitlines() if line != visible_result_marker]
        return "\n".join(lines)

    @staticmethod
    def task_marker(task_id: str) -> str:
        return f"Coding task `{task_id[:8]}`"

    @staticmethod
    def _result_marker(task_id: str) -> str:
        return f"Coding result `{task_id[:8]}`"

    @staticmethod
    def result_delivery_text(task_id: str, text: str) -> str:
        return f"**{CodingDelivery._result_marker(task_id)}**\n{text}"

    async def moderate_text(
        self,
        task: CodingTask,
        text: str,
        *,
        status: bool,
    ) -> ModeratedCodingText:
        service = self._moderation_service
        if not self.should_moderate_output(task) or service is None:
            return ModeratedCodingText(text)
        trust_tier = self._task_trust_tier(task)
        try:
            decision = await service.check(
                text=text,
                direction=Direction.OUTPUT,
                user_id=task.user_id,
                channel_id=task.channel_id,
                thread_id=task.thread_id,
                trust_tier=trust_tier.value,
            )
        except Exception:
            log.warning("Coding task output moderation failed", exc_info=True)
            return ModeratedCodingText(
                (
                    f"**Coding task `{task.id[:8]}`: {task.status.value}**"
                    if status
                    else service.refusal_for(Direction.OUTPUT, error=True)
                ),
                blocked=True,
            )
        if not decision.blocked:
            return ModeratedCodingText(text)
        if status:
            return ModeratedCodingText(
                f"**Coding task `{task.id[:8]}`: {task.status.value}**",
                blocked=True,
            )
        return ModeratedCodingText(
            service.refusal_for(Direction.OUTPUT, error=decision.error),
            blocked=True,
        )

    def should_moderate_output(self, task: CodingTask) -> bool:
        service = self._moderation_service
        if service is None or not service.enabled:
            return False
        exempt_tier = service.output_exempt_tier
        return exempt_tier is None or self._task_trust_tier(task) < exempt_tier

    @staticmethod
    def _task_trust_tier(task: CodingTask) -> TrustTier:
        raw = task.checkpoint.get("trust_tier")
        if not isinstance(raw, str):
            raise RuntimeError("Coding task checkpoint has no valid trust_tier")
        try:
            return TrustTier(raw)
        except ValueError as exc:
            raise RuntimeError("Coding task checkpoint has no valid trust_tier") from exc

    @staticmethod
    def status_wire_text(status_text: str) -> str:
        return suppress_link_previews(status_text)

    @staticmethod
    def status_text(task: CodingTask) -> str:
        icons = {
            CodingTaskStatus.QUEUED: "⏳",
            CodingTaskStatus.RECOVERING: "🔄",
            CodingTaskStatus.RUNNING: "🛠️",
            CodingTaskStatus.WAITING_FOR_JOB: "⚙️",
            CodingTaskStatus.WAITING_FOR_INPUT: "❓",
            CodingTaskStatus.CANCELLING: "🛑",
            CodingTaskStatus.COMPLETED: "✅",
            CodingTaskStatus.FAILED: "❌",
            CodingTaskStatus.CANCELLED: "🛑",
            CodingTaskStatus.TIMED_OUT: "⌛",
        }
        task_marker = CodingDelivery.task_marker(task.id)
        lines = [f"{icons[task.status]} **{task_marker}: {task.status.value}**"]
        if not task.plan:
            lines.append(
                CodingTaskService._display_summary(
                    task.objective,
                    task.display_summary,
                )
            )
        if task.milestone:
            lines.append(f"-# {task.milestone[:500]}")
        visible_plan = [step for step in task.plan if step.get("status") != "completed"][:3]
        for step in visible_plan:
            marker = "▶" if step.get("status") == "in_progress" else "•"
            lines.append(f"{marker} {step.get('content', '')[:180]}")
        return "\n".join(lines)[:2000]


class CodingTaskControllerState(StrEnum):
    DISABLED = "disabled"
    NOT_STARTED = "not_started"
    RUNNING = "running"


class CodingTaskController:
    def __init__(
        self,
        *,
        settings: Settings,
        store: CodingTaskStore,
        usage_store: UsageStore,
        provider_manager: ProviderManager,
        source_registry: ToolRegistry,
        tools: RuntimeTools,
        llm_semaphore: asyncio.Semaphore,
        privacy_barrier: UserPrivacyBarrier,
        user_blocked: Callable[[str], Awaitable[bool]],
        delivery: CodingDelivery,
    ) -> None:
        self._settings = settings
        self._store = store
        self._usage_store = usage_store
        self._provider_manager = provider_manager
        self._source_registry = source_registry
        self._tools = tools
        self._llm_semaphore = llm_semaphore
        self._privacy_barrier = privacy_barrier
        self._user_blocked = user_blocked
        self._delivery = delivery
        self._service: CodingTaskService | None = None
        self._state = CodingTaskControllerState.NOT_STARTED
        self._recovery_lock = asyncio.Lock()
        self._recovery_complete = False

    @property
    def state(self) -> CodingTaskControllerState:
        return self._state

    @property
    def running(self) -> bool:
        return self._state is CodingTaskControllerState.RUNNING

    @property
    def store(self) -> CodingTaskStore:
        return self._store

    async def recover_persisted_work(self) -> None:
        """Stop pre-crash units and normalize their rows before any deletion."""

        if self._recovery_complete:
            return
        if self.running:
            raise RuntimeError("Cannot run startup coding recovery while workers are active")
        async with self._recovery_lock:
            if self._recovery_complete:
                return
            await stop_recovered_coding_units(self._store)
            await self._store.recover_interrupted()
            self._recovery_complete = True

    async def start(self) -> None:
        if self._state is CodingTaskControllerState.RUNNING:
            return
        await self.recover_persisted_work()
        if self._state is CodingTaskControllerState.DISABLED:
            return
        if not self._settings.coding_tasks_enabled:
            self._state = CodingTaskControllerState.DISABLED
            log.info("Coding tasks disabled; CODING_TASKS_ENABLED is false")
            return

        model_config = self._provider_manager.model_config
        # A missing role or sandbox leaves the coding surface unregistered instead
        # of taking the whole bot down: a sandbox probe that stops passing after a
        # host upgrade must not turn into a Discord outage (docs/coding-agent.md).
        if model_config.roles.coding is None:
            self._state = CodingTaskControllerState.DISABLED
            log.warning("Coding tasks requested but config/models.yaml assigns no coding role")
            return
        coding_model = model_config.roles.coding
        sandbox_config = self._tools.code_sandbox_config
        if sandbox_config is None:
            self._state = CodingTaskControllerState.DISABLED
            log.warning("Coding tasks requested but the code sandbox is unavailable")
            return
        code_exec_guards = self._tools.code_exec_guards
        if code_exec_guards is None:
            self._state = CodingTaskControllerState.DISABLED
            log.warning("Coding tasks requested but code execution guards are unavailable")
            return

        jobs = CodingJobManager(
            store=self._store,
            workspace_manager=self._tools.workspace_manager,
            workspace_locks=self._tools.workspace_locks,
            sandbox_config=sandbox_config,
            max_seconds=self._settings.coding_job_max_seconds,
            max_cpu_seconds=self._settings.coding_job_max_cpu_seconds,
            runtime_guards=code_exec_guards,
            usage_store=self._usage_store,
            user_activity=self._privacy_barrier.activity,
        )
        service = CodingTaskService(
            CodingTaskRuntime(
                settings=self._settings,
                store=self._store,
                usage_store=self._usage_store,
                provider_manager=self._provider_manager,
                source_registry=self._source_registry,
                jobs=jobs,
                llm_semaphore=self._llm_semaphore,
                compactor=self._provider_manager.build_compactor(self._llm_semaphore),
                model_config=model_config,
                notifier=self._delivery.publish,
                user_activity=self._privacy_barrier.activity,
                user_blocked=self._user_blocked,
                workspace_manager=self._tools.workspace_manager,
                workspace_locks=self._tools.workspace_locks,
                workspace_config=self._tools.workspace_config,
                blocked_tools=load_blocked_tools,
                tool_configs=load_tool_configs,
            )
        )
        self._service = service
        # Built-in lifecycle controls are authoritative if a plugin happened to
        # claim one of their names before the database-backed service was ready.
        self._source_registry.remove_tools(set(CODING_CONTROL_TOOLS))
        init_coding_control_tools(self._source_registry, service)
        await service.start()
        self._state = CodingTaskControllerState.RUNNING
        log.info("Durable coding tasks enabled with model %s", coding_model)

    async def close(self) -> None:
        service = self._service
        if service is None:
            return
        await service.close()
        self._service = None
        self._state = CodingTaskControllerState.NOT_STARTED
        self._recovery_complete = False

    def _running_service(self) -> CodingTaskService:
        service = self._service
        if service is None or not self.running:
            raise RuntimeError("Coding task controller is not running")
        return service

    async def prepare_handoff(
        self,
        task_id: str,
        *,
        channel_id: str | None = None,
        thread_id: str | None = None,
    ) -> bool:
        return await self._running_service().prepare_handoff(
            task_id,
            channel_id=channel_id,
            thread_id=thread_id,
        )

    async def release_handoff(self, task_id: str) -> bool:
        return await self._running_service().release_handoff(task_id)

    async def finalize_handoff(
        self,
        task_id: str,
        *,
        channel_id: str | None = None,
        thread_id: str | None = None,
    ) -> bool:
        return await self._running_service().finalize_handoff(
            task_id,
            channel_id=channel_id,
            thread_id=thread_id,
        )

    async def delete_status_message(
        self,
        channel: discord.TextChannel | discord.Thread,
        task: CodingTask,
        marker: str,
        *,
        message: discord.Message | None = None,
    ) -> None:
        await self._delivery.delete_status_message(
            channel,
            task,
            marker,
            message=message,
        )

    def task_marker(self, task_id: str) -> str:
        return self._delivery.task_marker(task_id)

    async def failed_handoff_task(self, task_id: str, /) -> CodingTask | None:
        return await self._store.get_task(task_id)

    async def resolve_task_for_control(
        self,
        *,
        user_id: str,
        guild_id: str | None,
        trust_tier: TrustTier,
        task_id: str,
    ) -> CodingTask | None:
        return await self._running_service().resolve_task_for_control(
            user_id=user_id,
            guild_id=guild_id,
            trust_tier=trust_tier,
            task_id=task_id,
        )

    async def cancel_task(self, task_id: str, *, reason: str = "") -> bool:
        return await self._running_service().cancel_task(task_id, reason=reason)

    async def cancel_for_scope(
        self,
        *,
        user_id: str,
        root_key: str | None,
        channel_id: str | None = None,
        all_tasks: bool = False,
    ) -> tuple[list[str], bool]:
        return await self._running_service().cancel_for_scope(
            user_id=user_id,
            root_key=root_key,
            channel_id=channel_id,
            all_tasks=all_tasks,
        )

    async def cleanup_complete(self, task_id: str) -> bool:
        return await self._running_service().cleanup_complete(task_id)

    async def cancel_for_conversations(
        self,
        conversation_ids: Collection[int],
    ) -> tuple[list[str], bool]:
        return await self._running_service().cancel_for_conversations(conversation_ids)
