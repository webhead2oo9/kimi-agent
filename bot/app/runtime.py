from __future__ import annotations

import asyncio
import logging
import os
import sys
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

import discord
from discord import app_commands
from discord.ext import commands
from pydantic import SecretStr

from agent.attachments import (
    collect_reply_context,
    collect_turn_attachments,
    collect_turn_images,
    turn_has_image_input,
)
from agent.backfill import clean_message_text, message_source_timestamp, strip_chunk_marker
from workspace import WorkspaceKey
from agent.auto_handoff import build_auto_handoff_request
from config.fragments.channel_pins import (
    filter_pins_to_searchable,
    load_channel_auto_thread,
    load_channel_blocked_tools,
    load_channel_pinned_tools,
)
from config.fragments.guild_config import (
    load_guild_blocked_tools,
    load_guild_pinned_tools,
    load_guild_trust,
    server_setup_activation,
)
from agent.context import ContextManager
from utils.format import sanitize_author_name
from config.fragments.tool_config import load_tool_configs
from config.fragments.tool_policy import (
    load_blocked_tools,
    load_global_blocked_tools,
)
from agent.core import run_conversation
from agent.turn import (
    TurnExecutionConfig,
    TurnPreparationInput,
    TurnRequest,
    TurnResult,
    handle_turn,
)
from app.admission import (
    TURN_ADMISSION_BUSY_MESSAGE,
    TurnAdmissionController,
)
from app.cancellation import ActiveOperationRegistry
from app.conversation_routing import (
    ResolvedConversation,
    conversation_key_for_message,
    referenced_message_id,
    resolve_conversation_for_message,
    response_lock_key,
)
from app.consent import PrivacyConsentGate
from app.coding_jobs import CodingJobManager
from app.coding_tasks import CodingTaskRuntime, CodingTaskService
from app.control_plane import (
    RESTART_EXIT_CODE,
    ControlPlaneStore,
    ManagedConfigurationService,
    RestartCoordinator,
    apply_managed_settings,
    managed_models_path,
)
from app.modules import ModuleRuntimeContext, module_capabilities
from app.proposals import DurableProposalService
from app.thread_handoff_boundary import THREAD_HANDOFF_REACTION, ThreadHandoffBoundary
from app.threads import ThreadHandoffManager
from app.memory import MemoryManager
from app.moderation import build_moderation_service
from app.providers import (
    ContextWindowWarning,
    ProviderManager,
    build_provider_manager,
    codex_startup_check,
)
from utils.privacy_barrier import PrivacyDeletionPendingError, UserPrivacyBarrier
from app.tools import RuntimeTools, build_runtime_tools
from app.turn_entry import (
    TurnEntryHooks,
    build_turn_dependencies,
    chat_model_name_for_scope,
    build_turn_preparation_config,
    resolve_parent_channel_id,
)
from discord_adapter.lifecycle import (
    attachment_orphan_sweeper,
    auto_retain_sweeper,
    sweep_attachment_orphans_once,
    transcript_retention_sweeper,
    workspace_sweeper,
)
from commands.learn_cmd import register_learn_command
from commands.memory_cmd import register_memory_command
from commands.models_cmd import register_models_command
from commands.moderation_cmd import register_moderation_command
from commands.privacy_cmd import (
    drain_confirmed_privacy_deletions,
    register_privacy_command,
    run_privacy_deletion,
)
from commands.proposals_cmd import register_proposals_command
from commands.usage_cmd import register_usage_command
from commands.stop_cmd import register_stop_command
from config import paths
from config.model_config import Scope
from config.operator_settings import apply_operator_settings, settings_values
from config.settings import Settings
from discord_adapter.gateway import DiscordGateway
from discord_adapter.io import (
    DiscordActivityReporter,
    can_send_reply,
    chunk_message,
    is_allowed_guild_interaction,
    is_eligible_to_respond,
    should_respond,
    strip_mention,
    suppress_link_previews,
)
from memory.auto_retain import AutoRetainFlusher
from memory.banks import ensure_user_bank
from memory.recall import recall_current_user_context
from moderation.types import Direction
from storage.auto_retain import AutoRetainStore
from observability.events import start_event_writer, stop_event_writer
from providers.assets import write_generated_assets
from providers.types import ContentPart
from skills.loader import SkillsIndexCache
from app.learn_log import LearnLogFeed, build_learn_log_feed
from app.learn_turn import run_learn_turn
from tools.learn import LearnTarget
from storage.blocked_users import BlockedUserStore
from storage.coding_tasks import (
    CodingTask,
    CodingTaskStatus,
    CodingTaskStore,
)
from storage.image_distillations import ImageDistillationStore
from storage.model_selection import ModelSelectionStore
from storage.conversations import ChannelMessageRecord, ConversationStore
from storage.db import Database
from storage.memory_banks import UserMemoryBankStateStore
from storage.preferences import PreferenceStore
from storage.privacy import PrivacyDeletionRequestStore
from storage.usage import UsageStore
from tools.embeds import embed_transcript_summary
from tools.registry import ToolRegistry
from tools.coding_tasks import CODING_CONTROL_TOOLS, init_coding_control_tools
from tools.user_memory import set_user_memory_preference_store
from trust.resolver import TrustResolver
from trust.tiers import TrustTier

if TYPE_CHECKING:
    from moderation.service import ModerationService
    from tools.embeds import EmbedSpec


def _settings_secret_values(settings: Settings) -> tuple[str, ...]:
    """Every non-empty secret the settings hold, for the event log to redact.

    Reads the declared fields rather than a hand-kept list, so a new ``SecretStr``
    setting is covered the day it is added.
    """
    values = (getattr(settings, field_name) for field_name in type(settings).model_fields)
    return tuple(
        secret
        for value in values
        if isinstance(value, SecretStr)
        if (secret := value.get_secret_value())
    )


log = logging.getLogger(__name__)

GUILD_ACTIVATION_REFRESH_SECONDS = 5.0


class KimiCommandTree(app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction, /) -> bool:
        application = getattr(self.client, "_agent_application", None)
        if application is None:
            return True
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


@dataclass
class KimiApplication:
    settings: Settings
    inherited_settings_values: Mapping[str, Any]
    bot: KimiBot
    trust_resolver: TrustResolver
    discord_gateway: DiscordGateway
    provider_manager: ProviderManager
    memory_manager: MemoryManager
    tools: RuntimeTools
    database: Database
    proposal_service: DurableProposalService | None = None
    configuration_service: ManagedConfigurationService | None = None
    restart_coordinator: RestartCoordinator | None = None
    control_plane_store: ControlPlaneStore | None = None
    context_manager: ContextManager | None = None
    conversation_store: ConversationStore | None = None
    preference_store: PreferenceStore | None = None
    blocked_user_store: BlockedUserStore | None = None
    model_selection_store: ModelSelectionStore | None = None
    learn_log: LearnLogFeed | None = None
    image_distillation_store: ImageDistillationStore | None = None
    usage_store: UsageStore | None = None
    coding_task_store: CodingTaskStore | None = None
    coding_tasks: CodingTaskService | None = None
    privacy_deletion_store: PrivacyDeletionRequestStore | None = None
    user_memory_bank_state_store: UserMemoryBankStateStore | None = None
    privacy_barrier: UserPrivacyBarrier = field(default_factory=UserPrivacyBarrier)
    active_operations: ActiveOperationRegistry = field(default_factory=ActiveOperationRegistry)
    consent_gate: PrivacyConsentGate | None = None
    moderation_service: ModerationService | None = None
    thread_handoff: ThreadHandoffManager | None = None
    db_initialized: bool = False
    workspace_sweeper_started: bool = False
    auto_retain_sweeper_started: bool = False
    transcript_retention_sweeper_started: bool = False
    gateway_ready: bool = False
    active_transcript_retention_days: int = 0
    active_transcript_retention_sweep_interval_seconds: int | None = None
    _auto_retain_task: asyncio.Task | None = None
    _attachment_sweeper_task: asyncio.Task | None = None
    _workspace_sweeper_task: asyncio.Task | None = None
    _transcript_retention_task: asyncio.Task | None = None
    _guild_activation_refresh_task: asyncio.Task | None = None
    context_locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    _lock_refcounts: dict[str, int] = field(default_factory=dict)
    llm_semaphore: asyncio.Semaphore = field(init=False)
    turn_admission: TurnAdmissionController = field(init=False)
    skills_index_cache: SkillsIndexCache = field(init=False)
    _guild_activation_cache: paths.GuildActivationCache = field(init=False, repr=False)
    _ready_init_lock: asyncio.Lock = field(init=False, repr=False)
    _closed: bool = False
    _startup_error: Exception | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        # Discord-facing thread handoff lives in its own boundary; the manager is
        # read live because on_ready builds it (and leaves it None when disabled).
        self.threads = ThreadHandoffBoundary(
            get_bot=lambda: self.bot,
            settings=self.settings,
            get_manager=lambda: self.thread_handoff,
        )
        self.llm_semaphore = asyncio.Semaphore(self.settings.llm_max_concurrency)
        self.turn_admission = TurnAdmissionController(
            max_active=self.settings.turn_max_concurrency,
            max_active_per_user=self.settings.turn_max_concurrency_per_user,
        )
        self.skills_index_cache = SkillsIndexCache(catalog=self.tools.skill_catalog)
        self._guild_activation_cache = paths.GuildActivationCache(
            Path(self.settings.config_dir).resolve(),
            lambda content: server_setup_activation(
                content,
                validators=(self.tools.module_manager.validate_guild_config,),
            ),
        )
        self._guild_activation_cache.refresh()
        self._ready_init_lock = asyncio.Lock()

    @property
    def registry(self) -> ToolRegistry:
        return self.tools.registry

    def active_guilds(self) -> set[int]:
        """Guilds enabled by validated setup or the deployment allowlist.

        A validated explicit deactivation wins over the environment allowlist.
        This hot-path read uses an immutable cache; it never scans the config
        directory synchronously while processing a Discord event.
        """
        setup = self._guild_activation_cache.snapshot()
        return (self.settings.allowed_guilds | set(setup.active)) - set(setup.deactivated)

    def guild_activation_state(self, guild_id: int) -> dict[str, Any]:
        setup = self._guild_activation_cache.snapshot()
        environment_approved = guild_id in self.settings.allowed_guilds
        if guild_id in setup.deactivated:
            setup_state = "deactivated"
            activation = "deactivated"
            active = False
        elif guild_id in setup.active:
            setup_state = "active"
            activation = "server_setup"
            active = True
        elif guild_id in setup.invalid:
            setup_state = "invalid"
            active = environment_approved
            activation = "environment" if active else "invalid_setup"
        else:
            setup_state = "missing"
            active = environment_approved
            activation = "environment" if active else "pending"
        return {
            "active": active,
            "activation": activation,
            "setup_state": setup_state,
            "environment_approved": environment_approved,
        }

    async def refresh_guild_activation(self, guild_id: int | None = None) -> None:
        if guild_id is None:
            await asyncio.to_thread(self._guild_activation_cache.refresh)
        else:
            await asyncio.to_thread(self._guild_activation_cache.refresh_guild, guild_id)

    async def activate_managed_config(self, config_dir: Path) -> None:
        """Switch live fragment readers to one validated immutable revision."""
        resolved = await asyncio.to_thread(config_dir.resolve)
        self.settings.config_dir = str(resolved)
        paths.set_default_config_dir(resolved)
        self._guild_activation_cache = paths.GuildActivationCache(
            resolved,
            lambda content: server_setup_activation(
                content,
                validators=(self.tools.module_manager.validate_guild_config,),
            ),
        )
        await self.refresh_guild_activation()

    async def _guild_activation_refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(GUILD_ACTIVATION_REFRESH_SECONDS)
            try:
                await self.refresh_guild_activation()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Could not refresh guild activation config")

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
        if self._startup_error is not None:
            raise RuntimeError("Kimi Agent startup failed") from self._startup_error
        if self.restart_coordinator is not None and self.restart_coordinator.requested:
            return RESTART_EXIT_CODE
        return 0

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.gateway_ready = False
        if self._guild_activation_refresh_task is not None:
            self._guild_activation_refresh_task.cancel()
            try:
                await self._guild_activation_refresh_task
            except asyncio.CancelledError:
                pass
            except Exception:
                log.exception("Error stopping guild activation refresher")
            self._guild_activation_refresh_task = None
        if self._auto_retain_task is not None:
            self._auto_retain_task.cancel()
            try:
                await self._auto_retain_task
            except asyncio.CancelledError:
                pass
            except Exception:
                log.exception("Error stopping auto-retain sweeper")
            self._auto_retain_task = None
        if self._workspace_sweeper_task is not None:
            self._workspace_sweeper_task.cancel()
            try:
                await self._workspace_sweeper_task
            except asyncio.CancelledError:
                pass
            except Exception:
                log.exception("Error stopping workspace sweeper")
            self._workspace_sweeper_task = None
        if self._attachment_sweeper_task is not None:
            self._attachment_sweeper_task.cancel()
            try:
                await self._attachment_sweeper_task
            except asyncio.CancelledError:
                pass
            except Exception:
                log.exception("Error stopping attachment orphan sweeper")
            self._attachment_sweeper_task = None
        if self._transcript_retention_task is not None:
            self._transcript_retention_task.cancel()
            try:
                await self._transcript_retention_task
            except asyncio.CancelledError:
                pass
            except Exception:
                log.exception("Error stopping transcript retention sweeper")
            self._transcript_retention_task = None
            self.transcript_retention_sweeper_started = False
            self.active_transcript_retention_days = 0
            self.active_transcript_retention_sweep_interval_seconds = None
        await drain_confirmed_privacy_deletions()
        await stop_event_writer()
        if self.coding_tasks is not None:
            await self.coding_tasks.close()
            self.coding_tasks = None
        try:
            await self.tools.browser_service.close()
        except Exception:
            log.exception("Error closing browser service")
        if self.moderation_service is not None:
            try:
                await self.moderation_service.close()
            except Exception:
                log.exception("Error closing moderation service")
        await self.tools.module_manager.close()
        await self.memory_manager.close()
        await self.provider_manager.close()
        await self.database.close()

    async def on_disconnect(self) -> None:
        self.gateway_ready = False

    async def on_ready(self) -> None:
        self.gateway_ready = True
        log.info(
            "Logged in as %s (ID: %s)",
            self.bot.user,
            self.bot.user.id if self.bot.user else "?",
        )
        log.info(
            "LLM Models: chat=%s | compaction=%s",
            self._model_log_label("chat"),
            self._model_log_label("compaction"),
        )
        log.info(
            "Trust tiers: StaffRoleIDs=%s, RegularRoleIDs=%s",
            self.settings.staff_role_ids,
            self.settings.regular_role_ids,
        )

        active_guilds = self.active_guilds()
        pending_guilds = sum(1 for guild in self.bot.guilds if guild.id not in active_guilds)
        log.info(
            "Guild activation: %d active, %d connected and silent",
            len(active_guilds),
            pending_guilds,
        )

        if self.settings.tool_event_log_enabled:
            start_event_writer(
                self.settings.tool_event_log_path,
                self.settings.tool_event_log_max_field_bytes,
                content_mode=self.settings.tool_event_log_content_mode,
                secret_values=_settings_secret_values(self.settings),
            )
            if self.settings.tool_event_log_content_mode == "full":
                log.warning(
                    "Tool event log enabled at %s in full mode; sensitive content may be written",
                    self.settings.tool_event_log_path,
                )
            else:
                log.info(
                    "Tool event log enabled at %s in %s mode",
                    self.settings.tool_event_log_path,
                    self.settings.tool_event_log_content_mode,
                )

        async with self._ready_init_lock:
            if self._startup_error is not None:
                return
            first_init = not self.db_initialized
            if first_init:
                try:
                    await self._first_init_core()
                except Exception as exc:
                    self._startup_error = exc
                    log.critical("Kimi Agent startup failed; closing the client", exc_info=True)
                    await self.bot.close()
                    return

            if self.memory_manager.client:
                assert self.conversation_store is not None
                assert self.preference_store is not None
                await self.memory_manager.ensure_ready(
                    self.conversation_store,
                    self.preference_store,
                )
            else:
                log.warning("No Hindsight URL configured - running without memory")

            if first_init:
                self.db_initialized = True
                log.info("Database initialized at %s", self.settings.database_path)

        try:
            synced = await self.bot.tree.sync()
            log.info("Synced %d slash command(s)", len(synced))
        except discord.HTTPException:
            # Command propagation is retried on the next READY, but a transient
            # Discord failure must not prevent local sweepers from starting.
            log.warning("Failed to sync global slash commands", exc_info=True)

        # READY events can overlap on reconnect. Serialize the check/start pair so
        # only one copy of each filesystem maintenance loop is created.
        async with self._ready_init_lock:
            if not self.workspace_sweeper_started:
                await sweep_attachment_orphans_once(
                    self.tools.attachment_store,
                    max_age_seconds=self.settings.attachment_orphan_ttl_seconds,
                    max_files=self.settings.attachment_orphan_sweep_max_files,
                )
                # ``close()`` can run while the startup sweep is off-thread. It
                # cannot cancel a task that has not been installed yet, so honor
                # the closed state again before creating either maintenance loop.
                if self._closed:
                    return
                self._workspace_sweeper_task = asyncio.create_task(
                    workspace_sweeper(
                        self.tools.workspace_manager,
                        sweep_interval=self.settings.workspace_sweep_interval,
                        workspace_locks=self.tools.workspace_locks,
                        browser_profiles=self.tools.browser_service,
                    )
                )
                self._attachment_sweeper_task = asyncio.create_task(
                    attachment_orphan_sweeper(
                        self.tools.attachment_store,
                        sweep_interval=(self.settings.attachment_orphan_sweep_interval_seconds),
                        max_age_seconds=self.settings.attachment_orphan_ttl_seconds,
                        max_files=self.settings.attachment_orphan_sweep_max_files,
                    )
                )
                self.workspace_sweeper_started = True
                log.info(
                    "Filesystem sweepers started (workspace TTL: %ds; "
                    "attachment orphan TTL: %ds, every %ds)",
                    self.settings.workspace_file_ttl,
                    self.settings.attachment_orphan_ttl_seconds,
                    self.settings.attachment_orphan_sweep_interval_seconds,
                )

        if (
            not self.transcript_retention_sweeper_started
            and self.settings.transcript_retention_days > 0
            and self.conversation_store is not None
        ):
            retention_days = self.settings.transcript_retention_days
            sweep_interval = self.settings.transcript_retention_sweep_interval_seconds
            self._transcript_retention_task = asyncio.create_task(
                transcript_retention_sweeper(
                    self.conversation_store,
                    retention_days=retention_days,
                    sweep_interval=sweep_interval,
                )
            )
            self.transcript_retention_sweeper_started = True
            self.active_transcript_retention_days = retention_days
            self.active_transcript_retention_sweep_interval_seconds = sweep_interval
            log.info(
                "Transcript retention sweeper started (window: %dd, every %ds)",
                retention_days,
                sweep_interval,
            )

        active_memory_client = self.memory_manager.active_client()
        if (
            not self.auto_retain_sweeper_started
            and self.settings.memory_auto_retain_enabled
            and active_memory_client is not None
            and self.preference_store is not None
        ):
            flusher = AutoRetainFlusher(
                store=AutoRetainStore(self.database),
                preference_store=self.preference_store,
                memory_client=active_memory_client,
                ensure_user_bank=ensure_user_bank,
                get_bot_name=lambda: self.settings.bot_name,
                idle_seconds=self.settings.memory_auto_retain_idle_minutes * 60,
                backfill_horizon_seconds=(
                    self.settings.memory_auto_retain_backfill_horizon_hours * 3600
                ),
                min_user_chars=self.settings.memory_auto_retain_min_user_chars,
                max_content_chars=self.settings.memory_auto_retain_max_content_chars,
                max_flushes_per_sweep=(self.settings.memory_auto_retain_max_flushes_per_sweep),
            )
            self._auto_retain_task = asyncio.create_task(
                auto_retain_sweeper(
                    flusher,
                    sweep_interval=(self.settings.memory_auto_retain_sweep_interval_seconds),
                )
            )
            self.auto_retain_sweeper_started = True
            log.info(
                "Auto-retain sweeper started (idle: %dm, every %ds)",
                self.settings.memory_auto_retain_idle_minutes,
                self.settings.memory_auto_retain_sweep_interval_seconds,
            )

        if self._guild_activation_refresh_task is None and not self._closed:
            self._guild_activation_refresh_task = asyncio.create_task(
                self._guild_activation_refresh_loop()
            )
            log.info(
                "Guild activation refresher started (every %.0fs)",
                GUILD_ACTIVATION_REFRESH_SECONDS,
            )

        handshake_revision = os.environ.get("KIMI_CONTROL_REVISION", "").strip()
        if handshake_revision and self.control_plane_store is not None:
            self.control_plane_store.mark_healthy(handshake_revision)
            if self.proposal_service is not None:
                await self.proposal_service.reconcile_control_state(
                    self.control_plane_store.state()
                )

    async def _first_init_core(self) -> None:
        """One-time startup wiring: DB connect, stores, gates, slash commands.

        Runs under the READY initialization lock in ``on_ready``; the caller
        marks ``db_initialized`` only after the complete startup path succeeds.
        """
        await self.database.connect()
        self.conversation_store = ConversationStore(self.database)
        self.preference_store = PreferenceStore(self.database)
        self.blocked_user_store = BlockedUserStore(self.database)
        self.image_distillation_store = ImageDistillationStore(self.database)
        self.model_selection_store = ModelSelectionStore(self.database)
        await self.provider_manager.refresh_selectable_chat_models()
        selected_model = await self.model_selection_store.get()
        try:
            self.provider_manager.set_active_chat_model(selected_model)
        except ValueError:
            log.warning(
                "Stored chat model %r is no longer operator-selectable; reverting to config",
                selected_model,
            )
            await self.model_selection_store.set(None)
            self.provider_manager.set_active_chat_model(None)
        self.usage_store = UsageStore(self.database)
        self.coding_task_store = CodingTaskStore(self.database)
        self.privacy_deletion_store = PrivacyDeletionRequestStore(self.database)
        self.user_memory_bank_state_store = UserMemoryBankStateStore(self.database)
        configure_bank_tracking = getattr(
            self.memory_manager.client,
            "set_user_bank_state_store",
            None,
        )
        if configure_bank_tracking is not None:
            configure_bank_tracking(self.user_memory_bank_state_store)
        set_user_memory_preference_store(self.preference_store)
        auto_retain_watermarks = AutoRetainStore(self.database)
        await self._resume_pending_privacy_deletions(
            auto_retain_watermarks=auto_retain_watermarks,
        )
        self.consent_gate = PrivacyConsentGate(
            enabled=self.settings.privacy_consent_enabled,
            title=self.settings.privacy_consent_title,
            text=self.settings.privacy_consent_text,
            timeout=self.settings.privacy_consent_timeout,
            preference_store=self.preference_store,
            redispatch=self.on_message,
        )
        self.context_manager = ContextManager(store=self.conversation_store)
        if self.settings.thread_handoff_enabled:
            self.thread_handoff = ThreadHandoffManager(self.conversation_store)
            await self.thread_handoff.load()
            log.info(
                "Thread handoff enabled (%d managed thread(s), %d auto-responding)",
                self.thread_handoff.managed_count,
                self.thread_handoff.auto_respond_count,
            )
        register_memory_command(
            self.bot,
            self.preference_store,
            privacy_barrier=self.privacy_barrier,
        )
        register_models_command(
            self.bot,
            self.provider_manager,
            self.model_selection_store,
            owner_user_id=self.settings.owner_user_id,
        )
        if self.proposal_service is not None:
            register_proposals_command(
                self.bot,
                self.proposal_service,
                owner_user_id=self.settings.owner_user_id,
                configuration=self.configuration_service,
            )
        register_moderation_command(
            self.bot,
            self.blocked_user_store,
            self.trust_resolver,
        )
        register_usage_command(
            self.bot,
            self.usage_store,
            self.trust_resolver,
        )
        register_stop_command(self.bot, self._handle_stop_interaction)
        register_learn_command(
            self.bot,
            self.trust_resolver,
            run_learn=self._run_learn_turn,
            bot_name=self.settings.bot_name,
        )
        register_privacy_command(
            self.bot,
            self.conversation_store,
            self.preference_store,
            memory_client=self.memory_manager.client,
            auto_retain_watermarks=auto_retain_watermarks,
            deletion_request_store=self.privacy_deletion_store,
            memory_bank_state_store=self.user_memory_bank_state_store,
            conversation_turn_lock=self._lock_user_conversation_turns,
            workspace_manager=self.tools.workspace_manager,
            workspace_locks=self.tools.workspace_locks,
            privacy_barrier=self.privacy_barrier,
            retention_days=self.settings.transcript_retention_days,
            bot_name=self.settings.bot_name,
            policy_url=self.settings.privacy_policy_url,
            browser_data_store=self.tools.browser_service,
            cancel_user_work=self._cancel_user_for_privacy,
        )
        await self.tools.module_manager.start(
            ModuleRuntimeContext(
                bot=self.bot,
                database=self.database,
                trust_resolver=self.trust_resolver,
                gateway=self.discord_gateway,
                config_dir=self.tools.module_manager.config_dir,
                is_guild_active=lambda guild_id: guild_id in self.active_guilds(),
                get_module=self.tools.module_manager.get,
                capabilities=module_capabilities(self.settings),
                proposals=self.proposal_service,
                configuration=self.configuration_service,
                restart=self.restart_coordinator,
            )
        )
        # Start the durable scheduler only after pending privacy deletions have
        # replayed and their barriers are installed. This prevents recovered
        # work from racing a deletion request during READY initialization.
        await self._init_coding_tasks()

    async def _cancel_user_for_privacy(self, user_id: str) -> None:
        await self.active_operations.cancel(
            user_id=user_id,
            root_key=None,
            channel_id="",
            all_operations=True,
            wait_seconds=self.settings.coding_stop_cleanup_wait_seconds,
        )
        if self.coding_tasks is not None:
            await self.coding_tasks.cancel_for_scope(
                user_id=user_id,
                root_key=None,
                all_tasks=True,
            )

    async def _init_coding_tasks(self) -> None:
        model_config = self.provider_manager.model_config
        coding_model = model_config.roles.coding if model_config is not None else None
        sandbox_config = self.tools.code_sandbox_config
        if not self.settings.coding_tasks_enabled:
            log.info("Coding tasks disabled; CODING_TASKS_ENABLED is false")
            return
        if coding_model is None:
            log.warning("Coding tasks requested but config/models.yaml assigns no coding role")
            return
        if sandbox_config is None:
            log.warning("Coding tasks requested but the code sandbox is unavailable")
            return
        code_exec_guards = self.tools.code_exec_guards
        if code_exec_guards is None:
            log.warning("Coding tasks requested but code execution guards are unavailable")
            return
        assert self.usage_store is not None
        self.coding_task_store = self.coding_task_store or CodingTaskStore(self.database)
        jobs = CodingJobManager(
            store=self.coding_task_store,
            workspace_manager=self.tools.workspace_manager,
            workspace_locks=self.tools.workspace_locks,
            sandbox_config=sandbox_config,
            max_seconds=self.settings.coding_job_max_seconds,
            max_cpu_seconds=self.settings.coding_job_max_cpu_seconds,
            runtime_guards=code_exec_guards,
            usage_store=self.usage_store,
            user_activity=self.privacy_barrier.activity,
        )
        self.coding_tasks = CodingTaskService(
            CodingTaskRuntime(
                settings=self.settings,
                store=self.coding_task_store,
                usage_store=self.usage_store,
                provider_manager=self.provider_manager,
                source_registry=self.registry,
                jobs=jobs,
                llm_semaphore=self.llm_semaphore,
                compactor=self.provider_manager.build_compactor(self.llm_semaphore),
                model_config=model_config,
                notifier=self._publish_coding_task,
                user_activity=self.privacy_barrier.activity,
                workspace_manager=self.tools.workspace_manager,
                blocked_tools=load_blocked_tools,
                tool_configs=load_tool_configs,
            )
        )
        # Built-in lifecycle controls are authoritative if a plugin happened to
        # claim one of their names before the database-backed service was ready.
        self.registry.remove_tools(set(CODING_CONTROL_TOOLS))
        init_coding_control_tools(self.registry, self.coding_tasks)
        await self.coding_tasks.start()
        log.info("Durable coding tasks enabled with model %s", coding_model)

    async def _publish_coding_task(
        self,
        task: CodingTask,
        context: Any | None,
    ) -> None:
        """Project durable task state onto one edited status and one final reply."""

        # A worker completion, debounced milestone, and delivery retry can become
        # ready together. Serialize them per task and refresh the durable row so
        # a stale retry cannot send a second final response.
        async with self._root_lock(f"coding-delivery:{task.id}"):
            if self.coding_task_store is not None:
                refreshed = await self.coding_task_store.get_task(task.id)
                if refreshed is None:
                    return
                task = refreshed
            await self._publish_coding_task_locked(task, context)

    async def _publish_coding_task_locked(
        self,
        task: CodingTask,
        context: Any | None,
    ) -> None:

        target_id = task.thread_id or task.channel_id
        try:
            channel = self.bot.get_channel(int(target_id))
            if channel is None:
                channel = await self.bot.fetch_channel(int(target_id))
        except ValueError:
            await self._mark_coding_delivery_permanent_failure(task, "Invalid Discord channel id")
            log.warning("Invalid Discord channel for coding task %s", task.id)
            return
        except (discord.NotFound, discord.Forbidden) as exc:
            await self._mark_coding_delivery_permanent_failure(
                task,
                f"Discord channel is unavailable ({type(exc).__name__})",
            )
            log.warning("Discord channel is unavailable for coding task %s", task.id)
            return
        except discord.HTTPException:
            log.warning("Could not resolve Discord channel for coding task %s", task.id)
            return
        if not isinstance(channel, discord.TextChannel | discord.Thread):
            await self._mark_coding_delivery_permanent_failure(
                task,
                "Discord delivery target is not a text channel or thread",
            )
            return
        status_marker = self._coding_task_marker(task.id)
        terminal = task.status in {
            CodingTaskStatus.COMPLETED,
            CodingTaskStatus.FAILED,
            CodingTaskStatus.CANCELLED,
            CodingTaskStatus.TIMED_OUT,
        }
        if terminal and task.final_discord_message_id is not None:
            await self._delete_coding_status_message(channel, task, status_marker)
            return
        status_text = self._coding_status_text(task)
        status_text = await self._moderate_coding_text(task, status_text, status=True)
        status_text = self._coding_status_wire_text(status_text)[:2000]
        status_message: discord.Message | None = None
        if task.status_discord_message_id:
            try:
                status_message = await channel.fetch_message(int(task.status_discord_message_id))
                await status_message.edit(content=status_text)
            except ValueError, discord.HTTPException:
                status_message = None
        if status_message is None:
            status_message = await self._find_coding_delivery(channel, status_marker)
            if status_message is not None:
                with suppress(discord.HTTPException):
                    await status_message.edit(content=status_text)
                if self.conversation_store is not None and task.conversation_id is not None:
                    await self.conversation_store.map_message_context(
                        str(status_message.id), task.conversation_id, str(channel.id)
                    )
                if self.coding_task_store is not None:
                    await self.coding_task_store.mark_status_message(
                        task.id, str(status_message.id)
                    )
        if status_message is None:
            try:
                status_message = await channel.send(
                    status_text,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except (discord.NotFound, discord.Forbidden) as exc:
                await self._mark_coding_delivery_permanent_failure(
                    task,
                    f"Cannot send to Discord channel ({type(exc).__name__})",
                )
                log.warning("Cannot send coding status for task %s", task.id, exc_info=True)
                return
            except discord.HTTPException:
                log.warning("Could not send coding status for task %s", task.id, exc_info=True)
                return
            if self.conversation_store is not None and task.conversation_id is not None:
                await self.conversation_store.map_message_context(
                    str(status_message.id), task.conversation_id, str(channel.id)
                )
            if self.coding_task_store is not None:
                await self.coding_task_store.mark_status_message(task.id, str(status_message.id))

        if terminal:
            final_text = task.result_text.strip() or task.error_text.strip()
            if not final_text:
                final_text = f"Coding task `{task.id[:8]}` ended as **{task.status.value}**."
            final_text = await self._moderate_coding_text(task, final_text, status=False)
            final_text = self._coding_result_delivery_text(task.id, final_text)
            legacy_marker = f"coding-result:{task.id}"
            legacy_final = await self._find_coding_delivery(channel, legacy_marker)
            if legacy_final is not None:
                await self._persist_coding_final_messages(
                    task,
                    [legacy_final],
                    channel_id=str(channel.id),
                )
                if self.coding_task_store is not None:
                    await self.coding_task_store.mark_delivered(task.id, str(legacy_final.id))
                await self._delete_coding_status_message(
                    channel,
                    task,
                    status_marker,
                    message=status_message,
                )
                return
            delivery_channel = await self._coding_result_channel(task, channel, final_text)
            recovered_final = await self._find_coding_result_delivery(
                delivery_channel,
                final_text,
                legacy_marker=legacy_marker,
            )
            if recovered_final:
                await self._persist_coding_final_messages(
                    task,
                    recovered_final,
                    channel_id=str(delivery_channel.id),
                )
                if self.coding_task_store is not None:
                    await self.coding_task_store.mark_delivered(task.id, str(recovered_final[0].id))
                await self._delete_coding_status_message(
                    channel,
                    task,
                    status_marker,
                    message=status_message,
                )
                return
            delivery = task.checkpoint.get("delivery")
            durable_output_files = (
                delivery.get("output_files", []) if isinstance(delivery, dict) else []
            )
            durable_allowed_roots = (
                delivery.get("allowed_file_roots", []) if isinstance(delivery, dict) else []
            )
            output_files = (
                list(context.pending_output_files)
                if context is not None
                else [str(value) for value in durable_output_files]
            )
            allowed_roots = (
                list(context.pending_allowed_file_roots)
                if context is not None
                else [str(value) for value in durable_allowed_roots]
            )
            # The ordinary turn path loads every queued file into the moderation
            # request. Until the same file-content projection is available here,
            # fail closed by withholding background-task attachments when output
            # moderation is enabled.
            if self._should_moderate_coding_output(task):
                output_files = []
                allowed_roots = []
            async with self._root_lock(task.root_key):
                sent = await self.send_response(
                    delivery_channel,
                    final_text,
                    output_files=output_files,
                    allowed_file_roots=allowed_roots,
                    workspace_key=WorkspaceKey(task.workspace_key),
                )
                if not sent or bool(getattr(sent, "delivery_failed", False)):
                    if sent:
                        await self._delete_coding_messages(list(sent))
                    if bool(getattr(sent, "delivery_permanent", False)):
                        error = str(getattr(sent, "delivery_error", "Discord permission failure"))
                        await self._mark_coding_delivery_permanent_failure(task, error)
                    return
                first = sent[0]
                await self._persist_coding_final_messages(
                    task, list(sent), channel_id=str(delivery_channel.id)
                )
                if self.coding_task_store is not None:
                    await self.coding_task_store.mark_delivered(task.id, str(first.id))
                await self._delete_coding_status_message(
                    channel,
                    task,
                    status_marker,
                    message=status_message,
                )

    async def _mark_coding_delivery_permanent_failure(
        self,
        task: CodingTask,
        reason: str,
    ) -> None:
        if self.coding_task_store is not None:
            await self.coding_task_store.record_delivery_failure(
                task.id,
                reason,
                permanent=True,
            )

    async def _coding_result_channel(
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
                saved_channel = self.bot.get_channel(int(saved_thread_id))
                if saved_channel is None:
                    saved_channel = await self.bot.fetch_channel(int(saved_thread_id))
            except ValueError, discord.HTTPException:
                saved_channel = None
            if isinstance(saved_channel, discord.Thread):
                return saved_channel

        if (
            not isinstance(fallback, discord.TextChannel)
            or not self.settings.thread_handoff_enabled
            or self.thread_handoff is None
        ):
            return fallback

        try:
            trigger = await fallback.fetch_message(int(task.trigger_discord_message_id))
        except ValueError, discord.HTTPException:
            return fallback
        existing_thread = await self.threads._adopt_managed_handoff_thread(trigger)
        if existing_thread is not None:
            await self._save_coding_delivery_thread(task, existing_thread.id)
            return existing_thread
        if not self.settings.thread_auto_handoff_enabled:
            return fallback
        auto_cfg = (
            load_channel_auto_thread(task.channel_id)
            if self.threads._thread_handoff_creation_allowed(trigger)
            else None
        )
        if auto_cfg is None:
            return fallback
        request = build_auto_handoff_request(
            response_text=final_text,
            question_text=self._strip_message_invocation(
                trigger.content,
                bot_user=self.bot.user,
            ),
            bot_name=self.settings.bot_name,
            min_lines=auto_cfg.min_lines,
            min_chars=auto_cfg.min_chars,
            always=auto_cfg.always,
        )
        if request is None:
            return fallback
        thread = await self.threads._create_handoff_thread(
            trigger,
            request,
            task.conversation_id,
            creator_user_id=task.user_id,
        )
        if thread is None:
            return fallback
        await self._save_coding_delivery_thread(task, thread.id)
        await self.discord_gateway.add_status_reaction(trigger, THREAD_HANDOFF_REACTION)
        return thread

    async def _save_coding_delivery_thread(self, task: CodingTask, thread_id: int) -> None:
        if self.coding_task_store is None:
            return
        current = await self.coding_task_store.get_task(task.id)
        checkpoint = dict(current.checkpoint if current is not None else task.checkpoint)
        persisted_delivery = checkpoint.get("delivery")
        persisted_delivery = (
            dict(persisted_delivery) if isinstance(persisted_delivery, dict) else {}
        )
        persisted_delivery["thread_id"] = str(thread_id)
        checkpoint["delivery"] = persisted_delivery
        await self.coding_task_store.set_checkpoint(task.id, checkpoint)

    async def _find_coding_result_delivery(
        self,
        channel: discord.TextChannel | discord.Thread,
        expected_text: str,
        *,
        legacy_marker: str,
    ) -> list[discord.Message]:
        """Recover only a complete multi-message result after a process crash."""

        bot_user = self.bot.user
        expected_chunks = chunk_message(suppress_link_previews(expected_text))
        if bot_user is None or not expected_chunks:
            return []
        try:
            newest_first = [
                message
                async for message in channel.history(limit=max(100, len(expected_chunks) * 2))
            ]
        except discord.HTTPException:
            log.warning("Could not reconcile coding result %s", legacy_marker, exc_info=True)
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

        for message in newest_first:
            if message.author.id == bot_user.id and legacy_marker in message.content:
                return [message]
        return []

    async def _delete_coding_status_message(
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
            message = await self._find_coding_delivery(channel, marker)
        if message is None:
            return
        try:
            await message.delete()
        except discord.HTTPException:
            log.warning("Could not delete coding status for task %s", task.id, exc_info=True)

    @staticmethod
    async def _delete_coding_messages(messages: list[discord.Message]) -> None:
        for message in reversed(messages):
            try:
                await message.delete()
            except discord.HTTPException:
                log.warning("Could not clean up partial coding result", exc_info=True)

    async def _persist_coding_final_messages(
        self,
        task: CodingTask,
        messages: list[discord.Message],
        *,
        channel_id: str,
    ) -> None:
        """Persist and route a final before disabling marker-based retry."""

        if self.conversation_store is None or task.conversation_id is None:
            return
        records = [
            ChannelMessageRecord(
                discord_message_id=str(message.id),
                role="assistant",
                author_id=None,
                author_name=None,
                content=self._strip_coding_delivery_marker(
                    strip_chunk_marker(message.content),
                    task_ref=task.id[:8],
                ),
                source_created_at=message_source_timestamp(message),
            )
            for message in messages
        ]
        await self.conversation_store.save_channel_messages(
            task.conversation_id,
            records,
            context_channel_id=channel_id,
        )

    async def _find_coding_delivery(
        self,
        channel: discord.TextChannel | discord.Thread,
        marker: str,
    ) -> discord.Message | None:
        """Reconcile a send that may have committed just before a process crash."""

        bot_user = self.bot.user
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
    def _strip_coding_delivery_marker(text: str, *, task_ref: str | None = None) -> str:
        visible_result_marker = f"**Coding result `{task_ref}`**" if task_ref else None
        lines = [
            line
            for line in text.splitlines()
            if not line.startswith("-# coding-result:")
            and not line.startswith("-# coding-status:")
            and line != visible_result_marker
        ]
        return "\n".join(lines)

    @staticmethod
    def _coding_task_marker(task_id: str) -> str:
        return f"Coding task `{task_id[:8]}`"

    @staticmethod
    def _coding_result_marker(task_id: str) -> str:
        return f"Coding result `{task_id[:8]}`"

    @staticmethod
    def _coding_result_delivery_text(task_id: str, text: str) -> str:
        return f"**{KimiApplication._coding_result_marker(task_id)}**\n{text}"

    async def _moderate_coding_text(self, task: CodingTask, text: str, *, status: bool) -> str:
        service = self.moderation_service
        if not self._should_moderate_coding_output(task) or service is None:
            return text
        trust_tier = self._coding_task_trust_tier(task)
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
            return (
                f"**Coding task `{task.id[:8]}`: {task.status.value}**"
                if status
                else service.refusal_for(Direction.OUTPUT, error=True)
            )
        if not decision.blocked:
            return text
        if status:
            return f"**Coding task `{task.id[:8]}`: {task.status.value}**"
        return service.refusal_for(Direction.OUTPUT, error=decision.error)

    def _should_moderate_coding_output(self, task: CodingTask) -> bool:
        service = self.moderation_service
        if service is None or not service.enabled:
            return False
        exempt_tier = service.output_exempt_tier
        return exempt_tier is None or self._coding_task_trust_tier(task) < exempt_tier

    @staticmethod
    def _coding_task_trust_tier(task: CodingTask) -> TrustTier:
        raw = task.checkpoint.get("trust_tier")
        if isinstance(raw, str):
            with suppress(ValueError):
                return TrustTier(raw)
        # Tasks created before trust was recorded get the lowest usable tier.
        return TrustTier.MEMBER

    @staticmethod
    def _coding_status_wire_text(status_text: str) -> str:
        return suppress_link_previews(status_text)

    @staticmethod
    def _coding_status_text(task: CodingTask) -> str:
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
        task_marker = KimiApplication._coding_task_marker(task.id)
        lines = [
            f"{icons[task.status]} **{task_marker}: {task.status.value}**",
            task.objective[:500],
        ]
        if task.milestone:
            lines.append(f"-# {task.milestone[:500]}")
        visible_plan = [step for step in task.plan if step.get("status") != "completed"][:3]
        for step in visible_plan:
            marker = "▶" if step.get("status") == "in_progress" else "•"
            lines.append(f"{marker} {step.get('content', '')[:180]}")
        return "\n".join(lines)[:2000]

    async def _run_learn_turn(
        self,
        target: LearnTarget,
        interaction: discord.Interaction,
    ) -> str:
        """Bind the bot-name-derived teaching context menu to a scoped agent turn.

        The command module owns the Discord boundary and the staff check; this
        supplies the live provider, registry, and skills index. Exceptions
        propagate so the command surfaces one ephemeral failure message.
        """
        guild_id = str(interaction.guild_id) if interaction.guild_id else ""
        channel = interaction.channel
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        return await run_learn_turn(
            provider_manager=self.provider_manager,
            registry=self.tools.registry,
            target=target,
            user_id=str(interaction.user.id),
            user_name=interaction.user.display_name,
            guild_id=guild_id,
            guild_name=interaction.guild.name if interaction.guild else "",
            channel_id=str(interaction.channel_id) if interaction.channel_id else "",
            channel_name=getattr(channel, "name", "") or "",
            skills_index=self.skills_index_cache.index(guild_id or None),
            bot_name=self.settings.bot_name,
            platform_member=member,
            llm_semaphore=self.llm_semaphore,
        )

    async def _resume_pending_privacy_deletions(
        self,
        *,
        auto_retain_watermarks: AutoRetainStore,
    ) -> None:
        """Replay durable requests before exposing context or starting writers."""

        assert self.conversation_store is not None
        assert self.preference_store is not None
        assert self.privacy_deletion_store is not None
        if self.user_memory_bank_state_store is None:
            # Keep the replay helper usable in recovery/tests that compose the
            # stores directly instead of going through _first_init_core.
            self.user_memory_bank_state_store = UserMemoryBankStateStore(self.database)

        pending = await self.privacy_deletion_store.list_pending()
        if not pending:
            return

        # Tombstone every affected user before starting the first worker. This is
        # redundant while context_manager is still hidden, but protects the
        # ordering if startup composition changes later.
        for request in pending:
            await self.privacy_barrier.mark_deletion_pending(request.user_id)

        failed: list[str] = []
        for request in pending:
            try:
                outcome = await run_privacy_deletion(
                    scope=request.scope,
                    user_id=request.user_id,
                    conversation_store=self.conversation_store,
                    preference_store=self.preference_store,
                    memory_client=self.memory_manager.client,
                    auto_retain_watermarks=auto_retain_watermarks,
                    workspace_manager=self.tools.workspace_manager,
                    workspace_locks=self.tools.workspace_locks,
                    privacy_barrier=self.privacy_barrier,
                    deletion_request_store=self.privacy_deletion_store,
                    pending_request=request,
                    memory_bank_state_store=self.user_memory_bank_state_store,
                    conversation_turn_lock=self._lock_user_conversation_turns,
                    browser_data_store=self.tools.browser_service,
                )
            except Exception:
                log.exception(
                    "Startup privacy deletion replay failed for %s",
                    request.user_id,
                )
                failed.append(request.user_id)
                continue
            if not outcome.ok:
                log.error(
                    "Startup privacy deletion remains incomplete for %s: %s",
                    request.user_id,
                    " ".join(outcome.lines),
                )
                failed.append(request.user_id)

        remaining = await self.privacy_deletion_store.list_pending()
        if failed or remaining:
            affected = sorted({request.user_id for request in remaining} | set(failed))
            log.error(
                "Pending privacy deletion could not be completed at startup for "
                "%d user(s); their activity remains paused while unaffected users "
                "continue normally: %s",
                len(affected),
                ", ".join(affected),
            )

    async def on_guild_join(self, guild: discord.Guild) -> None:
        if guild.id in self.active_guilds():
            log.info("Joined active guild %s (%s)", guild.id, getattr(guild, "name", "?"))
            return
        log.warning(
            "Joined inactive guild %s (%s); staying connected but ignoring "
            "guild messages and commands until activation changes",
            guild.id,
            getattr(guild, "name", "?"),
        )

    def _strip_message_invocation(
        self,
        content: str,
        *,
        bot_user: discord.ClientUser | None,
    ) -> str:
        return strip_mention(
            content,
            bot_user=bot_user,
            bot_name=self.settings.bot_name,
        )

    def _is_stop_message(self, message: discord.Message) -> bool:
        text = self._strip_message_invocation(message.content, bot_user=self.bot.user)
        return text.strip().casefold() in {"stop", "cancel", "abort"}

    async def _handle_stop_message(self, message: discord.Message) -> None:
        await self.discord_gateway.add_status_reaction(message, "🛑")
        resolved = await self.resolve_conversation_for_message(message, allow_new_root=False)
        root_key = resolved.key if resolved is not None else None
        summary = await self._cancel_user_work(
            user_id=str(message.author.id),
            channel_id=str(message.channel.id),
            root_key=root_key,
            all_work=False,
        )
        await self.send_response(message.channel, summary, reference=message)

    async def _handle_stop_interaction(
        self,
        interaction: discord.Interaction,
        all_work: bool,
        task_id: str | None,
    ) -> str:
        user_id = str(interaction.user.id)
        channel_id = str(interaction.channel_id or "")
        if task_id is not None:
            if self.coding_task_store is None or self.coding_tasks is None:
                return "Coding tasks are not enabled."
            task = await self.coding_task_store.get_task(task_id)
            guild_id = str(interaction.guild_id) if interaction.guild_id else None
            member = interaction.user if isinstance(interaction.user, discord.Member) else None
            tier = self.trust_resolver.resolve(member, user_id, guild_id)
            same_guild_staff = (
                task is not None
                and tier >= TrustTier.STAFF
                and task.guild_id is not None
                and task.guild_id == guild_id
            )
            globally_configured_staff = user_id in self.settings.staff_ids
            if task is None or (
                task.user_id != user_id and not same_guild_staff and not globally_configured_staff
            ):
                return "That coding task was not found."
            await self.coding_tasks.cancel_task(task.id, reason="Stopped with /stop")
            cleanup_complete = await self.coding_tasks.cleanup_complete(task.id)
            cleanup = (
                "Cleanup is complete."
                if cleanup_complete
                else "Cleanup is still finishing in the background."
            )
            return (
                f"Stopped coding task `{task.id[:8]}`. {cleanup} "
                "Partial workspace changes were kept."
            )
        return await self._cancel_user_work(
            user_id=user_id,
            channel_id=channel_id,
            root_key=None,
            all_work=all_work,
        )

    async def _cancel_user_work(
        self,
        *,
        user_id: str,
        channel_id: str,
        root_key: str | None,
        all_work: bool,
    ) -> str:
        foreground_count, foreground_clean = await self.active_operations.cancel(
            user_id=user_id,
            root_key=root_key,
            channel_id=channel_id,
            all_operations=all_work,
            wait_seconds=self.settings.coding_stop_cleanup_wait_seconds,
        )
        coding_ids: list[str] = []
        coding_clean = True
        if self.coding_tasks is not None:
            coding_ids, coding_clean = await self.coding_tasks.cancel_for_scope(
                user_id=user_id,
                root_key=root_key,
                channel_id=channel_id,
                all_tasks=all_work,
            )
        total = foreground_count + len(coding_ids)
        if total == 0:
            return "I couldn't find active work to stop here."
        parts: list[str] = []
        if foreground_count:
            parts.append(f"{foreground_count} active response(s)")
        if coding_ids:
            labels = ", ".join(f"`{task_id[:8]}`" for task_id in coding_ids)
            parts.append(f"coding task(s) {labels}")
        cleanup = (
            "Cleanup is complete."
            if foreground_clean and coding_clean
            else "Cleanup is still finishing in the background."
        )
        return f"Stopped {' and '.join(parts)}. {cleanup} Partial file changes were kept."

    async def on_message(self, message: discord.Message) -> None:
        if self.context_manager is None:
            return
        active_guilds = self.active_guilds()
        if not is_eligible_to_respond(
            message,
            bot_user=self.bot.user,
            allowed_channels=self.settings.allowed_channels or None,
            allowed_guilds=active_guilds,
        ):
            return
        if isinstance(message.channel, discord.DMChannel):
            return

        # Pure routing check before taking a lease; messages the bot will ignore
        # have no state to coordinate with /privacy.
        if not self._should_respond(message, active_guilds=active_guilds):
            return

        # Hard block gate precedes reactions, transcript writes, every lock or
        # privacy lease, tools, and provider calls.
        if self.blocked_user_store is not None:
            blocked = await self.blocked_user_store.is_blocked(str(message.author.id))
            if blocked:
                log.info("Ignoring blocked user %s", message.author.id)
                return

        # Cancellation has its own lane before admission and the response lock;
        # otherwise a STOP message could queue behind the work it needs to end.
        if self._is_stop_message(message):
            await self._handle_stop_message(message)
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
        admission = await self.turn_admission.try_acquire(str(message.author.id))
        if admission.lease is None:
            log.info(
                "Rejecting turn from user %s at admission boundary: %s",
                message.author.id,
                admission.rejection,
            )
            await self.send_response(
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
            async with admission.lease:
                async with self.privacy_barrier.activity(str(message.author.id)):
                    await self._on_message_for_user(message)
        except PrivacyDeletionPendingError:
            log.info(
                "Ignoring user %s while their privacy deletion remains pending",
                message.author.id,
            )
        except asyncio.CancelledError:
            if self._closed:
                raise
            log.info("Stopped active response for user %s", message.author.id)

    async def _on_message_for_user(self, message: discord.Message) -> None:
        # First-interaction privacy gate. Sits before conversation resolution, the
        # lock, and the model turn, so an un-consented message never reaches the
        # provider or SQLite (resolve_conversation_for_message persists a
        # conversations row). On accept, the gate re-dispatches this message
        # through on_message.
        if self.consent_gate is not None and await self.consent_gate.maybe_prompt(message):
            return

        resolved = await self.resolve_conversation_for_message(
            message,
            allow_new_root=True,
        )
        if resolved is None:
            return

        lock_key = self.response_lock_key(message, resolved_conversation=resolved)
        # Ack before acquiring the lock so a continuation that queues behind an
        # in-flight turn on the same root (rapid replies / handoff-thread bursts)
        # shows ⏳ immediately instead of waiting silently for the lock.
        await self.discord_gateway.add_status_reaction(message, "⏳")
        async with self._root_lock(lock_key):
            # Re-check now that we hold the root lock. An earlier turn on this
            # root may have paused the thread while this message queued behind
            # it, and a paused thread must be neither answered nor transcribed
            # (docs/thread-handoff.md). The pre-lock check was made against the
            # old mode.
            # Re-read the cheap activation snapshot after waiting for the root
            # lock so an operator deactivation stops queued work immediately.
            if not self._should_respond(message):
                await self.discord_gateway.remove_status_reaction(message, "⏳")
                return
            try:
                result = await self.handle_message(
                    message,
                    lock_acquired=True,
                    resolved_conversation=resolved,
                )
                await self.discord_gateway.remove_status_reaction(message, "⏳")
                if result is None:
                    # No turn ran: a bare @mention with no text/attachment to act
                    # on. Acknowledge the ping with a wave rather than leaving the
                    # user with no signal; not ✅, since no reply was sent.
                    await self.discord_gateway.add_status_reaction(message, "👋")
                elif result.blocked_by_moderation:
                    await self.discord_gateway.add_status_reaction(message, "🚫")
                elif result.delivery_failed:
                    await self.discord_gateway.add_status_reaction(message, "❌")
                else:
                    await self.discord_gateway.add_status_reaction(message, "✅")
            except Exception:
                log.exception("Error handling message %s", message.id)
                await self.discord_gateway.remove_status_reaction(message, "⏳")
                await self.discord_gateway.add_status_reaction(message, "❌")

    async def handle_message(
        self,
        message: discord.Message,
        *,
        lock_acquired: bool = False,
        resolved_conversation: ResolvedConversation | None = None,
    ) -> TurnResult | None:
        assert self.context_manager is not None

        target_channel: discord.abc.Messageable = message.channel
        context_channel_id = str(message.channel.id)
        context_thread_id = (
            str(message.channel.id) if isinstance(message.channel, discord.Thread) else None
        )
        context_channel_name = getattr(message.channel, "name", "DM")
        if resolved_conversation is None:
            resolved_conversation = await self.resolve_conversation_for_message(
                message,
                allow_new_root=True,
            )
        if resolved_conversation is None:
            return None
        conversation_key = resolved_conversation.key

        member = message.author if isinstance(message.author, discord.Member) else None
        user_id = str(message.author.id)
        user_name = message.author.display_name

        guild_id = str(message.guild.id) if message.guild else None
        guild_name = message.guild.name if message.guild else ""

        trust_tier = self.trust_resolver.resolve(member, user_id, guild_id)

        conv_id = resolved_conversation.db_conversation_id
        if (
            self.conversation_store is not None
            and conv_id is not None
            and not await self.conversation_store.touch(conv_id)
        ):
            # A retention sweep may have won the race immediately before the
            # touch. Recreate the logical root rather than carrying a dead FK
            # through preparation and failing at transcript persistence.
            conv_id = None
        if self.conversation_store is not None and conv_id is None:
            conv_id = await self.conversation_store.get_or_create(
                conversation_key,
                context_channel_name,
                guild_id=guild_id,
                channel_id=context_channel_id,
                thread_id=context_thread_id,
                root_discord_message_id=str(message.id),
                owner_user_id=resolved_conversation.owner_user_id,
                access_scope=resolved_conversation.access_scope,
            )

        turn_result: TurnResult | None = None
        coding_handoff_task_id: str | None = None
        coding_handoff_prepared = False
        coding_handoff_finalized = False
        original_target_channel = target_channel
        mapped_building_ids: set[str] = set()

        async def map_building_message(message_id: int) -> None:
            if self.conversation_store is None or conv_id is None:
                return
            mid = str(message_id)
            if mid in mapped_building_ids:
                return
            try:
                await self.conversation_store.map_message_context(
                    mid,
                    conv_id,
                    context_channel_id,
                )
            except Exception:
                log.debug("Could not map Discord activity log route", exc_info=True)
                return
            mapped_building_ids.add(mid)

        activity_reporter = DiscordActivityReporter(
            target_channel,
            reference=message,
            on_committed_message=map_building_message,
        )

        async def persist_prepared_user_message(
            source: TurnPreparationInput,
            turn: TurnRequest,
        ) -> None:
            if self.conversation_store is None or conv_id is None:
                return
            content_parts = [
                ContentPart.from_text(turn.content),
                *list(turn.input_parts),
            ]
            await self.conversation_store.save_channel_messages(
                conv_id,
                [
                    ChannelMessageRecord(
                        discord_message_id=str(source.source_message.id),
                        role="user",
                        author_id=user_id,
                        author_name=sanitize_author_name(user_name),
                        content=turn.content,
                        source_created_at=message_source_timestamp(source.source_message),
                        content_parts=content_parts,
                    )
                ],
                context_channel_id=context_channel_id,
            )

        async def count_user_prior_messages(
            user_id: str, exclude_discord_message_id: str | None, limit: int
        ) -> int:
            assert self.conversation_store is not None
            return await self.conversation_store.count_user_messages(
                user_id, exclude_discord_message_id=exclude_discord_message_id, limit=limit
            )

        turn_input = TurnPreparationInput(
            raw_content=clean_message_text(message.content),
            source_message=message,
            bot_user=self.bot.user,
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
            referenced_message_id=self.referenced_message_id(message),
            conversation_owner_user_id=resolved_conversation.owner_user_id,
            conversation_access_scope=resolved_conversation.access_scope,
            allow_bot_authored_reply_context=(
                resolved_conversation.allow_bot_authored_reply_context
            ),
        )
        turn_stop_event = asyncio.Event()
        turn_dependencies = await build_turn_dependencies(
            self,
            turn_input,
            context_manager=self.context_manager,
            registry=self.registry,
            preference_store=self.preference_store,
            usage_store=self._usage_store(),
            hooks=_turn_entry_hooks(),
            command_template=None,
            collect_reply_context_func=collect_reply_context,
            collect_turn_attachments_func=collect_turn_attachments,
            strip_mention_func=self._strip_message_invocation,
            persist_prepared_user_message=persist_prepared_user_message,
            count_user_prior_messages=(
                count_user_prior_messages if self.conversation_store is not None else None
            ),
            activity_reporter=activity_reporter,
            extra_blocked_tools=self.threads._thread_state_blocked_tools(message),
        )

        @asynccontextmanager
        async def foreground_child_activity(activity_user_id: str) -> AsyncIterator[None]:
            # Mutable work is shielded from root cancellation so thread-backed
            # operations can finish safely. Register each child independently so
            # STOP waits for real completion and can still find cleanup after the
            # foreground response root has exited.
            async with self.active_operations.register(
                user_id=activity_user_id,
                root_key=conversation_key,
                channel_id=context_channel_id,
                # A child may be awaiting asyncio.to_thread; cancelling its
                # wrapper would unregister it while the OS thread kept mutating.
                # Stop the response root, retain the child as cleanup-pending,
                # and let its privacy/workspace lease prove actual completion.
                cancel_on_stop=False,
                stop_event=turn_stop_event,
            ):
                async with self.privacy_barrier.activity(activity_user_id):
                    yield

        turn_dependencies = replace(
            turn_dependencies,
            user_activity=foreground_child_activity,
            stop_event=turn_stop_event,
        )
        turn_config = build_turn_preparation_config(
            self.settings,
            recent_image_lookback=self.settings.recent_image_lookback,
            new_user_onboarding_turns=self.settings.new_user_onboarding_turns,
        )

        async def run_locked() -> None:
            nonlocal turn_result
            turn_result = await handle_turn(
                turn_input,
                dependencies=turn_dependencies,
                preparation_config=turn_config,
                execution_config=TurnExecutionConfig(
                    max_iterations=self.settings.react_max_iterations,
                    max_tokens=self.settings.react_max_tokens,
                    temperature=self.settings.react_temperature,
                    bot_name=self.settings.bot_name,
                    timeout_seconds=self.settings.react_turn_timeout_seconds,
                    thread_handoff_suggest_after_tool_calls=(
                        self.settings.thread_handoff_suggest_after_tool_calls
                    ),
                ),
            )

        active_registration = self.active_operations.register(
            user_id=user_id,
            root_key=conversation_key,
            channel_id=context_channel_id,
            stop_event=turn_stop_event,
        )
        await active_registration.__aenter__()
        try:
            source_binding = self.discord_gateway.bind_turn_source(
                conversation_key,
                str(message.id),
                message,
            )
            try:
                if lock_acquired:
                    await run_locked()
                else:
                    async with self._root_lock(
                        self.response_lock_key(
                            message,
                            resolved_conversation=resolved_conversation,
                        )
                    ):
                        await run_locked()
            finally:
                self.discord_gateway.unbind_turn_source(source_binding)

            if turn_result is None:
                return None

            if (
                turn_result.terminal_handoff is not None
                and turn_result.terminal_handoff.reason == "coding_task"
            ):
                coding_handoff_task_id = turn_result.terminal_handoff.task_id

            reply_reference: discord.Message | None = message
            # The model may have requested a thread itself; otherwise the
            # operator-gated backstop synthesizes one for an over-long reply in an
            # opted-in channel. Either way the same creation/enrollment path runs.
            thread_request = turn_result.thread_request
            if turn_result.blocked_by_moderation:
                # A blocked reply gets no thread at all. Same-channel that was
                # merely untidy; a cross-channel one would post an anchor in a
                # channel nobody in this conversation is even looking at.
                thread_request = None
            elif (
                thread_request is None
                and self.settings.thread_auto_handoff_enabled
                and self.settings.thread_handoff_enabled
                and self.thread_handoff is not None
                and not isinstance(message.channel, discord.Thread)
            ):
                channel_id = str(message.channel.id)
                # Automatic handoff observes the exact same creation policy as
                # move_to_thread. In particular, a global/guild/channel deny
                # cannot be bypassed by auto_thread_* thresholds.
                auto_cfg = (
                    load_channel_auto_thread(channel_id)
                    if self.threads._thread_handoff_creation_allowed(message)
                    else None
                )
                if auto_cfg is not None:
                    auto_request = build_auto_handoff_request(
                        response_text=turn_result.response_text,
                        question_text=self._strip_message_invocation(
                            message.content,
                            bot_user=self.bot.user,
                        ),
                        bot_name=self.settings.bot_name,
                        min_lines=auto_cfg.min_lines,
                        min_chars=auto_cfg.min_chars,
                        always=auto_cfg.always,
                    )
                    if auto_request is not None:
                        thread_request = auto_request

            handoff_thread: discord.Thread | None = None
            cross_channel = False
            if thread_request is not None:
                handoff_thread = await self.threads._create_handoff_thread(
                    message,
                    thread_request,
                    conv_id,
                )
                if handoff_thread is not None:
                    # The reply becomes the thread's first message; a cross-channel
                    # reply reference is not possible, and Discord's own "started a
                    # thread" notice anchors it in the parent channel.
                    cross_channel = thread_request.target_channel_id is not None
                    target_channel = handoff_thread
                    reply_reference = None

            if coding_handoff_task_id is not None and self.coding_tasks is not None:
                if isinstance(target_channel, discord.Thread):
                    parent_id = getattr(target_channel, "parent_id", None)
                    route_channel_id = (
                        str(parent_id) if parent_id is not None else context_channel_id
                    )
                    route_thread_id = str(target_channel.id)
                else:
                    route_channel_id = str(getattr(target_channel, "id", context_channel_id))
                    route_thread_id = None
                coding_handoff_prepared = await self.coding_tasks.prepare_handoff(
                    coding_handoff_task_id,
                    channel_id=route_channel_id,
                    thread_id=route_thread_id,
                )
                if not coding_handoff_prepared:
                    turn_result = replace(
                        turn_result,
                        response_text=(
                            f"Coding task `{coding_handoff_task_id[:8]}` was cancelled "
                            "before it started."
                        ),
                    )

            if handoff_thread is not None:
                await self.discord_gateway.add_status_reaction(message, THREAD_HANDOFF_REACTION)

            sent_messages = await self.send_response(
                target_channel,
                turn_result.response_text,
                reference=reply_reference,
                output_files=list(turn_result.output_files),
                allowed_file_roots=list(turn_result.allowed_file_roots),
                embed=turn_result.embed,
                mention_author=True,
                workspace_key=turn_result.workspace_key,
            )

            initial_handoff_delivery_failed = bool(
                not sent_messages or getattr(sent_messages, "delivery_failed", False)
            )
            if (
                coding_handoff_task_id is not None
                and coding_handoff_prepared
                and handoff_thread is not None
                and initial_handoff_delivery_failed
                and self.coding_tasks is not None
            ):
                task = (
                    await self.coding_task_store.get_task(coding_handoff_task_id)
                    if self.coding_task_store is not None
                    else None
                )
                if task is not None:
                    await self._delete_coding_status_message(
                        handoff_thread,
                        task,
                        self._coding_task_marker(task.id),
                    )
                if self.thread_handoff is not None:
                    await self.thread_handoff.prune(handoff_thread.id)
                if cross_channel:
                    await self.threads._discard_cross_channel_thread(handoff_thread)

                target_channel = original_target_channel
                reply_reference = message
                handoff_thread = None
                cross_channel = False
                fallback_channel_id = str(
                    getattr(original_target_channel, "id", context_channel_id)
                )
                fallback_thread_id = (
                    str(original_target_channel.id)
                    if isinstance(original_target_channel, discord.Thread)
                    else None
                )
                coding_handoff_prepared = await self.coding_tasks.prepare_handoff(
                    coding_handoff_task_id,
                    channel_id=fallback_channel_id,
                    thread_id=fallback_thread_id,
                )
                if coding_handoff_prepared:
                    sent_messages = await self.send_response(
                        target_channel,
                        turn_result.response_text,
                        reference=reply_reference,
                        output_files=list(turn_result.output_files),
                        allowed_file_roots=list(turn_result.allowed_file_roots),
                        embed=turn_result.embed,
                        mention_author=True,
                        workspace_key=turn_result.workspace_key,
                    )

            if (
                coding_handoff_task_id is not None
                and coding_handoff_prepared
                and self.coding_tasks is not None
            ):
                coding_handoff_finalized = await self.coding_tasks.release_handoff(
                    coding_handoff_task_id
                )

            # "The turn meant to say something." Both the thread cleanup below and
            # the ❌ reaction further down key on it: an embed-only or files-only
            # reply is just as real as a text one, so a failed send has to prune
            # and tidy up for those too, not only when there was text.
            expected_delivery = bool(
                turn_result.response_text.strip()
                or turn_result.embed is not None
                or turn_result.output_files
            )

            if (
                self.thread_handoff is not None
                and isinstance(target_channel, discord.Thread)
                and self.thread_handoff.is_managed(target_channel.id)
                and not sent_messages
                and expected_delivery
            ):
                # send_response logs and swallows per-chunk HTTP failures, so a
                # deleted/locked managed thread surfaces as nothing sent: revert
                # it to mention-only rather than failing silently forever.
                await self.thread_handoff.prune(target_channel.id)
                if cross_channel:
                    # Nothing landed in it, so nothing should be left over in the
                    # target channel advertising that it exists.
                    await self.threads._discard_cross_channel_thread(target_channel)
            elif cross_channel and handoff_thread is not None and sent_messages:
                await self.threads._send_cross_channel_pointer(message, handoff_thread)

            if (
                self.conversation_store is not None
                and conv_id is not None
                and sent_messages
                and not turn_result.blocked_by_moderation
            ):
                # An embed-only reply has empty content; persist a text summary so the
                # embed stays visible in the transcript that seeds later turns.
                embed_summary = (
                    embed_transcript_summary(turn_result.embed)
                    if turn_result.embed is not None
                    else ""
                )
                reply_records = []
                for index, sent in enumerate(sent_messages):
                    content = strip_chunk_marker(sent.content)
                    if index == 0 and not content and embed_summary:
                        content = embed_summary
                    reply_records.append(
                        ChannelMessageRecord(
                            discord_message_id=str(sent.id),
                            role="assistant",
                            author_id=None,
                            author_name=None,
                            content=content,
                            source_created_at=message_source_timestamp(sent),
                        )
                    )
                # Map replies under the channel they actually landed in: after a
                # thread handoff that is the new thread, and the reply-continuation
                # lookup filters message_contexts by the incoming message's channel.
                sent_channel = getattr(sent_messages[0], "channel", None)
                persist_channel_id = (
                    str(sent_channel.id) if sent_channel is not None else context_channel_id
                )
                await self.conversation_store.save_channel_messages(
                    conv_id,
                    reply_records,
                    context_channel_id=persist_channel_id,
                )
            if turn_result.thread_close_request is not None:
                await self.threads._close_handoff_thread(
                    target_channel,
                    turn_result.thread_close_request,
                )
            partial_delivery_failed = bool(getattr(sent_messages, "delivery_failed", False))
            if (
                expected_delivery
                and (not sent_messages or partial_delivery_failed)
                and not turn_result.blocked_by_moderation
            ):
                # send_response swallows per-chunk HTTP failures; surface a total
                # delivery failure so on_message reacts ❌ instead of ✅.
                return replace(turn_result, delivery_failed=True)
            return turn_result
        finally:
            if (
                coding_handoff_task_id is not None
                and not coding_handoff_finalized
                and self.coding_tasks is not None
            ):
                try:
                    coding_handoff_finalized = await self.coding_tasks.finalize_handoff(
                        coding_handoff_task_id
                    )
                except Exception:
                    log.exception(
                        "Could not release coding task %s after foreground routing failed",
                        coding_handoff_task_id,
                    )
            await active_registration.__aexit__(None, None, None)
            await activity_reporter.finish()
            kept_id = activity_reporter.committed_message_id
            if kept_id is not None:
                await map_building_message(kept_id)

    async def send_response(
        self,
        channel: discord.abc.Messageable,
        content: str,
        reference: discord.Message | None = None,
        output_files: list[str] | None = None,
        allowed_file_roots: list[str | Path] | None = None,
        embed: EmbedSpec | None = None,
        mention_author: bool = False,
        workspace_key: WorkspaceKey | None = None,
    ) -> list[discord.Message]:
        async def send() -> list[discord.Message]:
            return await self.discord_gateway.send_response(
                channel,
                content,
                reference=reference,
                output_files=output_files,
                allowed_file_roots=allowed_file_roots,
                embed=embed,
                mention_author=mention_author,
            )

        # Only local attachments need their workspace protected until Discord
        # has consumed them. A text-only response must remain deliverable while
        # a durable coding worker owns the workspace writer lease.
        if workspace_key and output_files:
            async with self.tools.workspace_locks.activity(workspace_key):
                return await send()
        return await send()

    async def resolve_conversation_for_message(
        self,
        message: discord.Message,
        *,
        allow_new_root: bool,
    ) -> ResolvedConversation | None:
        return await resolve_conversation_for_message(
            message,
            allow_new_root=allow_new_root,
            conversation_store=self.conversation_store,
            thread_handoff=self.thread_handoff,
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
            bot_user=self.bot.user,
            bot_name=self.settings.bot_name,
            responds_without_mention=self.responds_without_mention,
            allowed_channels=self.settings.allowed_channels or None,
            allowed_guilds=active_guilds if active_guilds is not None else self.active_guilds(),
        )

    def responds_without_mention(self, thread_id: int) -> bool:
        """Whether a managed thread currently answers without being mentioned.

        False when handoff is disabled, when the thread is not one the bot
        created, and when the thread is paused. A paused thread stays mapped to
        its root but falls back to the ordinary channel gates.
        """
        manager = self.thread_handoff
        return manager is not None and manager.is_auto_responding(thread_id)

    def referenced_message_id(self, message: discord.Message) -> str | None:
        return referenced_message_id(message)

    def conversation_key_for_message(self, message: discord.Message) -> str:
        return conversation_key_for_message(message)

    def response_lock_key(
        self,
        message: discord.Message,
        *,
        resolved_conversation: ResolvedConversation | None = None,
    ) -> str:
        return response_lock_key(
            message,
            resolved_conversation=resolved_conversation,
        )

    def _resolved_chat_model_name(self, scope: Scope, *, images: bool = False) -> str:
        return chat_model_name_for_scope(self.provider_manager, scope, images=images)

    def _usage_store(self) -> UsageStore:
        if self.usage_store is None:
            self.usage_store = UsageStore(self.database)
        return self.usage_store

    def _model_log_label(self, role: str) -> str:
        model_config = getattr(self.provider_manager, "model_config", None)
        if model_config is None:
            provider = getattr(self.provider_manager, "main", None)
            return getattr(provider, "model", "?")
        model_name = model_config.model_name_for_role(role)
        entry = model_config.models[model_name]
        profile = model_config.profile_for_model(model_name)
        return f"{model_name}={profile.type}/{entry.model}"

    @asynccontextmanager
    async def _lock_user_conversation_turns(
        self,
        user_id: str,
    ) -> AsyncIterator[None]:
        """Drain every active root whose transcript a deletion will mutate."""

        store = self.conversation_store
        if store is None:
            yield
            return
        list_keys = getattr(store, "list_user_conversation_keys", None)
        if list_keys is None:
            yield
            return
        keys = await list_keys(user_id)
        async with AsyncExitStack() as stack:
            # Stable ordering prevents two simultaneous user deletions whose
            # shared roots overlap from deadlocking while they drain turns.
            for key in sorted(set(keys)):
                await stack.enter_async_context(self._root_lock(key))
            yield

    @asynccontextmanager
    async def _root_lock(self, key: str) -> AsyncIterator[None]:
        # Per-root serialization lock with refcounted eviction so context_locks
        # does not grow unbounded across fresh-mention roots (each root key
        # embeds a unique trigger snowflake). The get-or-create + refcount bump
        # run synchronously before the first await (the lock acquire), so a
        # concurrent acquirer for the same root always sees the same Lock object;
        # the entry is evicted only once the last holder/waiter releases.
        lock = self.context_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self.context_locks[key] = lock
        self._lock_refcounts[key] = self._lock_refcounts.get(key, 0) + 1
        try:
            async with lock:
                yield
        finally:
            count = self._lock_refcounts[key] - 1
            if count <= 0:
                self._lock_refcounts.pop(key, None)
                self.context_locks.pop(key, None)
            else:
                self._lock_refcounts[key] = count


# Chat-command wiring helpers. Module-level (not methods) so tests can drive
# them with a plain attribute-bag stand-in for the application.


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


def build_app(settings: Settings) -> KimiApplication:
    # Layer the operator's settings file over the environment, before any
    # of it is captured into the config objects built below. The file wins over
    # .env on purpose: it is the operator's deliberate edit, so environment
    # precedence would make that edit silently do nothing
    # (config/operator_settings.py).
    apply_operator_settings(settings)
    inherited_settings = settings.model_dump(mode="python")
    inherited_settings_values = MappingProxyType(
        {
            field: tuple(value) if isinstance(value, list) else value
            for field, value in settings_values(settings).items()
        }
    )
    control_store: ControlPlaneStore | None = None
    if settings.control_plane_enabled:
        if not settings.owner_user_id.strip():
            raise RuntimeError("CONTROL_PLANE_ENABLED requires OWNER_USER_ID")
        control_store = ControlPlaneStore(
            settings.control_plane_dir,
            master_key=settings.control_plane_key.get_secret_value(),
            base_config_dir=settings.config_dir,
        )
        apply_managed_settings(settings, control_store)
        settings.config_dir = str(control_store.effective_config_dir(Path(settings.config_dir)))
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
    provider_manager = (
        build_provider_manager(settings)
        if control_store is None
        else build_provider_manager(
            settings,
            model_config_path=managed_models_path(settings, control_store),
        )
    )
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
    restart = (
        RestartCoordinator(enabled=settings.control_plane_auto_restart)
        if control_store is not None
        else None
    )
    proposal_service = (
        DurableProposalService(database, owner_user_id=settings.owner_user_id)
        if control_store is not None
        else None
    )
    configuration_service = (
        ManagedConfigurationService(
            proposals=proposal_service,
            store=control_store,
            settings=settings,
            inherited_settings=inherited_settings,
            restart=restart,
        )
        if proposal_service is not None and control_store is not None and restart is not None
        else None
    )
    application = KimiApplication(
        settings=settings,
        inherited_settings_values=inherited_settings_values,
        bot=bot,
        trust_resolver=trust_resolver,
        discord_gateway=gateway,
        provider_manager=provider_manager,
        memory_manager=memory_manager,
        moderation_service=moderation_service,
        learn_log=learn_log,
        proposal_service=proposal_service,
        configuration_service=configuration_service,
        restart_coordinator=restart,
        control_plane_store=control_store,
        tools=build_runtime_tools(
            settings,
            gateway,
            provider_manager,
            memory_manager,
            registry=registry,
            on_learn=learn_log.record,
            get_preference_store=lambda: application.preference_store,
            get_blocked_user_store=lambda: application.blocked_user_store,
            get_thread_handoff=lambda: application.thread_handoff,
            resolve_thread_target=lambda ctx, raw: application.threads.resolve_thread_target(
                ctx, raw
            ),
            can_manage_thread=lambda ctx, thread_id: application.threads.can_manage_thread(
                ctx, thread_id
            ),
        ),
        database=database,
    )
    if restart is not None:
        restart.bind(bot.close)
    if configuration_service is not None:
        configuration_service.bind_live_activation(application.activate_managed_config)
    bot._agent_application = application
    bot.event(application.on_ready)
    bot.event(application.on_disconnect)
    bot.event(application.on_message)
    bot.event(application.on_guild_join)
    return application
