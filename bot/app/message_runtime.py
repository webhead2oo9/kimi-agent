from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

import discord
from discord.ext import commands

from agent.attachments import (
    collect_reply_context,
    collect_turn_attachments,
    collect_turn_images,
    turn_has_image_input,
)
from agent.backfill import clean_message_text, message_source_timestamp
from agent.context import ContextManager
from agent.core import run_conversation
from agent.turn import TurnPreparationInput, TurnResult
from app.admission import (
    TURN_ADMISSION_BUSY_MESSAGE,
    AdmissionRejection,
    TurnAdmissionController,
)
from app.cancellation import ActiveOperationRegistry
from app.coding_delivery import CodingHandoffControl
from app.consent import PrivacyConsentGate
from app.conversation_routing import (
    ResolvedConversation,
    referenced_message_id,
    resolve_conversation_for_message,
    response_lock_key,
)
from app.foreground_turn import (
    ForegroundTurnInvocation,
    ForegroundTurnRunner,
    HandleTurn,
    TurnConversationSpec,
)
from app.guild_turn_adapter import (
    GuildMessageTurnAdapter,
    GuildTurnCollaborators,
    GuildTurnDeliveryConfig,
)
from app.learn_turn import run_learn_turn
from app.memory import MemoryManager
from app.providers import ProviderManager
from app.response_delivery import DiscordResponseSender
from app.root_locks import RootLockPool
from app.thread_handoff_boundary import ThreadHandoffBoundary
from app.threads import ThreadHandoffManager
from app.tools import RuntimeTools
from app.turn_entry import (
    TurnDependencyFactory,
    TurnEntryHooks,
    TurnEntryServices,
    chat_model_name_for_scope,
    resolve_parent_channel_id,
)
from config.fragments.channel_pins import (
    filter_pins_to_searchable,
    load_channel_blocked_tools,
    load_channel_pinned_tools,
)
from config.fragments.guild_config import (
    load_guild_blocked_tools,
    load_guild_pinned_tools,
)
from config.fragments.tool_config import load_tool_configs
from config.fragments.tool_policy import load_global_blocked_tools
from config.model_config import Scope
from config.settings import Settings
from discord_adapter.gateway import DiscordGateway
from discord_adapter.io import can_send_reply, is_eligible_to_respond, should_respond, strip_mention
from memory.banks import ensure_user_bank
from memory.recall import recall_current_user_context
from providers.assets import write_generated_assets
from skills.loader import SkillsIndexCache
from storage.blocked_users import BlockedUserStore
from storage.conversations import ConversationStore
from storage.image_distillations import ImageDistillationStore
from storage.preferences import PreferenceStore
from storage.usage import UsageStore
from tools.learn import LearnTarget
from tools.registry import USER_APP_SCOPE_CHANNEL_ID
from trust.resolver import TrustResolver
from trust.tiers import TrustTier
from utils.privacy_barrier import PrivacyDeletionPendingError, UserPrivacyBarrier
from workspace import user_app_workspace_key

if TYPE_CHECKING:
    from moderation.service import ModerationService

log = logging.getLogger(__name__)

_STATUS_REACTION_CLEANUP_TIMEOUT_SECONDS = 2.0


class BlockedUserCheck(Protocol):
    async def __call__(self, user_id: str, /) -> bool: ...


@dataclass(frozen=True, slots=True)
class RepositoryBlockedUserCheck:
    """Read the current blocked-user repository through one shared gate."""

    get_store: Callable[[], BlockedUserStore]

    async def __call__(self, user_id: str, /) -> bool:
        return await self.get_store().is_blocked(user_id)


class PersonalChatPort(Protocol):
    def classify_dm(self, message: discord.Message, /) -> TrustTier | None: ...

    async def resolve_dm_conversation(
        self,
        message: discord.Message,
        /,
    ) -> ResolvedConversation | None: ...


class WorkCancellationPort(Protocol):
    def is_stop_message(self, message: discord.Message, /) -> bool: ...

    async def handle_stop_message(self, message: discord.Message, /) -> None: ...


@dataclass(frozen=True, slots=True)
class MessageEntryConfig:
    allowed_channels: frozenset[int]
    bot_name: str
    new_user_onboarding_turns: int
    react_turn_timeout_seconds: float
    thread_handoff_suggest_after_tool_calls: int
    thread_auto_handoff_enabled: bool
    thread_handoff_enabled: bool


@dataclass(frozen=True, slots=True)
class MessageRuntimeBindings:
    """Post-initialization collaborators resolved only after the READY gate."""

    interactions_ready: Callable[[], bool]
    closed: Callable[[], bool]
    active_guilds: Callable[[], set[int]]
    active_operations: Callable[[], ActiveOperationRegistry]
    privacy_barrier: Callable[[], UserPrivacyBarrier]
    turn_admission: Callable[[], TurnAdmissionController]
    trust_resolver: Callable[[], TrustResolver]
    conversation_store: Callable[[], ConversationStore]
    preference_store: Callable[[], PreferenceStore]
    usage_store: Callable[[], UsageStore]
    image_distillation_store: Callable[[], ImageDistillationStore]
    context_manager: Callable[[], ContextManager]
    turn_runner: Callable[[], ForegroundTurnRunner]
    consent_gate: Callable[[], PrivacyConsentGate]
    personal_chat: Callable[[], PersonalChatPort]
    work_cancellation: Callable[[], WorkCancellationPort]
    thread_handoff: Callable[[], ThreadHandoffManager | None]
    coding: Callable[[], CodingHandoffControl]
    moderation_service: Callable[[], ModerationService | None]


class DiscordMessageController:
    """Own Discord message admission, routing, turn execution, and delivery wiring."""

    def __init__(
        self,
        *,
        config: MessageEntryConfig,
        turn_settings: Settings,
        bot: commands.Bot,
        gateway: DiscordGateway,
        responses: DiscordResponseSender,
        blocked_user: BlockedUserCheck,
        root_locks: RootLockPool,
        provider_manager: ProviderManager,
        memory_manager: MemoryManager,
        tools: RuntimeTools,
        llm_semaphore: asyncio.Semaphore,
        skills_index_cache: SkillsIndexCache,
        threads: ThreadHandoffBoundary,
        bindings: MessageRuntimeBindings,
    ) -> None:
        self._config = config
        self._turn_settings = turn_settings
        self._bot = bot
        self._gateway = gateway
        self._responses = responses
        self._blocked_user = blocked_user
        self._root_locks = root_locks
        self._provider_manager = provider_manager
        self._memory_manager = memory_manager
        self._tools = tools
        self._llm_semaphore = llm_semaphore
        self._skills_index_cache = skills_index_cache
        self._threads = threads
        self._bindings = bindings

    async def on_message(self, message: discord.Message) -> None:
        # This check must remain first: every binding below is lifecycle-owned
        # and is intentionally unavailable on a constructed-but-uninitialized app.
        if not self._bindings.interactions_ready():
            return
        active_guilds = self._bindings.active_guilds()
        if not is_eligible_to_respond(
            message,
            bot_user=self._bot.user,
            allowed_channels=set(self._config.allowed_channels) or None,
            allowed_guilds=active_guilds,
        ):
            return
        personal_chat = self._bindings.personal_chat()
        personal_dm = (
            isinstance(message.channel, discord.DMChannel)
            and personal_chat.classify_dm(message) is not None
        )
        if isinstance(message.channel, discord.DMChannel) and not personal_dm:
            # DMs are ignored unless this user has personal-chat access. Return
            # silently: replying would confirm the bot is listening and invite
            # probing from anyone who shares a guild with it.
            return

        # Pure routing check before taking a lease; messages the bot will ignore
        # have no state to coordinate with /privacy. A DM needs no invocation
        # gate: there is nothing else in the channel for it to be addressed to.
        if not personal_dm and not self._should_respond(message, active_guilds=active_guilds):
            return

        # Hard block gate precedes reactions, transcript writes, every lock or
        # privacy lease, tools, and provider calls.
        if await self._blocked_user(str(message.author.id)):
            log.info("Ignoring blocked user %s", message.author.id)
            return

        # Cancellation has its own lane before admission and the response lock;
        # otherwise a STOP message could queue behind the work it needs to end.
        work_cancellation = self._bindings.work_cancellation()
        if work_cancellation.is_stop_message(message) and (
            not personal_dm
            or self._bindings.active_operations().has_active_for_user(str(message.author.id))
        ):
            await work_cancellation.handle_stop_message(message)
            return

        # Reject before taking a privacy lease or doing any conversation,
        # attachment, moderation, memory, provider, or tool work. Admission is
        # deliberately non-waiting, so Discord event tasks cannot form a second
        # unbounded queue ahead of the provider semaphore.
        bot_member = message.guild.me if message.guild is not None else None
        if not can_send_reply(message.channel, bot_member=bot_member):
            log.info(
                "Skipping mention in channel %s: missing send permission",
                message.channel.id,
            )
            return
        admission = await self._bindings.turn_admission().try_acquire(str(message.author.id))
        if admission.lease is None:
            if admission.rejection is AdmissionRejection.SHUTTING_DOWN:
                return
            log.info(
                "Rejecting turn from user %s at admission boundary: %s",
                message.author.id,
                admission.rejection,
            )
            await self._responses.send(
                message.channel,
                TURN_ADMISSION_BUSY_MESSAGE,
                reference=message,
            )
            return

        # Hold this across every await below: consent/routing, model tools,
        # Discord delivery, and transcript persistence. Complete privacy deletion
        # drains already-started leases before wiping and blocks later ones until
        # the wipe finishes.
        try:
            with self._bindings.active_operations().register_provisional(
                user_id=str(message.author.id),
                channel_id=(USER_APP_SCOPE_CHANNEL_ID if personal_dm else str(message.channel.id)),
            ):
                async with admission.lease:
                    async with self._bindings.privacy_barrier().activity(str(message.author.id)):
                        await self._on_message_for_user(message)
        except PrivacyDeletionPendingError:
            log.info(
                "Ignoring user %s while their privacy deletion remains pending",
                message.author.id,
            )
        except asyncio.CancelledError:
            if self._bindings.closed():
                raise
            log.info("Stopped active response for user %s", message.author.id)

    async def _on_message_for_user(self, message: discord.Message) -> None:
        # First-interaction privacy gate. Sits before conversation resolution, the
        # lock, and the model turn, so an un-consented message never reaches the
        # provider or SQLite (resolve_conversation_for_message persists a
        # conversations row). On accept, the gate re-dispatches this message
        # through on_message.
        if await self._bindings.consent_gate().maybe_prompt(message):
            return

        personal_chat = self._bindings.personal_chat()
        resolved: ResolvedConversation | None
        if (
            isinstance(message.channel, discord.DMChannel)
            and personal_chat.classify_dm(message) is not None
        ):
            resolved = await personal_chat.resolve_dm_conversation(message)
        else:
            resolved = await self.resolve_conversation_for_message(
                message,
                allow_new_root=True,
            )
        if resolved is None:
            return
        self._bindings.active_operations().bind_current_provisional(resolved.key)

        lock_key = response_lock_key(message, resolved_conversation=resolved)
        # Ack before acquiring the lock so a continuation that queues behind an
        # in-flight turn on the same root (rapid replies / handoff-thread bursts)
        # shows ⏳ immediately instead of waiting silently for the lock.
        try:
            await self._gateway.add_status_reaction(message, "⏳")
            async with self._root_locks.hold(lock_key):
                # Re-check now that we hold the root lock. An earlier turn on this
                # root may have paused the thread while this message queued behind
                # it, and a paused thread must be neither answered nor transcribed
                # (docs/thread-handoff.md). The pre-lock check was made against the
                # old mode.
                # Re-read the cheap activation snapshot after waiting for the root
                # lock so an operator deactivation stops queued work immediately.
                # A DM has no invocation gate; its live equivalent is personal-chat
                # access, which an operator may have revoked while this queued.
                if isinstance(message.channel, discord.DMChannel):
                    if personal_chat.classify_dm(message) is None:
                        return
                elif not self._should_respond(message):
                    return
                try:
                    result = await self.handle_message(
                        message,
                        lock_acquired=True,
                        resolved_conversation=resolved,
                    )
                    if result is None:
                        # No turn ran: a bare @mention with no text/attachment to act
                        # on. Acknowledge the ping with a wave rather than leaving the
                        # user with no signal; not ✅, since no reply was sent.
                        await self._gateway.add_status_reaction(message, "👋")
                    elif result.blocked_by_moderation:
                        await self._gateway.add_status_reaction(message, "🚫")
                    elif result.termination_reason == "attachment_error" or result.delivery_failed:
                        await self._gateway.add_status_reaction(message, "❌")
                    else:
                        await self._gateway.add_status_reaction(message, "✅")
                except Exception:
                    log.exception("Error handling message %s", message.id)
                    await self._gateway.add_status_reaction(message, "❌")
        finally:
            await remove_processing_reaction(
                self._gateway,
                message,
                _STATUS_REACTION_CLEANUP_TIMEOUT_SECONDS,
            )

    async def handle_message(
        self,
        message: discord.Message,
        *,
        lock_acquired: bool = False,
        resolved_conversation: ResolvedConversation | None = None,
    ) -> TurnResult | None:
        assert self._bindings.context_manager() is not None
        personal_chat = self._bindings.personal_chat()
        # A DM from an allowlisted user is personal chat arriving as a real
        # message instead of a slash interaction. It scopes exactly like /chat:
        # one guild-less root, the shared "userapp" scope channel, the personal
        # workspace, personal prompt template, and no first-guild-turn onboarding.
        personal_dm_tier = (
            personal_chat.classify_dm(message)
            if isinstance(message.channel, discord.DMChannel)
            else None
        )
        personal_dm = personal_dm_tier is not None

        context_channel_id = USER_APP_SCOPE_CHANNEL_ID if personal_dm else str(message.channel.id)
        context_thread_id = (
            None
            if personal_dm
            else (str(message.channel.id) if isinstance(message.channel, discord.Thread) else None)
        )
        context_channel_name = (
            "Personal chat" if personal_dm else getattr(message.channel, "name", "DM")
        )
        if resolved_conversation is None:
            resolved_conversation = (
                await personal_chat.resolve_dm_conversation(message)
                if personal_dm
                else await self.resolve_conversation_for_message(
                    message,
                    allow_new_root=True,
                )
            )
        if resolved_conversation is None:
            return None
        conversation_key = resolved_conversation.key

        member = message.author if isinstance(message.author, discord.Member) else None
        user_id = str(message.author.id)
        user_name = message.author.display_name

        guild_id = str(message.guild.id) if message.guild else None
        guild_name = message.guild.name if message.guild else ""

        # Personal-chat standing comes from the USER_APP_* allowlists, never from
        # guild roles, and a DM has no guild to resolve against anyway.
        trust_tier = (
            personal_dm_tier
            if personal_dm_tier is not None
            else self._bindings.trust_resolver().resolve(member, user_id, guild_id)
        )

        conversation_store = self._bindings.conversation_store()
        assert conversation_store is not None

        async def count_user_prior_messages(
            user_id: str,
            exclude_discord_message_id: str | None,
            limit: int,
        ) -> int:
            return await conversation_store.count_user_messages(
                user_id,
                exclude_discord_message_id=exclude_discord_message_id,
                limit=limit,
            )

        turn_input = TurnPreparationInput(
            raw_content=clean_message_text(message.content),
            source_message=message,
            bot_user=self._bot.user,
            guild_id=guild_id,
            guild_name=guild_name,
            channel_id=context_channel_id,
            thread_id=context_thread_id,
            parent_channel_id=resolve_parent_channel_id(message.channel),
            channel_name=context_channel_name,
            user_id=user_id,
            user_name=user_name,
            trust_tier=trust_tier,
            conversation_key=conversation_key,
            trigger_discord_message_id=str(message.id),
            referenced_message_id=referenced_message_id(message),
            conversation_owner_user_id=resolved_conversation.owner_user_id,
            conversation_access_scope=resolved_conversation.access_scope,
            allow_bot_authored_reply_context=(
                resolved_conversation.allow_bot_authored_reply_context
            ),
            personal_chat=personal_dm,
            platform_channel_id=str(message.channel.id) if personal_dm else "",
            workspace_key=user_app_workspace_key(user_id) if personal_dm else None,
        )
        turn_stop_event = asyncio.Event()
        invocation = ForegroundTurnInvocation(
            conversation=TurnConversationSpec(
                key=conversation_key,
                channel_name=context_channel_name,
                guild_id=guild_id,
                channel_id=context_channel_id,
                thread_id=context_thread_id,
                root_discord_message_id=str(message.id),
                owner_user_id=resolved_conversation.owner_user_id,
                access_scope=resolved_conversation.access_scope,
                existing_conversation_id=resolved_conversation.db_conversation_id,
            ),
            source=turn_input,
            prepared_user_discord_message_id=str(message.id),
            prepared_user_source_created_at=message_source_timestamp(message),
            prepared_user_context_channel_id=context_channel_id,
            collect_reply_context=collect_reply_context,
            strip_mention=self.strip_message_invocation,
            stop_event=turn_stop_event,
            hooks=_turn_entry_hooks(),
            collect_turn_attachments=collect_turn_attachments,
            command_template="chat" if personal_dm else None,
            count_user_prior_messages=(count_user_prior_messages if not personal_dm else None),
            new_user_onboarding_turns=(
                0 if personal_dm else self._config.new_user_onboarding_turns
            ),
            timeout_seconds=self._config.react_turn_timeout_seconds,
            thread_handoff_suggest_after_tool_calls=(
                self._config.thread_handoff_suggest_after_tool_calls
            ),
            extra_blocked_tools=self._threads.thread_state_blocked_tools(message),
        )
        adapter = GuildMessageTurnAdapter(
            collaborators=self.guild_turn_collaborators(),
            message=message,
            context_channel_id=context_channel_id,
            personal_chat=personal_dm,
        )

        active_registration = self._bindings.active_operations().register(
            user_id=user_id,
            root_key=conversation_key,
            channel_id=context_channel_id,
            stop_event=turn_stop_event,
        )
        await active_registration.__aenter__()
        try:
            if lock_acquired:
                return await self._bindings.turn_runner().run(invocation, adapter=adapter)
            async with self._root_locks.hold(
                response_lock_key(
                    message,
                    resolved_conversation=resolved_conversation,
                )
            ):
                return await self._bindings.turn_runner().run(invocation, adapter=adapter)
        finally:
            await active_registration.__aexit__(None, None, None)

    def guild_turn_collaborators(self) -> GuildTurnCollaborators:
        """Compose the post-initialization capabilities used for guild delivery."""

        return GuildTurnCollaborators(
            config=GuildTurnDeliveryConfig(
                thread_auto_handoff_enabled=self._config.thread_auto_handoff_enabled,
                thread_handoff_enabled=self._config.thread_handoff_enabled,
                bot_name=self._config.bot_name,
            ),
            gateway=self._gateway,
            threads=self._threads,
            thread_handoff=self._bindings.thread_handoff(),
            coding=self._bindings.coding(),
            responses=self._responses,
            bot_user=lambda: self._bot.user,
            strip_invocation=self.strip_message_invocation,
        )

    async def resolve_conversation_for_message(
        self,
        message: discord.Message,
        *,
        allow_new_root: bool,
    ) -> ResolvedConversation | None:
        return await resolve_conversation_for_message(
            message,
            allow_new_root=allow_new_root,
            conversation_store=self._bindings.conversation_store(),
            thread_handoff=self._bindings.thread_handoff(),
        )

    def _should_respond(
        self,
        message: discord.Message,
        *,
        active_guilds: set[int] | None = None,
    ) -> bool:
        """The full response gate for a channel message (pure, no side effects)."""
        return should_respond(
            message,
            bot_user=self._bot.user,
            bot_name=self._config.bot_name,
            responds_without_mention=self.responds_without_mention,
            allowed_channels=set(self._config.allowed_channels) or None,
            allowed_guilds=(
                active_guilds if active_guilds is not None else self._bindings.active_guilds()
            ),
        )

    def responds_without_mention(self, thread_id: int) -> bool:
        """Whether a managed thread currently answers without being mentioned."""
        manager = self._bindings.thread_handoff()
        return manager is not None and manager.is_auto_responding(thread_id)

    def strip_message_invocation(
        self,
        content: str,
        *,
        bot_user: discord.ClientUser | None,
    ) -> str:
        return strip_mention(
            content,
            bot_user=bot_user,
            bot_name=self._config.bot_name,
        )

    def resolved_chat_model_name(self, scope: Scope, *, images: bool = False) -> str:
        return chat_model_name_for_scope(self._provider_manager, scope, images=images)

    def make_turn_dependency_factory(
        self,
        *,
        context_manager: ContextManager | None = None,
    ) -> TurnDependencyFactory:
        if context_manager is None:
            context_manager = self._bindings.context_manager()
        return TurnDependencyFactory(
            TurnEntryServices(
                settings=self._turn_settings,
                get_bot_user=lambda: self._bot.user,
                provider_manager=self._provider_manager,
                context_manager=context_manager,
                registry=self._tools.registry,
                preference_store=self._bindings.preference_store(),
                usage_store=self._bindings.usage_store(),
                attachment_store=self._tools.attachment_store,
                workspace_dir=self._tools.workspace_dir,
                workspace_manager=self._tools.workspace_manager,
                workspace_locks=self._tools.workspace_locks,
                llm_semaphore=self._llm_semaphore,
                get_memory_client=self._memory_manager.active_client,
                skills_index=self._skills_index_cache.index,
                personal_skills_index=self._tools.personal_skill_manager.index,
                resolve_reference_hints=self._gateway.resolve_reference_hints,
                moderation_service=self._bindings.moderation_service(),
                image_distillation_store=self._bindings.image_distillation_store(),
                user_activity=self._bindings.privacy_barrier().activity,
            )
        )

    def turn_entry_hooks(self) -> TurnEntryHooks:
        return _turn_entry_hooks()

    def make_foreground_turn_runner(
        self,
        *,
        handle_turn_hook: HandleTurn | None = None,
        conversation_store: ConversationStore | None = None,
        context_manager: ContextManager | None = None,
    ) -> ForegroundTurnRunner:
        if conversation_store is None:
            conversation_store = self._bindings.conversation_store()
        dependencies = self.make_turn_dependency_factory(context_manager=context_manager)
        if handle_turn_hook is None:
            return ForegroundTurnRunner(
                settings=self._turn_settings,
                conversation_store=conversation_store,
                dependency_factory=dependencies,
                active_operations=self._bindings.active_operations(),
                privacy_barrier=self._bindings.privacy_barrier(),
                workspace_locks=self._tools.workspace_locks,
            )
        return ForegroundTurnRunner(
            settings=self._turn_settings,
            conversation_store=conversation_store,
            dependency_factory=dependencies,
            active_operations=self._bindings.active_operations(),
            privacy_barrier=self._bindings.privacy_barrier(),
            workspace_locks=self._tools.workspace_locks,
            handle_turn_hook=handle_turn_hook,
        )

    def model_log_label(self, role: str) -> str:
        model_config = getattr(self._provider_manager, "model_config", None)
        if model_config is None:
            provider = getattr(self._provider_manager, "main", None)
            return getattr(provider, "model", "?")
        model_name = model_config.model_name_for_role(role)
        entry = model_config.models[model_name]
        profile = model_config.profile_for_model(model_name)
        return f"{model_name}={profile.type}/{entry.model}"

    async def run_learn_turn(
        self,
        target: LearnTarget,
        interaction: discord.Interaction,
    ) -> str:
        """Bind the bot-name-derived teaching context menu to a scoped agent turn."""
        guild_id = str(interaction.guild_id) if interaction.guild_id else ""
        channel = interaction.channel
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        return await run_learn_turn(
            provider_manager=self._provider_manager,
            registry=self._tools.registry,
            target=target,
            user_id=str(interaction.user.id),
            user_name=interaction.user.display_name,
            guild_id=guild_id,
            guild_name=interaction.guild.name if interaction.guild else "",
            channel_id=str(interaction.channel_id) if interaction.channel_id else "",
            channel_name=getattr(channel, "name", "") or "",
            skills_index=self._skills_index_cache.index(guild_id or None),
            bot_name=self._config.bot_name,
            platform_member=member,
            llm_semaphore=self._llm_semaphore,
        )


async def remove_processing_reaction(
    gateway: DiscordGateway,
    message: discord.Message,
    timeout: float,
) -> None:
    """Remove the working reaction without letting cancellation strand it."""

    removal = asyncio.create_task(gateway.remove_status_reaction(message, "⏳"))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    cancellation: asyncio.CancelledError | None = None

    while not removal.done():
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        try:
            await asyncio.wait((removal,), timeout=remaining)
        except asyncio.CancelledError as exc:
            # asyncio.wait does not cancel the task it is watching. Finish
            # the bounded cleanup before preserving the caller's cancellation.
            if cancellation is None:
                cancellation = exc

    if not removal.done():
        removal.cancel()

        def consume_result(completed: asyncio.Task[None]) -> None:
            with suppress(asyncio.CancelledError, Exception):
                completed.result()

        removal.add_done_callback(consume_result)
        log.warning("Timed out removing Discord processing reaction")
    else:
        try:
            removal.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            # Reaction cleanup is cosmetic and must not replace a provider,
            # routing, or cancellation outcome.
            log.debug("Could not remove Discord processing reaction", exc_info=True)

    if cancellation is not None:
        raise cancellation


def _turn_entry_hooks() -> TurnEntryHooks:
    return TurnEntryHooks(
        turn_has_image_input=turn_has_image_input,
        collect_turn_images=collect_turn_images,
        collect_reply_context=collect_reply_context,
        collect_turn_attachments=collect_turn_attachments,
        run_conversation=run_conversation,
        ensure_user_bank=ensure_user_bank,
        recall_current_user_context=recall_current_user_context,
        write_generated_assets=write_generated_assets,
        load_channel_pinned_tools=load_channel_pinned_tools,
        load_guild_pinned_tools=load_guild_pinned_tools,
        load_channel_blocked_tools=load_channel_blocked_tools,
        load_guild_blocked_tools=load_guild_blocked_tools,
        load_global_blocked_tools=load_global_blocked_tools,
        load_tool_configs=load_tool_configs,
        filter_pins_to_searchable=filter_pins_to_searchable,
    )
