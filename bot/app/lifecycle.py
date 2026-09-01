from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import discord
from discord.ext import commands
from kimi_agent_module_api import ModuleSpec
from kimi_agent_module_api.contracts import InteractionRouter
from pydantic import SecretStr

from agent.context import ContextManager
from app.admission import TurnAdmissionController
from app.cancellation import ActiveOperationRegistry
from app.coding_delivery import CodingTaskController
from app.command_sync import DiscordCommandSync, GuildCommandSyncPort
from app.consent import PrivacyConsentGate
from app.foreground_turn import ForegroundTurnRunner
from app.guild_activation import GuildActivationService
from app.memory import MemoryManager
from app.modules import ModuleManager, ModuleRuntimeBase, module_capabilities
from app.proposals import ConfigProposalService, ProposalHost, ROUTER_NAME
from app.providers import ProviderManager
from app.tools import RuntimeTools
from app.user_app_chat import UserAppChatController
from app.user_app_consent import UserAppConsentPrompter
from app.work_cancellation import WorkCancellationCoordinator
from commands.chat_cmd import register_user_app_chat_commands
from commands.learn_cmd import register_learn_command
from commands.memory_cmd import register_memory_command
from commands.models_cmd import register_models_command
from commands.moderation_cmd import register_moderation_command
from commands.modules_cmd import register_modules_command
from commands.privacy_cmd import (
    drain_confirmed_privacy_deletions,
    register_privacy_command,
    run_privacy_deletion,
)
from commands.stop_cmd import register_stop_command
from commands.usage_cmd import register_usage_command
from config.fragments.guild_config import (
    load_proposal_channel_id,
    proposal_channel_id_is_configured,
)
from config.settings import Settings
from discord_adapter.lifecycle import (
    attachment_orphan_sweeper,
    auto_retain_sweeper,
    sweep_attachment_orphans_once,
    transcript_retention_sweeper,
    video_session_sweeper,
    workspace_sweeper,
)
from discord_adapter.module_actions import DiscordActionsImpl, TrustLookupImpl
from discord_adapter.module_events import ModuleEventPublisher
from discord_adapter.module_interactions import InteractionRuntime
from memory.auto_retain import AutoRetainFlusher
from memory.banks import ensure_user_bank
from modules.events import EventBusImpl
from modules.guild_settings import GuildSettingsService
from modules.http import ModuleHttpRuntime
from modules.scheduler import DurableScheduler
from observability.events import emit_module_health, start_event_writer, stop_event_writer
from storage.auto_retain import AutoRetainStore
from storage.blocked_users import BlockedUserStore
from storage.coding_tasks import CodingTaskStore
from storage.conversations import ConversationStore
from storage.db import Database
from storage.image_distillations import ImageDistillationStore
from storage.memory_banks import UserMemoryBankStateStore
from storage.model_selection import ModelSelectionStore
from storage.module_commands import GuildCommandScopeStore
from storage.preferences import PreferenceStore
from storage.privacy import PrivacyDeletionRequestStore
from storage.provider_circuits import ProviderCircuitStore
from storage.usage import UsageStore
from storage.video_sessions import VideoSessionStore
from tools.learn import LearnTarget
from tools.user_memory import set_user_memory_preference_store
from trust.resolver import TrustResolver
from utils.asyncio import await_uncancellable
from utils.privacy_barrier import UserPrivacyBarrier

if TYPE_CHECKING:
    from moderation.service import ModerationService

log = logging.getLogger(__name__)


class ShutdownSignal(Protocol):
    @property
    def closed(self) -> bool: ...


class UserConversationLock(Protocol):
    def __call__(self, user_id: str, /) -> AbstractAsyncContextManager[None]: ...


class LearnTurn(Protocol):
    async def __call__(
        self,
        target: LearnTarget,
        interaction: discord.Interaction,
        /,
    ) -> str: ...


@dataclass(frozen=True, slots=True)
class AppRepositories:
    conversation_store: ConversationStore
    preference_store: PreferenceStore
    blocked_user_store: BlockedUserStore
    model_selection_store: ModelSelectionStore
    image_distillation_store: ImageDistillationStore
    usage_store: UsageStore
    video_session_store: VideoSessionStore
    coding_task_store: CodingTaskStore
    privacy_deletion_store: PrivacyDeletionRequestStore
    user_memory_bank_state_store: UserMemoryBankStateStore


@dataclass(frozen=True, slots=True)
class LifecycleCallbacks:
    gateway_interactions_ready: Callable[[], bool]
    active_guilds: Callable[[], set[int]]
    refresh_guild_activation: Callable[[int | None], Awaitable[None]]
    lock_user_conversations: UserConversationLock
    run_learn: LearnTurn
    is_user_blocked: Callable[[str], Awaitable[bool]]
    model_log_label: Callable[[str], str]


@dataclass(frozen=True, slots=True)
class LifecycleResources:
    settings: Settings
    bot: commands.Bot
    database: Database
    provider_manager: ProviderManager
    memory_manager: MemoryManager
    tools: RuntimeTools
    repositories: AppRepositories
    turn_admission: TurnAdmissionController
    active_operations: ActiveOperationRegistry
    consent_gate: PrivacyConsentGate
    privacy_barrier: UserPrivacyBarrier
    moderation_service: ModerationService | None
    guild_activation: GuildActivationService
    command_sync: DiscordCommandSync
    coding_tasks: CodingTaskController
    module_manager: ModuleManager
    trust_resolver: TrustResolver
    context_manager: ContextManager
    turn_runner: ForegroundTurnRunner
    user_app_consent: UserAppConsentPrompter
    user_app_chat: UserAppChatController
    work_cancellation: WorkCancellationCoordinator
    callbacks: LifecycleCallbacks


@dataclass(frozen=True, slots=True)
class LifecycleSnapshot:
    closed: bool
    close_complete: bool
    startup_error: Exception | None
    db_initialized: bool
    gateway_ready: bool
    workspace_sweeper_started: bool
    auto_retain_sweeper_started: bool
    transcript_retention_sweeper_started: bool
    video_session_sweeper_started: bool
    active_transcript_retention_days: int
    active_transcript_retention_sweep_interval_seconds: int | None
    guild_activation_refresh_task: asyncio.Task[None] | None
    auto_retain_task: asyncio.Task[Any] | None
    attachment_sweeper_task: asyncio.Task[Any] | None
    workspace_sweeper_task: asyncio.Task[Any] | None
    transcript_retention_task: asyncio.Task[Any] | None
    video_session_sweeper_task: asyncio.Task[Any] | None
    module_event_publisher: ModuleEventPublisher | None
    module_interaction_runtime: InteractionRuntime | None


def settings_secret_values(settings: Settings) -> tuple[str, ...]:
    values = (getattr(settings, field_name) for field_name in type(settings).model_fields)
    return tuple(
        secret
        for value in values
        if isinstance(value, SecretStr)
        if (secret := value.get_secret_value())
    )


class ApplicationLifecycle:
    def __init__(self, resources: LifecycleResources) -> None:
        self._resources = resources
        self._ready_init_lock = asyncio.Lock()
        self._startup_error: Exception | None = None
        self._db_initialized = False
        self._closed = False
        self._close_complete = asyncio.Event()
        self._gateway_ready = False
        self._workspace_sweeper_started = False
        self._auto_retain_sweeper_started = False
        self._transcript_retention_sweeper_started = False
        self._video_session_sweeper_started = False
        self._active_transcript_retention_days = 0
        self._active_transcript_retention_sweep_interval_seconds: int | None = None
        self._auto_retain_task: asyncio.Task[Any] | None = None
        self._attachment_sweeper_task: asyncio.Task[Any] | None = None
        self._workspace_sweeper_task: asyncio.Task[Any] | None = None
        self._transcript_retention_task: asyncio.Task[Any] | None = None
        self._video_session_sweeper_task: asyncio.Task[Any] | None = None
        self._module_event_publisher: ModuleEventPublisher | None = None
        self._module_interaction_runtime: InteractionRuntime | None = None
        self._proposal_service: ConfigProposalService | None = None
        self._thread_handoff: Any | None = None

    @property
    def resources(self) -> LifecycleResources:
        return self._resources

    @property
    def repositories(self) -> AppRepositories:
        return self._resources.repositories

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def gateway_ready(self) -> bool:
        return self._gateway_ready

    @property
    def db_initialized(self) -> bool:
        return self._db_initialized

    @property
    def startup_error(self) -> Exception | None:
        return self._startup_error

    @property
    def module_interaction_runtime(self) -> GuildCommandSyncPort | None:
        return self._module_interaction_runtime

    @property
    def thread_handoff(self) -> Any | None:
        return self._thread_handoff

    @property
    def proposal_service(self) -> ConfigProposalService | None:
        return self._proposal_service

    def snapshot(self) -> LifecycleSnapshot:
        return LifecycleSnapshot(
            closed=self._closed,
            close_complete=self._close_complete.is_set(),
            startup_error=self._startup_error,
            db_initialized=self._db_initialized,
            gateway_ready=self._gateway_ready,
            workspace_sweeper_started=self._workspace_sweeper_started,
            auto_retain_sweeper_started=self._auto_retain_sweeper_started,
            transcript_retention_sweeper_started=self._transcript_retention_sweeper_started,
            video_session_sweeper_started=self._video_session_sweeper_started,
            active_transcript_retention_days=self._active_transcript_retention_days,
            active_transcript_retention_sweep_interval_seconds=(
                self._active_transcript_retention_sweep_interval_seconds
            ),
            guild_activation_refresh_task=self._resources.guild_activation.refresh_task,
            auto_retain_task=self._auto_retain_task,
            attachment_sweeper_task=self._attachment_sweeper_task,
            workspace_sweeper_task=self._workspace_sweeper_task,
            transcript_retention_task=self._transcript_retention_task,
            video_session_sweeper_task=self._video_session_sweeper_task,
            module_event_publisher=self._module_event_publisher,
            module_interaction_runtime=self._module_interaction_runtime,
        )

    def interactions_ready(self) -> bool:
        return self._gateway_ready and self._startup_error is None and not self._closed

    def can_restore_gateway_readiness(self) -> bool:
        return self._db_initialized and self._startup_error is None and not self._closed

    async def ready(self) -> None:
        if self._closed:
            return
        command_sync = self._resources.command_sync
        async with command_sync.ready_cohort() as gateway_generation:
            self._gateway_ready = False
            try:
                self.log_ready_state()
                async with self._ready_init_lock:
                    if self._closed or self._startup_error is not None:
                        return
                    startup_succeeded = await self.initialize_ready()
                if not startup_succeeded:
                    await self._resources.bot.close()
                    return
                if self._closed:
                    return

                await command_sync.sync_for_ready(gateway_generation)
                if gateway_generation != command_sync.current_generation:
                    return

                async with self._ready_init_lock:
                    if self._closed:
                        return
                    await self.start_filesystem_sweepers()
                    if self._closed:
                        return
                    self.start_ready_background_tasks()
            finally:
                if gateway_generation == command_sync.current_generation:
                    self._gateway_ready = self.can_restore_gateway_readiness()

    async def disconnect(self) -> None:
        self._gateway_ready = False
        await self._resources.command_sync.disconnect()

    async def resume(self) -> None:
        self._gateway_ready = self.can_restore_gateway_readiness()
        await self._resources.command_sync.resume()

    async def close(self) -> None:
        if self._closed:
            await self._close_complete.wait()
            return
        # There is no await between this check and assignment, so competing
        # close calls cannot both become the teardown owner on one event loop.
        self._closed = True
        owner_task = asyncio.current_task()
        try:
            await await_uncancellable(self.finish_close(owner_task))
        finally:
            self._close_complete.set()

    async def finish_close(self, owner_task: asyncio.Task[Any] | None) -> None:
        await self.cancel_ready_events(exclude=owner_task)
        await self.close_resources()

    async def cancel_ready_events(self, *, exclude: asyncio.Task[Any] | None) -> None:
        await self._resources.command_sync.cancel_ready_events(exclude=exclude)

    async def drain_interactions(self) -> None:
        if self._module_interaction_runtime is not None:
            try:
                # Close the guild-sync gate synchronously inside drain() before
                # a stubborn global PUT can return and enter guild publication.
                await self._module_interaction_runtime.drain()
            except Exception:
                log.exception("Error draining module interaction handlers")
        await self._resources.command_sync.cancel_all()

    async def close_resources(self) -> None:
        resources = self._resources
        self._gateway_ready = False
        await self.drain_interactions()
        if self._module_event_publisher is not None:
            self._module_event_publisher.uninstall()
            self._module_event_publisher = None
        await resources.turn_admission.close()
        await resources.guild_activation.close()
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
        if self._video_session_sweeper_task is not None:
            self._video_session_sweeper_task.cancel()
            try:
                await self._video_session_sweeper_task
            except asyncio.CancelledError:
                pass
            except Exception:
                log.exception("Error stopping video session sweeper")
            self._video_session_sweeper_task = None
            self._video_session_sweeper_started = False
        if self._transcript_retention_task is not None:
            self._transcript_retention_task.cancel()
            try:
                await self._transcript_retention_task
            except asyncio.CancelledError:
                pass
            except Exception:
                log.exception("Error stopping transcript retention sweeper")
            self._transcript_retention_task = None
            self._transcript_retention_sweeper_started = False
            self._active_transcript_retention_days = 0
            self._active_transcript_retention_sweep_interval_seconds = None
        if not await resources.active_operations.cancel_all():
            log.warning("Timed out waiting for active operations during shutdown")
        await drain_confirmed_privacy_deletions()
        await stop_event_writer()
        await resources.coding_tasks.close()
        try:
            await resources.tools.browser_service.close()
        except Exception:
            log.exception("Error closing browser service")
        if resources.moderation_service is not None:
            try:
                await resources.moderation_service.close()
            except Exception:
                log.exception("Error closing moderation service")
        # Order matters: stop claiming jobs, then close modules (which cancels
        # each module's in-flight event handlers via events.close_module), then
        # tear down the shared HTTP client and event bus nothing can reach anymore.
        if resources.module_manager.scheduler is not None:
            try:
                await resources.module_manager.scheduler.close()
            except Exception:
                log.exception("Error closing the module scheduler")
        await resources.module_manager.close()
        if resources.module_manager.http is not None:
            await resources.module_manager.http.close()
        if resources.module_manager.events is not None:
            await resources.module_manager.events.close()
        await resources.memory_manager.close()
        await resources.provider_manager.close()
        try:
            await resources.tools.video_service.close()
        except Exception:
            log.exception("Error closing video understanding service")
        await resources.database.close()

    def log_ready_state(self) -> None:
        resources = self._resources
        log.info(
            "Logged in as %s (ID: %s)",
            resources.bot.user,
            resources.bot.user.id if resources.bot.user else "?",
        )
        log.info(
            "LLM Models: chat=%s | compaction=%s",
            resources.callbacks.model_log_label("chat"),
            resources.callbacks.model_log_label("compaction"),
        )
        log.info(
            "Trust tiers: StaffRoleIDs=%s, RegularRoleIDs=%s",
            resources.settings.staff_role_ids,
            resources.settings.regular_role_ids,
        )
        active_guilds = resources.callbacks.active_guilds()
        pending_guilds = sum(1 for guild in resources.bot.guilds if guild.id not in active_guilds)
        log.info(
            "Guild activation: %d active, %d connected and silent",
            len(active_guilds),
            pending_guilds,
        )

    async def initialize_ready(self) -> bool:
        resources = self._resources
        settings = resources.settings
        if settings.tool_event_log_enabled:
            start_event_writer(
                settings.tool_event_log_path,
                settings.tool_event_log_max_field_bytes,
                content_mode=settings.tool_event_log_content_mode,
                secret_values=settings_secret_values(settings),
            )
            if settings.tool_event_log_content_mode == "full":
                log.warning(
                    "Tool event log enabled at %s in full mode; sensitive content may be written",
                    settings.tool_event_log_path,
                )
            else:
                log.info(
                    "Tool event log enabled at %s in %s mode",
                    settings.tool_event_log_path,
                    settings.tool_event_log_content_mode,
                )

        first_init = not self._db_initialized
        if first_init:
            try:
                await self.initialize()
            except Exception as exc:
                self._startup_error = exc
                log.critical("Kimi Agent startup failed; closing the client", exc_info=True)
                return False

        try:
            if resources.memory_manager.client:
                await resources.memory_manager.ensure_ready(
                    resources.repositories.conversation_store,
                    resources.repositories.preference_store,
                )
            else:
                log.warning("No Hindsight URL configured - running without memory")
        except Exception as exc:
            if first_init:
                self._startup_error = exc
                log.critical("Kimi Agent startup failed; closing the client", exc_info=True)
                return False
            log.warning("Could not refresh memory integration after READY", exc_info=True)

        if first_init:
            self._db_initialized = True
            log.info("Database initialized at %s", settings.database_path)
        return True

    async def start_filesystem_sweepers(self) -> None:
        resources = self._resources
        settings = resources.settings
        if self._workspace_sweeper_started:
            return
        try:
            await sweep_attachment_orphans_once(
                resources.tools.attachment_store,
                max_age_seconds=settings.attachment_orphan_ttl_seconds,
                max_files=settings.attachment_orphan_sweep_max_files,
            )
        except OSError:
            log.warning("Initial attachment orphan sweep failed", exc_info=True)
        if self._closed:
            return
        self._workspace_sweeper_task = asyncio.create_task(
            workspace_sweeper(
                resources.tools.workspace_manager,
                sweep_interval=settings.workspace_sweep_interval,
                workspace_locks=resources.tools.workspace_locks,
                browser_profiles=resources.tools.browser_service,
            )
        )
        self._attachment_sweeper_task = asyncio.create_task(
            attachment_orphan_sweeper(
                resources.tools.attachment_store,
                sweep_interval=settings.attachment_orphan_sweep_interval_seconds,
                max_age_seconds=settings.attachment_orphan_ttl_seconds,
                max_files=settings.attachment_orphan_sweep_max_files,
            )
        )
        self._workspace_sweeper_started = True
        log.info(
            "Filesystem sweepers started (workspace TTL: %ds; "
            "attachment orphan TTL: %ds, every %ds)",
            settings.workspace_file_ttl,
            settings.attachment_orphan_ttl_seconds,
            settings.attachment_orphan_sweep_interval_seconds,
        )

    def start_ready_background_tasks(self) -> None:
        resources = self._resources
        settings = resources.settings
        repositories = resources.repositories
        if not self._video_session_sweeper_started:
            sweep_interval = settings.transcript_retention_sweep_interval_seconds
            self._video_session_sweeper_task = asyncio.create_task(
                video_session_sweeper(
                    resources.tools.video_service,
                    sweep_interval=sweep_interval,
                )
            )
            self._video_session_sweeper_started = True
            log.info("Video session sweeper started (every %ds)", sweep_interval)

        if (
            not self._transcript_retention_sweeper_started
            and settings.transcript_retention_days > 0
        ):
            retention_days = settings.transcript_retention_days
            sweep_interval = settings.transcript_retention_sweep_interval_seconds
            self._transcript_retention_task = asyncio.create_task(
                transcript_retention_sweeper(
                    repositories.conversation_store,
                    retention_days=retention_days,
                    sweep_interval=sweep_interval,
                )
            )
            self._transcript_retention_sweeper_started = True
            self._active_transcript_retention_days = retention_days
            self._active_transcript_retention_sweep_interval_seconds = sweep_interval
            log.info(
                "Transcript retention sweeper started (window: %dd, every %ds)",
                retention_days,
                sweep_interval,
            )

        active_memory_client = resources.memory_manager.active_client()
        if (
            not self._auto_retain_sweeper_started
            and settings.memory_auto_retain_enabled
            and active_memory_client is not None
        ):
            flusher = AutoRetainFlusher(
                store=AutoRetainStore(resources.database),
                preference_store=repositories.preference_store,
                memory_client=active_memory_client,
                ensure_user_bank=ensure_user_bank,
                get_bot_name=lambda: settings.bot_name,
                idle_seconds=settings.memory_auto_retain_idle_minutes * 60,
                backfill_horizon_seconds=(
                    settings.memory_auto_retain_backfill_horizon_hours * 3600
                ),
                min_user_chars=settings.memory_auto_retain_min_user_chars,
                max_content_chars=settings.memory_auto_retain_max_content_chars,
                max_flushes_per_sweep=settings.memory_auto_retain_max_flushes_per_sweep,
            )
            self._auto_retain_task = asyncio.create_task(
                auto_retain_sweeper(
                    flusher,
                    sweep_interval=settings.memory_auto_retain_sweep_interval_seconds,
                )
            )
            self._auto_retain_sweeper_started = True
            log.info(
                "Auto-retain sweeper started (idle: %dm, every %ds)",
                settings.memory_auto_retain_idle_minutes,
                settings.memory_auto_retain_sweep_interval_seconds,
            )
        resources.guild_activation.start()

    async def initialize(self) -> None:
        resources = self._resources
        settings = resources.settings
        repositories = resources.repositories
        callbacks = resources.callbacks
        await resources.database.connect()
        await resources.provider_manager.initialize_circuits(
            ProviderCircuitStore(resources.database)
        )
        await resources.provider_manager.refresh_selectable_chat_models()
        selected_model = await repositories.model_selection_store.get()
        try:
            resources.provider_manager.set_active_chat_model(selected_model)
        except ValueError:
            log.warning(
                "Stored chat model %r is no longer operator-selectable; reverting to config",
                selected_model,
            )
            await repositories.model_selection_store.set(None)
            resources.provider_manager.set_active_chat_model(None)

        configure_bank_tracking = getattr(
            resources.memory_manager.client,
            "set_user_bank_state_store",
            None,
        )
        if configure_bank_tracking is not None:
            configure_bank_tracking(repositories.user_memory_bank_state_store)
        set_user_memory_preference_store(repositories.preference_store)
        auto_retain_watermarks = AutoRetainStore(resources.database)
        await self.resume_pending_privacy_deletions(auto_retain_watermarks=auto_retain_watermarks)

        if settings.thread_handoff_enabled:
            from app.threads import ThreadHandoffManager

            self._thread_handoff = ThreadHandoffManager(repositories.conversation_store)
            await self._thread_handoff.load()
            log.info(
                "Thread handoff enabled (%d managed thread(s), %d auto-responding)",
                self._thread_handoff.managed_count,
                self._thread_handoff.auto_respond_count,
            )

        register_memory_command(
            resources.bot,
            repositories.preference_store,
            privacy_barrier=resources.privacy_barrier,
            user_install_enabled=settings.user_app_chat_enabled,
        )
        register_models_command(
            resources.bot,
            resources.provider_manager,
            repositories.model_selection_store,
            owner_user_id=settings.owner_user_id,
        )
        register_moderation_command(
            resources.bot,
            repositories.blocked_user_store,
            resources.trust_resolver,
        )
        module_manager = resources.module_manager
        register_modules_command(
            resources.bot,
            owner_user_id=settings.owner_user_id,
            requested=lambda: module_manager.load_state.requested,
            specs=lambda: module_manager.specs,
            health=module_manager.health_snapshot,
            disabled=lambda: module_manager.disabled_modules,
            tools=module_manager.tool_names,
            resolved_hosts=lambda name: tuple(
                f"{rule.host}{' (private)' if rule.private else ''}"
                for rule in module_manager.host_rules(name)
            ),
        )
        register_usage_command(
            resources.bot,
            repositories.usage_store,
            resources.trust_resolver,
        )
        register_stop_command(
            resources.bot,
            resources.work_cancellation.handle_stop_interaction,
            user_install_enabled=settings.user_app_chat_enabled,
        )
        register_learn_command(
            resources.bot,
            resources.trust_resolver,
            run_learn=callbacks.run_learn,
            is_blocked=callbacks.is_user_blocked,
            request_consent=lambda interaction, resume: resources.user_app_consent.prompt_if_needed(
                interaction,
                on_accept=resume,
                public_response=False,
            ),
            bot_name=settings.bot_name,
        )
        register_privacy_command(
            resources.bot,
            repositories.conversation_store,
            repositories.preference_store,
            memory_client=resources.memory_manager.client,
            auto_retain_watermarks=auto_retain_watermarks,
            deletion_request_store=repositories.privacy_deletion_store,
            memory_bank_state_store=repositories.user_memory_bank_state_store,
            conversation_turn_lock=callbacks.lock_user_conversations,
            workspace_manager=resources.tools.workspace_manager,
            workspace_locks=resources.tools.workspace_locks,
            privacy_barrier=resources.privacy_barrier,
            retention_days=settings.transcript_retention_days,
            bot_name=settings.bot_name,
            policy_url=settings.privacy_policy_url,
            browser_data_store=resources.tools.browser_service,
            video_data_store=resources.tools.video_service,
            cancel_user_work=resources.work_cancellation.cancel_for_privacy,
            is_available=callbacks.gateway_interactions_ready,
            user_install_enabled=settings.user_app_chat_enabled,
        )
        if settings.user_app_chat_enabled:
            register_user_app_chat_commands(
                resources.bot,
                run_chat=resources.user_app_chat.handle,
                reset_chat=resources.user_app_chat.reset,
                bot_name=settings.bot_name,
            )

        module_manager.health.on_change = lambda name, health: emit_module_health(
            module=name,
            state=health.state,
            detail=health.detail,
            metrics=dict(health.metrics),
        )
        module_manager.events = EventBusImpl(metrics_sink=module_manager.health.merge_metrics)
        module_manager.scheduler = DurableScheduler(
            resources.database,
            max_concurrent=settings.module_scheduler_max_concurrent_jobs,
            on_health=lambda module, state, detail: module_manager.health.mark(
                module, state, detail, source="scheduler"
            ),
        )
        module_manager.http = ModuleHttpRuntime(user_agent=f"{settings.bot_name}-modules")
        module_manager.guild_settings = GuildSettingsService(
            config_dir=lambda: Path(settings.config_dir),
            schemas=module_manager.guild_settings_schemas,
            on_health=lambda module, state, detail: module_manager.health.mark(
                module, state, detail, source="guild_settings"
            ),
        )
        await resources.guild_activation.refresh_module_guild_settings(None)
        self._module_event_publisher = ModuleEventPublisher(
            resources.bot, module_manager.events.publish_core
        )
        self._module_event_publisher.install()
        module_trust = TrustLookupImpl(resources.bot, resources.trust_resolver)
        is_guild_active = lambda guild_id: guild_id in callbacks.active_guilds()  # noqa: E731
        interaction_runtime = InteractionRuntime(
            resources.bot,
            is_available=callbacks.gateway_interactions_ready,
            scope_store=GuildCommandScopeStore(resources.database),
            on_sync_health=lambda module, state, detail: module_manager.health.mark(
                module,
                state,
                detail,
                source="guild_commands",
            ),
        )
        self._module_interaction_runtime = interaction_runtime
        interaction_runtime.install()
        proposal_actions = DiscordActionsImpl(
            bot=resources.bot,
            trust=module_trust,
            module_name=ROUTER_NAME,
            is_guild_active=is_guild_active,
        )
        self._proposal_service = ConfigProposalService(
            resources.database,
            ProposalHost(
                config_dir=lambda: Path(settings.config_dir),
                review_channel_id=lambda guild_id: load_proposal_channel_id(
                    guild_id, config_dir=Path(settings.config_dir)
                ),
                channel_guild_id=resources.guild_activation.channel_guild_id,
                known_modules=lambda: module_manager.load_state.loaded,
                post_review=proposal_actions.send_message,
                on_applied=callbacks.refresh_guild_activation,
                verify_guild=resources.guild_activation.proposal_guild_health,
                review_channel_configured=lambda guild_id: proposal_channel_id_is_configured(
                    guild_id, config_dir=Path(settings.config_dir)
                ),
            ),
        )
        self._proposal_service.install(
            interaction_runtime.router_for(
                ROUTER_NAME,
                trust=module_trust,
                is_guild_active=is_guild_active,
            )
        )
        await self._proposal_service.warn_unattached()

        def module_discord_actions(
            spec: ModuleSpec, module_is_guild_active: Callable[[int], bool]
        ) -> DiscordActionsImpl:
            return DiscordActionsImpl(
                bot=resources.bot,
                trust=module_trust,
                module_name=spec.name,
                is_guild_active=module_is_guild_active,
                override_target_policy=spec.permissions.override_target_policy,
            )

        def module_interactions(
            module_name: str, module_is_guild_active: Callable[[int], bool]
        ) -> InteractionRouter:
            return interaction_runtime.router_for(
                module_name,
                trust=module_trust,
                is_guild_active=module_is_guild_active,
            )

        await module_manager.start(
            ModuleRuntimeBase(
                database=resources.database,
                bot=resources.bot,
                is_guild_active=is_guild_active,
                current_config_dir=lambda: Path(settings.config_dir),
                capabilities=module_capabilities(settings),
                trust=module_trust,
                discord_actions=module_discord_actions,
                interactions=module_interactions,
                proposals=self._proposal_service,
            )
        )
        # Persisted module jobs re-bind to handlers registered during start().
        module_manager.scheduler.start()
        # Start the durable scheduler only after pending privacy deletions have
        # replayed and their barriers are installed. This prevents recovered
        # work from racing a deletion request during READY initialization.
        await resources.coding_tasks.start()

    async def resume_pending_privacy_deletions(
        self,
        *,
        auto_retain_watermarks: AutoRetainStore,
    ) -> None:
        resources = self._resources
        repositories = resources.repositories
        pending = await repositories.privacy_deletion_store.list_pending()
        if not pending:
            return
        for request in pending:
            await resources.privacy_barrier.mark_deletion_pending(request.user_id)

        failed: list[str] = []
        for request in pending:
            try:
                outcome = await run_privacy_deletion(
                    scope=request.scope,
                    user_id=request.user_id,
                    conversation_store=repositories.conversation_store,
                    preference_store=repositories.preference_store,
                    memory_client=resources.memory_manager.client,
                    auto_retain_watermarks=auto_retain_watermarks,
                    workspace_manager=resources.tools.workspace_manager,
                    workspace_locks=resources.tools.workspace_locks,
                    privacy_barrier=resources.privacy_barrier,
                    deletion_request_store=repositories.privacy_deletion_store,
                    pending_request=request,
                    memory_bank_state_store=repositories.user_memory_bank_state_store,
                    conversation_turn_lock=resources.callbacks.lock_user_conversations,
                    browser_data_store=resources.tools.browser_service,
                    video_data_store=resources.tools.video_service,
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

        remaining = await repositories.privacy_deletion_store.list_pending()
        if failed or remaining:
            affected = sorted({request.user_id for request in remaining} | set(failed))
            log.error(
                "Pending privacy deletion could not be completed at startup for "
                "%d user(s); their activity remains paused while unaffected users "
                "continue normally: %s",
                len(affected),
                ", ".join(affected),
            )

    # Explicit test seams for lifecycle states that cannot be reached without
    # starting Discord or intentionally blocking shutdown.
    def set_closed_for_test(self, value: bool) -> None:
        self._closed = value

    def set_startup_error_for_test(self, error: Exception | None) -> None:
        self._startup_error = error

    def set_db_initialized_for_test(self, value: bool) -> None:
        self._db_initialized = value

    def set_gateway_ready_for_test(self, value: bool) -> None:
        self._gateway_ready = value

    def set_workspace_sweeper_started_for_test(self, value: bool) -> None:
        self._workspace_sweeper_started = value

    def set_video_session_sweeper_started_for_test(self, value: bool) -> None:
        self._video_session_sweeper_started = value

    def set_video_session_sweeper_task_for_test(self, task: asyncio.Task[Any] | None) -> None:
        self._video_session_sweeper_task = task

    def set_guild_activation_refresh_task_for_test(self, task: asyncio.Task[None] | None) -> None:
        self._resources.guild_activation._refresh_task = task

    def set_module_event_publisher_for_test(self, publisher: ModuleEventPublisher | None) -> None:
        self._module_event_publisher = publisher

    def set_module_interaction_runtime_for_test(self, runtime: InteractionRuntime | None) -> None:
        self._module_interaction_runtime = runtime

    def set_thread_handoff_for_test(self, handoff: Any | None) -> None:
        self._thread_handoff = handoff

    def replace_repositories_for_test(self, repositories: AppRepositories) -> None:
        self._resources = replace(self._resources, repositories=repositories)

    def replace_resources_for_test(self, **changes: Any) -> None:
        self._resources = replace(self._resources, **changes)
