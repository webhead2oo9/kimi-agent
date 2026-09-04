from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

import discord
from discord.ext import commands

from app.cancellation import ActiveOperationRegistry
from app.coding_delivery import CodingTaskController, MessageInvocationStripper
from app.consent import PrivacyConsentGate
from app.conversation_routing import ResolvedConversation
from discord_adapter.gateway import DiscordGateway
from discord_adapter.io import is_user_integration, is_user_only_integration
from tools.registry import USER_APP_SCOPE_CHANNEL_ID
from trust.resolver import TrustResolver
from trust.tiers import TrustTier

if TYPE_CHECKING:
    from storage.conversations import ConversationStore

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class WorkScope:
    channel_id: str
    root_key: str | None


@dataclass(frozen=True, slots=True)
class CancellationSummary:
    foreground_count: int = 0
    foreground_clean: bool = True
    coding_task_ids: tuple[str, ...] = ()
    coding_clean: bool = True

    @property
    def total(self) -> int:
        return self.foreground_count + len(self.coding_task_ids)

    @property
    def clean(self) -> bool:
        return self.foreground_clean and self.coding_clean

    def describe(self) -> str:
        if self.total == 0:
            return "I couldn't find active work to stop here."
        parts: list[str] = []
        if self.foreground_count:
            parts.append(f"{self.foreground_count} active response(s)")
        if self.coding_task_ids:
            labels = ", ".join(f"`{task_id[:8]}`" for task_id in self.coding_task_ids)
            parts.append(f"coding task(s) {labels}")
        cleanup = (
            "Cleanup is complete."
            if self.clean
            else "Cleanup is still finishing in the background."
        )
        return f"Stopped {' and '.join(parts)}. {cleanup} Partial file changes were kept."


class PersonalRequestInvalidator(Protocol):
    def invalidate_requests(self, user_id: str) -> None: ...

    def classify_dm(self, message: discord.Message) -> TrustTier | None: ...


class ConversationResolver(Protocol):
    async def __call__(
        self,
        message: discord.Message,
        /,
        *,
        allow_new_root: bool,
    ) -> ResolvedConversation | None: ...


class ResponseSender(Protocol):
    async def __call__(
        self,
        channel: discord.abc.Messageable,
        content: str,
        /,
        *,
        reference: discord.Message | None = None,
    ) -> object: ...


_STOP_WORDS = frozenset({"stop", "cancel", "abort"})


def is_stop_message(
    content: str,
    *,
    bot_user: discord.ClientUser | None,
    strip_message_invocation: MessageInvocationStripper,
) -> bool:
    """Exact bot-directed stop/cancel/abort, independent of any runtime state.

    on_message needs this before the coordinator exists, so it must not live
    on an object that is only constructed once the database is initialized.
    """

    text = strip_message_invocation(content, bot_user=bot_user)
    return text.strip().casefold() in _STOP_WORDS


class WorkCancellationCoordinator:
    def __init__(
        self,
        *,
        bot: commands.Bot,
        consent_gate: PrivacyConsentGate | None,
        personal_requests: PersonalRequestInvalidator,
        active_operations: ActiveOperationRegistry,
        coding_tasks: CodingTaskController,
        trust_resolver: TrustResolver,
        discord_gateway: DiscordGateway,
        conversation_resolver: ConversationResolver,
        response_sender: ResponseSender,
        strip_message_invocation: MessageInvocationStripper,
        cleanup_wait_seconds: float,
        conversation_store: ConversationStore | None = None,
    ) -> None:
        self._bot = bot
        self._consent_gate = consent_gate
        self._personal_requests = personal_requests
        self._active_operations = active_operations
        self._coding_tasks = coding_tasks
        self._trust_resolver = trust_resolver
        self._discord_gateway = discord_gateway
        self._conversation_resolver = conversation_resolver
        self._response_sender = response_sender
        self._strip_message_invocation = strip_message_invocation
        self._cleanup_wait_seconds = cleanup_wait_seconds
        self._conversation_store = conversation_store

    def is_stop_message(self, message: discord.Message) -> bool:
        return is_stop_message(
            message.content,
            bot_user=self._bot.user,
            strip_message_invocation=self._strip_message_invocation,
        )

    async def handle_stop_message(self, message: discord.Message) -> None:
        await self._discord_gateway.add_status_reaction(message, "🛑")
        user_id = str(message.author.id)
        root_key: str | None
        if self._personal_requests.classify_dm(message) is not None:
            channel_id = USER_APP_SCOPE_CHANNEL_ID
            root_key = f"userchat:{user_id}"
        else:
            channel_id = str(message.channel.id)
            resolved = await self._conversation_resolver(message, allow_new_root=False)
            root_key = resolved.key if resolved is not None else None
        summary = await self.cancel(
            user_id=user_id,
            scopes=(WorkScope(channel_id, root_key),),
            all_work=False,
        )
        await self._response_sender(message.channel, summary.describe(), reference=message)

    async def handle_stop_interaction(
        self,
        interaction: discord.Interaction,
        all_work: bool,
        task_id: str | None,
    ) -> str:
        user_id = str(interaction.user.id)
        channel_id = str(interaction.channel_id or "")
        if task_id is not None:
            if not self._coding_tasks.running:
                return "Coding tasks are not enabled."
            user_only = is_user_only_integration(interaction)
            # User-install invocations are logically guild-less even when Discord
            # supplies the physical guild where the command was opened.
            guild_id = (
                None if user_only else str(interaction.guild_id) if interaction.guild_id else None
            )
            member = interaction.user if isinstance(interaction.user, discord.Member) else None
            tier = self._trust_resolver.resolve(member, user_id, guild_id)
            task = await self._coding_tasks.resolve_task_for_control(
                user_id=user_id,
                guild_id=guild_id,
                trust_tier=tier,
                task_id=task_id,
            )
            if task is None:
                return "That coding task was not found."
            await self._coding_tasks.cancel_task(task.id, reason="Stopped with /stop")
            cleanup_complete = await self._coding_tasks.cleanup_complete(task.id)
            cleanup = (
                "Cleanup is complete."
                if cleanup_complete
                else "Cleanup is still finishing in the background."
            )
            return (
                f"Stopped coding task `{task.id[:8]}`. {cleanup} "
                "Partial workspace changes were kept."
            )
        user_only = is_user_only_integration(interaction)
        if all_work:
            # all_operations ignores the scope filters, so one entry sweeps everything.
            summary = await self.cancel(
                user_id=user_id,
                scopes=(WorkScope(USER_APP_SCOPE_CHANNEL_ID if user_only else channel_id, None),),
                all_work=True,
            )
            return summary.describe()
        # An app installed both to the user and to the guild reports both
        # integration owners, so the invoking context is ambiguous. Personal
        # chat is only reachable through the user install, and a guild channel
        # conversation only through the guild install; cancel every scope the
        # caller could have meant rather than silently missing one. Scoping
        # stays limited to this caller's own operations either way.
        scopes: list[WorkScope] = []
        if is_user_integration(interaction):
            scopes.append(WorkScope(USER_APP_SCOPE_CHANNEL_ID, f"userchat:{user_id}"))
        if channel_id and not user_only:
            scopes.append(WorkScope(channel_id, None))
        if not scopes:
            scopes.append(WorkScope(channel_id, None))
        summary = await self.cancel(
            user_id=user_id,
            scopes=scopes,
            all_work=False,
        )
        return summary.describe()

    async def cancel(
        self,
        *,
        user_id: str,
        scopes: Sequence[WorkScope],
        all_work: bool,
    ) -> CancellationSummary:
        foreground_count = 0
        foreground_clean = True
        coding_ids: list[str] = []
        coding_clean = True
        seen_tasks: set[str] = set()
        for scope in scopes:
            scope_count, scope_clean = await self._active_operations.cancel(
                user_id=user_id,
                root_key=scope.root_key,
                channel_id=scope.channel_id,
                all_operations=all_work,
                wait_seconds=self._cleanup_wait_seconds,
            )
            foreground_count += scope_count
            foreground_clean = foreground_clean and scope_clean
            if self._coding_tasks.running:
                scope_ids, scope_task_clean = await self._coding_tasks.cancel_for_scope(
                    user_id=user_id,
                    root_key=scope.root_key,
                    channel_id=scope.channel_id,
                    all_tasks=all_work,
                )
                # Scopes can overlap, so report each task once.
                for task_id in scope_ids:
                    if task_id not in seen_tasks:
                        seen_tasks.add(task_id)
                        coding_ids.append(task_id)
                coding_clean = coding_clean and scope_task_clean
        return CancellationSummary(
            foreground_count=foreground_count,
            foreground_clean=foreground_clean,
            coding_task_ids=tuple(coding_ids),
            coding_clean=coding_clean,
        )

    async def cancel_for_reset(
        self,
        *,
        user_id: str,
        scope: WorkScope,
    ) -> CancellationSummary:
        await self._invalidate_retained_requests(user_id)
        return await self.cancel(user_id=user_id, scopes=(scope,), all_work=False)

    async def cancel_for_privacy(self, user_id: str) -> None:
        await self._invalidate_retained_requests(user_id)
        summary = await self.cancel(
            user_id=user_id,
            scopes=(WorkScope(channel_id="", root_key=None),),
            all_work=True,
        )
        if not summary.clean:
            raise RuntimeError("The user's active work did not finish cleanup")
        # Deleting this user's rooted conversations would otherwise cascade
        # through the conversation FK and silently erase another user's live
        # coding-task row mid-worker. Drain those tasks first regardless of
        # task owner; the transcript delete that follows then only removes
        # already-settled rows. Drain failures propagate: proceeding to delete
        # with live cross-owner tasks would silently erase them, so the
        # caller must fail closed instead.
        if self._conversation_store is not None and self._coding_tasks.running:
            rooted_ids = await self._conversation_store.rooted_conversation_ids(user_id)
            if rooted_ids:
                _cancelled, clean = await self._coding_tasks.cancel_for_conversations(rooted_ids)
                if not clean:
                    raise RuntimeError("Shared-conversation coding work did not finish cleanup")

    async def _invalidate_retained_requests(self, user_id: str) -> None:
        # Pending consent callbacks are not active asyncio tasks. Invalidate
        # them before the privacy workflow drains active work and deletes data,
        # so an older prompt cannot recreate a personal transcript afterward.
        if self._consent_gate is not None:
            await self._consent_gate.invalidate_user(user_id)
        self._personal_requests.invalidate_requests(user_id)
