from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

import discord
from discord import app_commands
from discord.ext import commands

from config.fragments.guild_config import (
    load_guild_trust,
    server_setup_activation,
)
from agent.context import ContextManager
from config.fragments.tool_policy import load_global_blocked_tools
from agent.turn import (
    TurnResult,
)
from app.admission import (
    TurnAdmissionController,
)
from app.cancellation import ActiveOperationRegistry
from app.command_sync import CommandSyncConfig, DiscordCommandSync
from app.conversation_routing import (
    ResolvedConversation,
)
from app.consent import PrivacyConsentGate
from app.coding_delivery import (
    CodingDelivery,
    CodingDeliveryConfig,
    CodingTaskController,
)
from app.guild_activation import GuildActivationConfig, GuildActivationService
from app.lifecycle import (
    AppRepositories,
    ApplicationLifecycle,
    LifecycleCallbacks,
    LifecycleResources,
    ShutdownSignal,
)
from app.message_runtime import (
    DiscordMessageController,
    MessageEntryConfig,
    MessageRuntimeBindings,
    RepositoryBlockedUserCheck,
)
from app.response_delivery import DiscordResponseSender
from app.user_app_chat import UserAppChatConfig, UserAppChatController
from app.user_app_consent import UserAppConsentConfig, UserAppConsentPrompter
from app.work_cancellation import WorkCancellationCoordinator, WorkScope
from app.root_locks import RootLockPool
from app.thread_handoff_boundary import ThreadHandoffBoundary
from app.threads import ThreadHandoffManager
from app.memory import MemoryManager
from app.moderation import build_moderation_service
from app.providers import (
    ContextWindowWarning,
    ProviderManager,
    build_provider_manager,
    codex_startup_check,
)
from utils.privacy_barrier import UserPrivacyBarrier
from app.tools import RuntimeTools, build_runtime_tools
from config import paths
from config.operator_settings import apply_operator_settings, settings_values
from config.settings import Settings
from discord_adapter.gateway import DiscordGateway
from discord_adapter.io import (
    is_allowed_guild_interaction,
)
from skills.loader import SkillsIndexCache
from app.learn_log import LearnLogFeed, build_learn_log_feed
from storage.blocked_users import BlockedUserStore
from storage.coding_tasks import CodingTaskStore
from storage.image_distillations import ImageDistillationStore
from storage.model_selection import ModelSelectionStore
from storage.conversations import ConversationStore
from storage.db import Database
from storage.memory_banks import UserMemoryBankStateStore
from storage.preferences import PreferenceStore
from storage.privacy import PrivacyDeletionRequestStore
from storage.usage import UsageStore
from storage.video_sessions import VideoSessionStore
from tools.registry import ToolRegistry
from trust.resolver import TrustResolver
from trust.user_app import UserAppAccess

if TYPE_CHECKING:
    from moderation.service import ModerationService

log = logging.getLogger(__name__)

GUILD_ACTIVATION_REFRESH_SECONDS = 5.0
READY_EVENT_DRAIN_SECONDS = 5.0


class _DeferredShutdownSignal(ShutdownSignal):
    def __init__(self, get_lifecycle: Callable[[], ApplicationLifecycle | None]) -> None:
        self._get_lifecycle = get_lifecycle

    @property
    def closed(self) -> bool:
        lifecycle = self._get_lifecycle()
        if lifecycle is None:
            raise RuntimeError("Application lifecycle is not composed")
        return lifecycle.closed


class KimiCommandTree(app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction, /) -> bool:
        application = getattr(self.client, "_agent_application", None)
        if application is None:
            return True
        if not application.gateway_interactions_ready():
            log.warning(
                "Rejecting app command interaction %s while the application is not ready",
                getattr(interaction, "id", "?"),
            )
            await _reject_unready_interaction(interaction)
            return False
        active_guilds = application.active_guilds()
        if is_allowed_guild_interaction(interaction, allowed_guilds=active_guilds):
            return True
        log.warning(
            "Rejecting app command interaction %s from unapproved guild %s",
            getattr(interaction, "id", "?"),
            getattr(interaction, "guild_id", "?"),
        )
        await _reject_unapproved_guild_interaction(interaction)
        return False


class KimiBot(commands.Bot):
    _agent_application: KimiApplication | None = None

    async def close(self) -> None:
        try:
            if self._agent_application is not None:
                # Hoisted ahead of the disconnect: discord.py never cancels the
                # tasks it dispatches interactions in, so a module handler must
                # be given its bounded chance to reply while the HTTP session
                # is still open. Teardown order below is otherwise unchanged.
                try:
                    await self._agent_application.drain_interactions()
                except Exception:
                    log.exception("Error draining interactions before disconnect")
            await super().close()
        finally:
            if self._agent_application is not None:
                await self._agent_application.close()


async def _reject_unapproved_guild_interaction(
    interaction: discord.Interaction,
) -> None:
    if interaction.type is discord.InteractionType.autocomplete:
        return
    text = "This bot is not available in this server."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)
    except discord.HTTPException:
        pass


async def _reject_unready_interaction(interaction: discord.Interaction) -> None:
    if interaction.type is discord.InteractionType.autocomplete:
        return
    text = "The bot is still starting up or temporarily unavailable. Please try again shortly."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)
    except discord.HTTPException:
        pass


@dataclass
class KimiApplication:
    settings: Settings
    inherited_settings_values: Mapping[str, Any]
    bot: KimiBot
    _trust_resolver: TrustResolver = field(repr=False)
    discord_gateway: DiscordGateway
    _provider_manager: ProviderManager = field(repr=False)
    _memory_manager: MemoryManager = field(repr=False)
    _tools: RuntimeTools = field(repr=False)
    _database: Database = field(repr=False)
    _repository_bundle: AppRepositories = field(repr=False)
    learn_log: LearnLogFeed | None = None
    _privacy_barrier: UserPrivacyBarrier = field(default_factory=UserPrivacyBarrier, repr=False)
    _active_operations: ActiveOperationRegistry = field(
        default_factory=ActiveOperationRegistry, repr=False
    )
    _moderation_service: ModerationService | None = field(default=None, repr=False)
    lifecycle: ApplicationLifecycle = field(init=False, repr=False)
    _command_sync: DiscordCommandSync = field(init=False, repr=False)
    _guild_activation: GuildActivationService = field(init=False, repr=False)
    root_locks: RootLockPool = field(default_factory=RootLockPool, init=False, repr=False)
    llm_semaphore: asyncio.Semaphore = field(init=False)
    _turn_admission: TurnAdmissionController = field(init=False, repr=False)
    skills_index_cache: SkillsIndexCache = field(init=False)
    user_app_access: UserAppAccess = field(init=False)
    user_blocked: RepositoryBlockedUserCheck = field(init=False, repr=False)
    response_sender: DiscordResponseSender = field(init=False, repr=False)
    message_controller: DiscordMessageController = field(init=False, repr=False)

    def __post_init__(self) -> None:
        # Discord-facing thread handoff lives in its own boundary; the manager is
        # read live because on_ready builds it (and leaves it None when disabled).
        self.threads = ThreadHandoffBoundary(
            get_bot=lambda: self.bot,
            settings=self.settings,
            get_manager=lambda: self.thread_handoff,
        )
        self.llm_semaphore = asyncio.Semaphore(self.settings.llm_max_concurrency)
        self._turn_admission = TurnAdmissionController(
            max_active=self.settings.turn_max_concurrency,
            max_active_per_user=self.settings.turn_max_concurrency_per_user,
        )
        self.skills_index_cache = SkillsIndexCache(catalog=self.tools.skill_catalog)
        self.user_app_access = UserAppAccess(
            member_ids=frozenset(self.settings.user_app_member_id_set),
            regular_ids=frozenset(self.settings.user_app_regular_id_set),
            staff_ids=frozenset(self.settings.user_app_staff_id_set),
        )
        self.user_blocked = RepositoryBlockedUserCheck(lambda: self.blocked_user_store)
        self.response_sender = DiscordResponseSender(
            gateway=self.discord_gateway,
            workspace_locks=self.tools.workspace_locks,
        )
        self.message_controller = DiscordMessageController(
            config=MessageEntryConfig(
                allowed_channels=frozenset(self.settings.allowed_channels),
                bot_name=self.settings.bot_name,
                new_user_onboarding_turns=self.settings.new_user_onboarding_turns,
                react_turn_timeout_seconds=self.settings.react_turn_timeout_seconds,
                thread_handoff_suggest_after_tool_calls=(
                    self.settings.thread_handoff_suggest_after_tool_calls
                ),
                thread_auto_handoff_enabled=self.settings.thread_auto_handoff_enabled,
                thread_handoff_enabled=self.settings.thread_handoff_enabled,
            ),
            turn_settings=self.settings,
            bot=self.bot,
            gateway=self.discord_gateway,
            responses=self.response_sender,
            blocked_user=self.user_blocked,
            root_locks=self.root_locks,
            provider_manager=self.provider_manager,
            memory_manager=self.memory_manager,
            tools=self.tools,
            llm_semaphore=self.llm_semaphore,
            skills_index_cache=self.skills_index_cache,
            threads=self.threads,
            bindings=MessageRuntimeBindings(
                interactions_ready=self.gateway_interactions_ready,
                closed=lambda: self.lifecycle.closed,
                active_guilds=self.active_guilds,
                active_operations=lambda: self.active_operations,
                privacy_barrier=lambda: self.privacy_barrier,
                turn_admission=lambda: self.turn_admission,
                trust_resolver=lambda: self.trust_resolver,
                conversation_store=lambda: self.conversation_store,
                preference_store=lambda: self.preference_store,
                usage_store=lambda: self.repositories.usage_store,
                image_distillation_store=lambda: self.repositories.image_distillation_store,
                context_manager=lambda: self.lifecycle.resources.context_manager,
                turn_runner=lambda: self.lifecycle.resources.turn_runner,
                consent_gate=lambda: self.lifecycle.resources.consent_gate,
                personal_chat=lambda: self.user_app_chat,
                work_cancellation=lambda: self.work_cancellation,
                thread_handoff=lambda: self.thread_handoff,
                coding=lambda: self.lifecycle.resources.coding_tasks,
                moderation_service=lambda: self.moderation_service,
            ),
        )

    def gateway_interactions_ready(self) -> bool:
        """Whether a new Discord interaction may enter application code."""

        lifecycle = getattr(self, "lifecycle", None)
        return lifecycle is not None and lifecycle.interactions_ready()

    @property
    def trust_resolver(self) -> TrustResolver:
        lifecycle = getattr(self, "lifecycle", None)
        return self._trust_resolver if lifecycle is None else lifecycle.resources.trust_resolver

    @property
    def privacy_barrier(self) -> UserPrivacyBarrier:
        lifecycle = getattr(self, "lifecycle", None)
        return self._privacy_barrier if lifecycle is None else lifecycle.resources.privacy_barrier

    @property
    def active_operations(self) -> ActiveOperationRegistry:
        lifecycle = getattr(self, "lifecycle", None)
        return (
            self._active_operations if lifecycle is None else lifecycle.resources.active_operations
        )

    @property
    def turn_admission(self) -> TurnAdmissionController:
        lifecycle = getattr(self, "lifecycle", None)
        return self._turn_admission if lifecycle is None else lifecycle.resources.turn_admission

    @property
    def command_sync(self) -> DiscordCommandSync:
        lifecycle = getattr(self, "lifecycle", None)
        return self._command_sync if lifecycle is None else lifecycle.resources.command_sync

    @property
    def guild_activation(self) -> GuildActivationService:
        lifecycle = getattr(self, "lifecycle", None)
        return self._guild_activation if lifecycle is None else lifecycle.resources.guild_activation

    @property
    def provider_manager(self) -> ProviderManager:
        lifecycle = getattr(self, "lifecycle", None)
        return self._provider_manager if lifecycle is None else lifecycle.resources.provider_manager

    @property
    def memory_manager(self) -> MemoryManager:
        lifecycle = getattr(self, "lifecycle", None)
        return self._memory_manager if lifecycle is None else lifecycle.resources.memory_manager

    @property
    def tools(self) -> RuntimeTools:
        lifecycle = getattr(self, "lifecycle", None)
        return self._tools if lifecycle is None else lifecycle.resources.tools

    @property
    def database(self) -> Database:
        lifecycle = getattr(self, "lifecycle", None)
        return self._database if lifecycle is None else lifecycle.resources.database

    @property
    def moderation_service(self) -> ModerationService | None:
        lifecycle = getattr(self, "lifecycle", None)
        return (
            self._moderation_service
            if lifecycle is None
            else lifecycle.resources.moderation_service
        )

    @property
    def db_initialized(self) -> bool:
        return self.lifecycle.db_initialized

    @property
    def gateway_ready(self) -> bool:
        return self.lifecycle.gateway_ready

    @property
    def repositories(self) -> AppRepositories:
        lifecycle = getattr(self, "lifecycle", None)
        return self._repository_bundle if lifecycle is None else lifecycle.repositories

    @property
    def conversation_store(self) -> ConversationStore:
        return self.repositories.conversation_store

    @property
    def preference_store(self) -> PreferenceStore:
        return self.repositories.preference_store

    @property
    def blocked_user_store(self) -> BlockedUserStore:
        return self.repositories.blocked_user_store

    @property
    def video_session_store(self) -> VideoSessionStore:
        return self.repositories.video_session_store

    @property
    def privacy_deletion_store(self) -> PrivacyDeletionRequestStore:
        return self.repositories.privacy_deletion_store

    @property
    def user_app_consent(self) -> UserAppConsentPrompter:
        return self.lifecycle.resources.user_app_consent

    @property
    def user_app_chat(self) -> UserAppChatController:
        return self.lifecycle.resources.user_app_chat

    @property
    def work_cancellation(self) -> WorkCancellationCoordinator:
        return self.lifecycle.resources.work_cancellation

    @property
    def thread_handoff(self) -> ThreadHandoffManager | None:
        return self.lifecycle.thread_handoff

    @property
    def registry(self) -> ToolRegistry:
        return self.tools.registry

    @property
    def coding_task_store(self) -> CodingTaskStore | None:
        # The repository bundle is the canonical owner; the controller was built
        # from the same instance.
        return self.repositories.coding_task_store

    def active_guilds(self) -> set[int]:
        return self.guild_activation.active_guilds()

    async def refresh_guild_activation(self, guild_id: int | None = None) -> None:
        await self.guild_activation.refresh_guild_activation(guild_id)

    def run(self) -> int:
        if not self.settings.discord_bot_token.get_secret_value():
            log.error("DISCORD_BOT_TOKEN is not set")
            sys.exit(1)
        if not self.provider_manager.has_active_llm_credentials():
            log.error(
                "Configured model credentials are unavailable; check config/models.yaml "
                "and the referenced .env secret values."
            )
            sys.exit(1)
        context_window_warnings: Callable[[], list[ContextWindowWarning]] = getattr(
            self.provider_manager,
            "context_window_warnings",
            list,
        )
        for warning in context_window_warnings():
            log.warning(
                "COMPACTION_TRIGGER_TOKENS (%d) + REACT_MAX_TOKENS (%d) exceeds "
                "context_window (%d) for chat model %s (%s); lower the trigger or "
                "route that scope to a larger model.",
                self.settings.compaction_trigger_tokens,
                self.settings.react_max_tokens,
                warning.context_window,
                warning.model_name,
                warning.model_id,
            )
        codex_startup_check(
            self.settings,
            model_config=getattr(self.provider_manager, "model_config", None),
        )
        log.info("Starting %s...", self.settings.bot_name)
        self.bot.run(
            self.settings.discord_bot_token.get_secret_value(),
            log_handler=None,
        )
        if self.lifecycle.startup_error is not None:
            raise RuntimeError("Kimi Agent startup failed") from self.lifecycle.startup_error
        return 0

    async def close(self) -> None:
        await self.lifecycle.close()

    async def drain_interactions(self) -> None:
        """Stop admitting module interactions and wait for in-flight handlers.

        Idempotent, because the Discord client runs it before disconnecting and
        teardown repeats it for callers that close the application directly.
        """

        await self.lifecycle.drain_interactions()

    async def on_disconnect(self) -> None:
        await self.lifecycle.disconnect()

    async def on_resumed(self) -> None:
        await self.lifecycle.resume()

    async def on_ready(self) -> None:
        await self.lifecycle.ready()

    async def on_guild_join(self, guild: discord.Guild) -> None:
        await self.guild_activation.on_guild_join(guild)

    async def on_message(self, message: discord.Message) -> None:
        await self.message_controller.on_message(message)

    async def handle_message(
        self,
        message: discord.Message,
        *,
        lock_acquired: bool = False,
        resolved_conversation: ResolvedConversation | None = None,
    ) -> TurnResult | None:
        return await self.message_controller.handle_message(
            message,
            lock_acquired=lock_acquired,
            resolved_conversation=resolved_conversation,
        )


def build_app(settings: Settings) -> KimiApplication:
    # Layer the operator's settings file over the environment, before any
    # of it is captured into the config objects built below. The file wins over
    # .env on purpose: it is the operator's deliberate edit, so environment
    # precedence would make that edit silently do nothing
    # (config/operator_settings.py). The instance directory is named
    # explicitly: the process-wide default still points at the checkout here,
    # and reading the overlay from there ignored every production settings.md.
    apply_operator_settings(settings, config_dir=Path(settings.config_dir).resolve())
    inherited_settings_values = MappingProxyType(
        {
            field: tuple(value) if isinstance(value, list) else value
            for field, value in settings_values(settings).items()
        }
    )
    # Point the process-wide config-dir default at the fully resolved instance
    # directory before anything reads prompt or fragment paths.
    paths.set_default_config_dir(Path(settings.config_dir).resolve())
    # The deployment-wide tool denylist is the security-sensitive scope: prime
    # its last-known-good cache and reject a malformed cold-start policy before
    # the bot connects. Later live reload failures retain this validated value.
    load_global_blocked_tools()
    intents = discord.Intents.default()
    # Members is off in the core; optional modules may request join/leave/role
    # events. Ordinary operation reads roles off the message author.
    intents.members = settings.members_intent
    intents.message_content = settings.message_content_intent
    if settings.members_intent:
        log.info(
            "MEMBERS_INTENT is on: optional modules can receive member join/leave "
            "and role-change events. Server Members must also be enabled in the "
            "Discord Developer Portal or the gateway will reject the connection."
        )
    if not settings.message_content_intent:
        log.warning(
            "MESSAGE_CONTENT_INTENT is off. Running in degraded mode: @mentions "
            "and replies work, but the 'hey %s' "
            "text trigger, thread auto-reply, and discord_text_search will not.",
            settings.bot_name,
        )
    bot = KimiBot(
        command_prefix="!",
        intents=intents,
        tree_cls=KimiCommandTree,
        allowed_installs=app_commands.AppInstallationType(guild=True, user=False),
        allowed_contexts=app_commands.AppCommandContext(
            guild=True,
            dm_channel=False,
            private_channel=False,
        ),
        allowed_mentions=discord.AllowedMentions(
            everyone=False,
            roles=False,
            users=False,
            replied_user=True,
        ),
    )
    trust_resolver = TrustResolver(
        staff_role_ids=settings.staff_role_id_set,
        regular_role_ids=settings.regular_role_id_set,
        staff_ids=settings.staff_ids,
        guild_trust_loader=load_guild_trust,
    )
    gateway = DiscordGateway(
        bot_user_provider=lambda: bot.user,
        trust_resolver=trust_resolver,
    )
    provider_manager = build_provider_manager(settings)
    registry = ToolRegistry()
    # Built before the tool layer so both knowledge sinks (community memory and
    # skill documents) can be handed the same audit hook at registration.
    learn_log = build_learn_log_feed(
        get_bot=lambda: bot,
        is_guild_active=lambda guild_id: guild_id in application.active_guilds(),
    )
    memory_manager = MemoryManager(
        settings=settings,
        registry=registry,
        on_learn=learn_log.record,
    )
    moderation_service = build_moderation_service(settings)
    database = Database(
        path=settings.database_path,
        encryption_key=settings.database_encryption_key.get_secret_value() or None,
    )
    repositories = AppRepositories(
        conversation_store=ConversationStore(database),
        preference_store=PreferenceStore(database),
        blocked_user_store=BlockedUserStore(database),
        model_selection_store=ModelSelectionStore(database),
        image_distillation_store=ImageDistillationStore(database),
        usage_store=UsageStore(database),
        video_session_store=VideoSessionStore(database),
        coding_task_store=CodingTaskStore(database),
        privacy_deletion_store=PrivacyDeletionRequestStore(database),
        user_memory_bank_state_store=UserMemoryBankStateStore(database),
    )
    application = KimiApplication(
        settings=settings,
        inherited_settings_values=inherited_settings_values,
        bot=bot,
        _trust_resolver=trust_resolver,
        discord_gateway=gateway,
        _provider_manager=provider_manager,
        _memory_manager=memory_manager,
        _moderation_service=moderation_service,
        learn_log=learn_log,
        _tools=build_runtime_tools(
            settings,
            gateway,
            provider_manager,
            memory_manager,
            registry=registry,
            on_learn=learn_log.record,
            get_preference_store=lambda: application.preference_store,
            get_blocked_user_store=lambda: application.blocked_user_store,
            get_video_session_store=lambda: application.video_session_store,
            get_thread_handoff=lambda: application.thread_handoff,
            resolve_thread_target=lambda ctx, raw: application.threads.resolve_thread_target(
                ctx, raw
            ),
            can_manage_thread=lambda ctx, thread_id: application.threads.can_manage_thread(
                ctx, thread_id
            ),
        ),
        _database=database,
        _repository_bundle=repositories,
    )
    guild_activation = GuildActivationService(
        config=GuildActivationConfig(
            config_dir=Path(settings.config_dir).resolve(),
            allowed_guilds=frozenset(settings.allowed_guilds),
            refresh_seconds=GUILD_ACTIVATION_REFRESH_SECONDS,
        ),
        bot=bot,
        module_manager=application.tools.module_manager,
        activation_parser=server_setup_activation,
    )
    application._guild_activation = guild_activation
    lifecycle_ref: ApplicationLifecycle | None = None
    shutdown_signal = _DeferredShutdownSignal(lambda: lifecycle_ref)
    command_sync = DiscordCommandSync(
        tree=bot.tree,
        get_guild_sync_port=lambda: (
            lifecycle_ref.module_interaction_runtime if lifecycle_ref is not None else None
        ),
        config=CommandSyncConfig(
            drain_timeout_seconds=READY_EVENT_DRAIN_SECONDS,
        ),
        shutdown=shutdown_signal,
    )
    application._command_sync = command_sync
    context_manager = ContextManager(store=repositories.conversation_store)
    turn_runner = application.message_controller.make_foreground_turn_runner(
        conversation_store=repositories.conversation_store,
        context_manager=context_manager,
    )
    consent_gate = PrivacyConsentGate(
        enabled=settings.privacy_consent_enabled,
        title=settings.privacy_consent_title,
        text=settings.privacy_consent_text,
        timeout=settings.privacy_consent_timeout,
        preference_store=repositories.preference_store,
        redispatch=application.on_message,
        is_available=application.gateway_interactions_ready,
    )
    user_app_consent = UserAppConsentPrompter(
        config=UserAppConsentConfig(
            enabled=settings.privacy_consent_enabled,
            title=settings.privacy_consent_title,
            text=settings.privacy_consent_text,
            timeout=settings.privacy_consent_timeout,
        ),
        preference_store=repositories.preference_store,
    )
    coding_task_controller = CodingTaskController(
        settings=settings,
        store=repositories.coding_task_store,
        usage_store=repositories.usage_store,
        provider_manager=provider_manager,
        source_registry=application.registry,
        tools=application.tools,
        llm_semaphore=application.llm_semaphore,
        privacy_barrier=application.privacy_barrier,
        user_blocked=application.user_blocked,
        delivery=CodingDelivery(
            bot=bot,
            store=repositories.coding_task_store,
            conversation_store=repositories.conversation_store,
            discord_gateway=gateway,
            workspace_locks=application.tools.workspace_locks,
            root_locks=application.root_locks,
            threads=application.threads,
            moderation_service=moderation_service,
            config=CodingDeliveryConfig(
                thread_handoff_enabled=settings.thread_handoff_enabled,
                thread_auto_handoff_enabled=settings.thread_auto_handoff_enabled,
                bot_name=settings.bot_name,
            ),
            strip_message_invocation=application.message_controller.strip_message_invocation,
        ),
    )
    work_cancellation: WorkCancellationCoordinator | None = None

    async def cancel_personal_work(
        *,
        user_id: str,
        channel_id: str,
        root_key: str,
    ) -> bool:
        coordinator = work_cancellation
        if coordinator is None:
            raise RuntimeError("Work cancellation coordinator is not initialized")
        summary = await coordinator.cancel_for_reset(
            user_id=user_id,
            scope=WorkScope(channel_id=channel_id, root_key=root_key),
        )
        return summary.clean

    user_app_chat = UserAppChatController(
        config=UserAppChatConfig(
            timeout_seconds=settings.user_app_chat_timeout_seconds,
            dm_enabled=settings.user_app_dm_enabled,
        ),
        bot=bot,
        access=application.user_app_access,
        user_blocked=application.user_blocked,
        consent=user_app_consent,
        conversation_store=repositories.conversation_store,
        active_operations=application.active_operations,
        privacy_barrier=application.privacy_barrier,
        turn_admission=application.turn_admission,
        root_locks=application.root_locks,
        turn_runner=turn_runner,
        shutdown=shutdown_signal,
        cancel_personal_work=cancel_personal_work,
        turn_entry_hooks=application.message_controller.turn_entry_hooks(),
    )
    work_cancellation = WorkCancellationCoordinator(
        bot=bot,
        consent_gate=consent_gate,
        personal_requests=user_app_chat,
        active_operations=application.active_operations,
        coding_tasks=coding_task_controller,
        trust_resolver=trust_resolver,
        discord_gateway=gateway,
        conversation_resolver=application.message_controller.resolve_conversation_for_message,
        response_sender=application.response_sender.send,
        strip_message_invocation=application.message_controller.strip_message_invocation,
        cleanup_wait_seconds=settings.coding_stop_cleanup_wait_seconds,
        global_staff_ids=frozenset(settings.staff_ids),
    )
    lifecycle = ApplicationLifecycle(
        LifecycleResources(
            settings=settings,
            bot=bot,
            database=database,
            provider_manager=provider_manager,
            memory_manager=memory_manager,
            tools=application.tools,
            repositories=repositories,
            turn_admission=application.turn_admission,
            active_operations=application.active_operations,
            consent_gate=consent_gate,
            privacy_barrier=application.privacy_barrier,
            moderation_service=moderation_service,
            guild_activation=guild_activation,
            command_sync=command_sync,
            coding_tasks=coding_task_controller,
            module_manager=application.tools.module_manager,
            trust_resolver=trust_resolver,
            context_manager=context_manager,
            turn_runner=turn_runner,
            user_app_consent=user_app_consent,
            user_app_chat=user_app_chat,
            work_cancellation=work_cancellation,
            callbacks=LifecycleCallbacks(
                gateway_interactions_ready=application.gateway_interactions_ready,
                active_guilds=application.active_guilds,
                refresh_guild_activation=application.refresh_guild_activation,
                lock_user_conversations=lambda user_id: (
                    application.root_locks.hold_user_conversations(
                        user_id,
                        application.conversation_store,
                    )
                ),
                run_learn=application.message_controller.run_learn_turn,
                is_user_blocked=application.user_blocked,
                model_log_label=application.message_controller.model_log_label,
            ),
        )
    )
    lifecycle_ref = lifecycle
    application.lifecycle = lifecycle
    bot._agent_application = application
    bot.event(application.on_ready)
    bot.event(application.on_disconnect)
    bot.event(application.on_resumed)
    bot.event(application.on_message)
    bot.event(application.on_guild_join)
    return application
