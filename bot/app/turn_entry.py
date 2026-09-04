from __future__ import annotations

import discord
import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from agent.activity import ActivityReporter
from agent.attachments import (
    AttachmentStore,
    collect_reply_context,
    collect_turn_attachments,
    collect_turn_images,
    turn_has_image_input,
)
from config.fragments.channel_pins import (
    filter_pins_to_searchable,
    load_channel_blocked_tools,
    load_channel_pinned_tools,
    load_channel_thread_handoff,
)
from agent.core import UserActivityGuard, run_conversation
from agent.discord_references import ResolvedDiscordReferenceHint
from config.fragments.guild_config import (
    load_guild_blocked_tools,
    load_guild_pinned_tools,
    load_guild_thread_handoff,
)
from config.fragments.tool_config import load_tool_configs
from config.fragments.tool_policy import (
    load_blocked_tools,
    load_global_blocked_tools,
    thread_handoff_creation_allowed,
)
from agent.turn import (
    CollectReplyContext,
    CollectTurnAttachments,
    CollectTurnImages,
    CountUserPriorMessages,
    EnsureUserBank,
    MemoryPreferenceStore,
    PersistPreparedUserMessage,
    RecallCurrentUserContext,
    RunConversation,
    StripMention,
    TurnDependencies,
    TurnContextManager,
    TurnPreparationConfig,
    TurnPreparationInput,
    TurnProvider,
    WriteGeneratedAssets,
)
from config.model_config import Scope
from config.settings import Settings
from memory.banks import ensure_user_bank
from memory.recall import recall_current_user_context
from providers.assets import write_generated_assets
from tools.coding_tasks import CODING_CONTROL_TOOLS
from tools.config_spec import ToolConfigField
from tools.registry import ToolRegistry
from tools.workspace.common import UserLocks
from trust.tiers import TrustTier
from workspace import WorkspaceManager

if TYPE_CHECKING:
    from app.providers import ProviderManager
    from memory.client import MemoryClient
    from moderation.service import ModerationService
    from storage.image_distillations import ImageDistillationStore
    from storage.usage import UsageStore


class TurnHasImageInput(Protocol):
    async def __call__(
        self,
        message: object,
        *,
        bot_user: object | None = None,
        allow_bot_authored: bool = False,
    ) -> bool: ...


type FragmentSetLoader = Callable[[str], frozenset[str]]
type GlobalBlockedToolsLoader = Callable[[], frozenset[str]]
type ThreadHandoffLoader = Callable[[str], bool | None]
type FilterPinsToSearchable = Callable[
    [frozenset[str], ToolRegistry, TrustTier, str | None], frozenset[str]
]
type ToolConfigLoader = Callable[
    [Mapping[str, Sequence[ToolConfigField]]], Mapping[str, Mapping[str, Any]]
]


class TurnPreferenceStore(MemoryPreferenceStore, Protocol):
    async def get_persona(self, user_id: str) -> str: ...


class ResolveReferenceHints(Protocol):
    async def __call__(
        self,
        source_message: object,
        content: str,
        *,
        excluded_channel_ids: frozenset[str],
    ) -> tuple[ResolvedDiscordReferenceHint, ...]: ...


@dataclass(frozen=True, slots=True)
class TurnEntryHooks:
    turn_has_image_input: TurnHasImageInput = turn_has_image_input
    collect_turn_images: CollectTurnImages = collect_turn_images
    collect_reply_context: CollectReplyContext = collect_reply_context
    collect_turn_attachments: CollectTurnAttachments = collect_turn_attachments
    run_conversation: RunConversation = run_conversation
    ensure_user_bank: EnsureUserBank = ensure_user_bank
    recall_current_user_context: RecallCurrentUserContext = recall_current_user_context
    write_generated_assets: WriteGeneratedAssets = write_generated_assets
    load_channel_pinned_tools: FragmentSetLoader = load_channel_pinned_tools
    load_guild_pinned_tools: FragmentSetLoader = load_guild_pinned_tools
    load_channel_blocked_tools: FragmentSetLoader = load_channel_blocked_tools
    load_guild_blocked_tools: FragmentSetLoader = load_guild_blocked_tools
    load_global_blocked_tools: GlobalBlockedToolsLoader = load_global_blocked_tools
    load_tool_configs: ToolConfigLoader = load_tool_configs
    load_channel_thread_handoff: ThreadHandoffLoader = load_channel_thread_handoff
    load_guild_thread_handoff: ThreadHandoffLoader = load_guild_thread_handoff
    filter_pins_to_searchable: FilterPinsToSearchable = filter_pins_to_searchable


@dataclass(frozen=True, slots=True)
class TurnEntryServices:
    settings: Settings
    get_bot_user: Callable[[], object | None]
    provider_manager: ProviderManager
    context_manager: TurnContextManager
    registry: ToolRegistry
    preference_store: TurnPreferenceStore | None
    usage_store: UsageStore | None
    attachment_store: AttachmentStore
    workspace_dir: Path
    workspace_manager: WorkspaceManager
    workspace_locks: UserLocks
    llm_semaphore: asyncio.Semaphore
    get_memory_client: Callable[[], MemoryClient | None]
    skills_index: Callable[[str | None], str]
    personal_skills_index: Callable[[str], str]
    resolve_reference_hints: ResolveReferenceHints
    moderation_service: ModerationService | None
    image_distillation_store: ImageDistillationStore | None
    user_activity: UserActivityGuard


@dataclass(frozen=True, slots=True)
class TurnDependencyFactory:
    """Build turn dependencies with application services bound once."""

    services: TurnEntryServices

    async def build(
        self,
        source: TurnPreparationInput,
        *,
        collect_reply_context_func: CollectReplyContext,
        strip_mention_func: StripMention,
        persist_prepared_user_message: PersistPreparedUserMessage,
        hooks: TurnEntryHooks | None = None,
        command_template: str | None = None,
        collect_turn_attachments_func: CollectTurnAttachments | None = None,
        count_user_prior_messages: CountUserPriorMessages | None = None,
        activity_reporter: ActivityReporter | None = None,
        extra_blocked_tools: frozenset[str] = frozenset(),
    ) -> TurnDependencies:
        return await build_turn_dependencies(
            self,
            source,
            collect_reply_context_func=collect_reply_context_func,
            strip_mention_func=strip_mention_func,
            persist_prepared_user_message=persist_prepared_user_message,
            hooks=hooks,
            command_template=command_template,
            collect_turn_attachments_func=collect_turn_attachments_func,
            count_user_prior_messages=count_user_prior_messages,
            activity_reporter=activity_reporter,
            extra_blocked_tools=extra_blocked_tools,
        )


def _resolve_chat_provider(
    provider_manager: ProviderManager,
    scope: Scope,
    *,
    images: bool = False,
) -> TurnProvider:
    return provider_manager.resolve("chat", scope, images=images)


def chat_model_name_for_scope(
    provider_manager: ProviderManager,
    scope: Scope,
    *,
    images: bool = False,
) -> str:
    """Return the configured chat model for a scope."""

    return provider_manager.resolved_chat_model_name(scope, images=images)


def build_turn_preparation_config(
    settings: Settings,
    *,
    recent_image_lookback: int,
    new_user_onboarding_turns: int = 0,
) -> TurnPreparationConfig:
    return TurnPreparationConfig(
        user_memory_recall_types=settings.user_memory_recall_types,
        user_memory_recall_budget=settings.memory_recall_budget,
        user_memory_recall_max_tokens=settings.memory_recall_max_tokens,
        image_detail=settings.image_detail,
        recent_image_lookback=recent_image_lookback,
        max_turn_images=settings.max_turn_images,
        new_user_onboarding_turns=new_user_onboarding_turns,
    )


def resolve_parent_channel_id(channel: Any) -> str:
    """The parent channel id for a thread, else the channel's own id.

    Per-channel operator config (tool pins, denylists, handoff policy, and the
    instructions fragment) is keyed on the channel a thread hangs off, not the
    thread itself. Returns "" when handed something with no usable id (a
    synthetic source message, a DM-less interaction).
    """
    if isinstance(channel, discord.Thread):
        parent_id = getattr(channel, "parent_id", None)
        if parent_id:
            return str(parent_id)
    channel_id = getattr(channel, "id", None)
    return str(channel_id) if channel_id else ""


def _tool_config_channel_id(source: TurnPreparationInput) -> str:
    channel = getattr(source.source_message, "channel", None)
    return resolve_parent_channel_id(channel) or source.channel_id


# Personal chat (`/chat`) is a guild-less surface invoked from an arbitrary
# location, so a tool whose meaning is bound to one guild has no coherent target
# there. Structurally message-rooted thread actions are simply absent. Community
# memory and shared skills are guild/deployment artifacts: writing to them from a
# personal turn would apply a tier granted outside that guild, and reading them
# would pull guild-private knowledge into a private transcript. Blocking is
# re-checked at the dispatch privilege boundary, and it keeps the model's tool
# list honest rather than offering tools that can only refuse.
_PERSONAL_CHAT_BLOCKED_TOOLS = (
    frozenset(
        {
            "move_to_thread",
            "leave_thread",
            "pause_thread_replies",
            "resume_thread_replies",
            "teach",
            "recall_community",
            "reflect_community",
            "skill_create",
            "skill_edit",
            "skill_delete",
        }
    )
    | CODING_CONTROL_TOOLS
)


def _platform_scope_blocked_tools(guild_id: str | None) -> frozenset[str]:
    """Hide platform actions that cannot exist in the current conversation scope."""
    return frozenset() if guild_id else frozenset({"move_to_thread"})


async def build_turn_dependencies(
    factory: TurnDependencyFactory,
    source: TurnPreparationInput,
    *,
    collect_reply_context_func: CollectReplyContext,
    strip_mention_func: StripMention,
    persist_prepared_user_message: PersistPreparedUserMessage,
    hooks: TurnEntryHooks | None = None,
    command_template: str | None = None,
    collect_turn_attachments_func: CollectTurnAttachments | None = None,
    count_user_prior_messages: CountUserPriorMessages | None = None,
    activity_reporter: ActivityReporter | None = None,
    extra_blocked_tools: frozenset[str] = frozenset(),
) -> TurnDependencies:
    services = factory.services
    hooks = hooks or TurnEntryHooks()
    collect_turn_attachments_func = collect_turn_attachments_func or hooks.collect_turn_attachments
    chat_scope = Scope(
        guild_id=None if source.personal_chat else source.guild_id,
        channel_id="" if source.personal_chat else source.channel_id,
        user_id=source.user_id,
        command=command_template,
    )

    def chat_provider_for_turn(*, images: bool = False) -> TurnProvider:
        return _resolve_chat_provider(
            services.provider_manager,
            chat_scope,
            images=images,
        )

    def chat_model_name_for_turn(*, images: bool = False) -> str:
        return chat_model_name_for_scope(services.provider_manager, chat_scope, images=images)

    image_probe_kwargs: dict[str, Any] = {"bot_user": services.get_bot_user()}
    if source.allow_bot_authored_reply_context:
        image_probe_kwargs["allow_bot_authored"] = True
    has_images = bool(services.settings.max_turn_images > 0) and await hooks.turn_has_image_input(
        source.source_message, **image_probe_kwargs
    )
    provider = chat_provider_for_turn(images=has_images)
    tool_config_channel_id = _tool_config_channel_id(source)

    def skills_index_builder() -> str:
        return services.skills_index(None if source.personal_chat else source.guild_id)

    def personal_skills_index_builder() -> str:
        return services.personal_skills_index(source.user_id)

    async def user_persona_loader(user_id: str) -> str:
        if services.preference_store is None:
            return ""
        return await services.preference_store.get_persona(user_id)

    def channel_pinned_tools() -> frozenset[str]:
        if source.personal_chat:
            return frozenset()
        pins = hooks.load_channel_pinned_tools(
            tool_config_channel_id
        ) | hooks.load_guild_pinned_tools(source.guild_id or "")
        if not pins:
            return frozenset()
        return hooks.filter_pins_to_searchable(
            pins,
            services.registry,
            source.trust_tier,
            source.guild_id,
        )

    def blocked_tools() -> frozenset[str]:
        # Global ∪ guild ∪ channel, plus whatever this turn's own state masks
        # (the caller's extra_blocked_tools; see thread_state_blocked_tools).
        # The three fragment scopes are read fresh each turn, so an operator
        # un-blocking a tool takes effect on the next message.
        if source.personal_chat:
            # Personal chat obeys deployment-wide policy, but never silently
            # inherits the guild/channel policy of wherever the command happened
            # to be invoked.
            return (
                hooks.load_global_blocked_tools()
                | extra_blocked_tools
                | _PERSONAL_CHAT_BLOCKED_TOOLS
            )
        blocked = (
            load_blocked_tools(
                source.guild_id or "",
                tool_config_channel_id,
                load_global=hooks.load_global_blocked_tools,
                load_guild=hooks.load_guild_blocked_tools,
                load_channel=hooks.load_channel_blocked_tools,
            )
            | extra_blocked_tools
            | _platform_scope_blocked_tools(source.guild_id)
        )
        # The tri-state fragment switch and an explicit move_to_thread deny both
        # gate creation. Channel still wins over guild for the switch, but no
        # narrower allow can subtract an explicit deny. leave_thread is separate
        # so managed conversations can always continue to a deliberate closure.
        if not thread_handoff_creation_allowed(
            blocked,
            channel=hooks.load_channel_thread_handoff(tool_config_channel_id),
            guild=hooks.load_guild_thread_handoff(source.guild_id or ""),
        ):
            blocked |= {"move_to_thread"}
        return blocked

    def tool_configs() -> Mapping[str, Mapping[str, Any]]:
        # Read fresh each turn from config/tools/<name>.md, against whatever the
        # registry for this entry path declares.
        return hooks.load_tool_configs(services.registry.config_specs())

    async def resolve_discord_references(
        content: str,
    ) -> tuple[ResolvedDiscordReferenceHint, ...]:
        # Personal chat is logically guild-less even when /chat was physically
        # invoked in a guild. Its platform location confers no read authority.
        if source.personal_chat or source.guild_id is None:
            return ()
        return await services.resolve_reference_hints(
            source.source_message,
            content,
            excluded_channel_ids=services.settings.discord_search_excluded_channel_ids,
        )

    return TurnDependencies(
        context_manager=services.context_manager,
        provider=provider,
        registry=services.registry,
        attachment_store=services.attachment_store,
        workspace_dir=services.workspace_dir,
        workspace_manager=services.workspace_manager,
        workspace_locks=services.workspace_locks,
        llm_semaphore=services.llm_semaphore,
        memory_client=services.get_memory_client(),
        preference_store=services.preference_store,
        ensure_user_bank=hooks.ensure_user_bank,
        recall_current_user_context=hooks.recall_current_user_context,
        skills_index_builder=skills_index_builder,
        personal_skills_index_builder=personal_skills_index_builder,
        user_persona_loader=user_persona_loader,
        count_user_prior_messages=count_user_prior_messages,
        channel_pinned_tools=channel_pinned_tools,
        blocked_tools=blocked_tools,
        tool_configs=tool_configs,
        resolve_discord_references=resolve_discord_references,
        collect_turn_images=hooks.collect_turn_images,
        collect_reply_context=collect_reply_context_func,
        collect_turn_attachments=collect_turn_attachments_func,
        strip_mention=strip_mention_func,
        run_conversation=hooks.run_conversation,
        chat_provider_resolver=chat_provider_for_turn,
        chat_model_name_resolver=chat_model_name_for_turn,
        persist_prepared_user_message=persist_prepared_user_message,
        write_generated_assets=hooks.write_generated_assets,
        compactor=services.provider_manager.build_compactor(services.llm_semaphore),
        activity_reporter=activity_reporter,
        moderation_service=services.moderation_service,
        usage_store=services.usage_store,
        image_distillation_store=services.image_distillation_store,
        model_config=services.provider_manager.model_config,
        resolved_model_name=chat_model_name_for_turn(images=has_images),
        user_activity=services.user_activity,
    )
