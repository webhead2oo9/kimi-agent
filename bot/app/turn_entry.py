from __future__ import annotations

import discord
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

from agent.attachments import (
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
from agent.core import run_conversation
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
    EnsureUserBank,
    RecallCurrentUserContext,
    RunConversation,
    TurnDependencies,
    TurnPreparationConfig,
    TurnPreparationInput,
    UserPersonaLoader,
    WriteGeneratedAssets,
)
from config.model_config import Scope
from memory.banks import ensure_user_bank
from memory.recall import recall_current_user_context
from providers.assets import write_generated_assets


@dataclass(frozen=True)
class TurnEntryHooks:
    turn_has_image_input: Any = turn_has_image_input
    collect_turn_images: Any = collect_turn_images
    collect_reply_context: Any = collect_reply_context
    collect_turn_attachments: Any = collect_turn_attachments
    run_conversation: Any = run_conversation
    ensure_user_bank: Any = ensure_user_bank
    recall_current_user_context: Any = recall_current_user_context
    write_generated_assets: Any = write_generated_assets
    load_channel_pinned_tools: Any = load_channel_pinned_tools
    load_guild_pinned_tools: Any = load_guild_pinned_tools
    load_channel_blocked_tools: Any = load_channel_blocked_tools
    load_guild_blocked_tools: Any = load_guild_blocked_tools
    load_global_blocked_tools: Any = load_global_blocked_tools
    load_tool_configs: Any = load_tool_configs
    load_channel_thread_handoff: Any = load_channel_thread_handoff
    load_guild_thread_handoff: Any = load_guild_thread_handoff
    filter_pins_to_searchable: Any = filter_pins_to_searchable


def _resolve_chat_provider(provider_manager: object, scope: Scope, *, images: bool = False) -> Any:
    resolve = getattr(provider_manager, "resolve", None)
    if callable(resolve):
        return resolve("chat", scope, images=images)
    # getattr, not attribute access: provider_manager is typed `object` here so the
    # duck-typed fallback stays usable by test doubles that only expose `main`.
    return getattr(provider_manager, "main")  # noqa: B009


def chat_model_name_for_scope(provider_manager: Any, scope: Scope, *, images: bool = False) -> str:
    """The configured chat model for a scope, or "" when routing is unavailable.

    One implementation, called both here and by the application's own method
    (`app/runtime.py`), which the duck-typed lookup below prefers when present.
    """

    resolver = getattr(provider_manager, "resolved_chat_model_name", None)
    if callable(resolver):
        return str(resolver(scope, images=images))
    model_config = getattr(provider_manager, "model_config", None)
    if model_config is None:
        return ""
    return str(model_config.model_name_for_role("chat", scope, images=images))


def _resolved_chat_model_name(app: Any, scope: Scope, *, images: bool = False) -> str:
    # Test doubles stand in for the application without carrying its method, so
    # fall back to resolving from the provider manager directly.
    resolver = getattr(app, "_resolved_chat_model_name", None)
    if callable(resolver):
        return str(resolver(scope, images=images))
    return chat_model_name_for_scope(app.provider_manager, scope, images=images)


def build_turn_preparation_config(
    settings: Any,
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
_PERSONAL_CHAT_BLOCKED_TOOLS = frozenset(
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


def _platform_scope_blocked_tools(guild_id: str | None) -> frozenset[str]:
    """Hide platform actions that cannot exist in the current conversation scope."""
    return frozenset() if guild_id else frozenset({"move_to_thread"})


async def build_turn_dependencies(
    app: Any,
    source: TurnPreparationInput,
    *,
    context_manager: Any,
    registry: Any,
    preference_store: Any | None,
    usage_store: Any,
    collect_reply_context_func: Any,
    strip_mention_func: Any,
    persist_prepared_user_message: Any,
    hooks: TurnEntryHooks | None = None,
    command_template: str | None = None,
    collect_turn_attachments_func: Any | None = None,
    count_user_prior_messages: Any | None = None,
    activity_reporter: Any | None = None,
    extra_blocked_tools: frozenset[str] = frozenset(),
) -> TurnDependencies:
    hooks = hooks or TurnEntryHooks()
    collect_turn_attachments_func = collect_turn_attachments_func or hooks.collect_turn_attachments
    chat_scope = Scope(
        guild_id=None if source.personal_chat else source.guild_id,
        channel_id="" if source.personal_chat else source.channel_id,
        user_id=source.user_id,
        command=command_template,
    )

    def chat_provider_for_turn(*, images: bool = False) -> Any:
        return _resolve_chat_provider(
            app.provider_manager,
            chat_scope,
            images=images,
        )

    def chat_model_name_for_turn(*, images: bool = False) -> str:
        return _resolved_chat_model_name(
            app,
            chat_scope,
            images=images,
        )

    image_probe_kwargs: dict[str, Any] = {"bot_user": app.bot.user}
    if getattr(source, "allow_bot_authored_reply_context", False):
        image_probe_kwargs["allow_bot_authored"] = True
    has_images = bool(app.settings.max_turn_images > 0) and await hooks.turn_has_image_input(
        source.source_message, **image_probe_kwargs
    )
    provider = chat_provider_for_turn(images=has_images)
    tool_config_channel_id = _tool_config_channel_id(source)

    def skills_index_builder() -> str:
        return app.skills_index_cache.index(None if source.personal_chat else source.guild_id)

    def personal_skills_index_builder() -> str:
        return app.tools.personal_skill_manager.index(source.user_id)

    async def user_persona_loader(user_id: str) -> str:
        if preference_store is None:
            return ""
        return await preference_store.get_persona(user_id)

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
            registry,
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
        specs = getattr(registry, "config_specs", None)
        if not callable(specs):
            return {}
        return hooks.load_tool_configs(specs())

    return TurnDependencies(
        context_manager=context_manager,
        provider=provider,
        registry=registry,
        attachment_store=app.tools.attachment_store,
        workspace_dir=app.tools.workspace_dir,
        workspace_manager=app.tools.workspace_manager,
        workspace_locks=app.tools.workspace_locks,
        llm_semaphore=app.llm_semaphore,
        memory_client=app.memory_manager.active_client(),
        preference_store=preference_store,
        ensure_user_bank=cast(EnsureUserBank, hooks.ensure_user_bank),
        recall_current_user_context=cast(
            RecallCurrentUserContext, hooks.recall_current_user_context
        ),
        skills_index_builder=skills_index_builder,
        personal_skills_index_builder=personal_skills_index_builder,
        user_persona_loader=cast(UserPersonaLoader, user_persona_loader),
        count_user_prior_messages=count_user_prior_messages,
        channel_pinned_tools=channel_pinned_tools,
        blocked_tools=blocked_tools,
        tool_configs=tool_configs,
        collect_turn_images=hooks.collect_turn_images,
        collect_reply_context=collect_reply_context_func,
        collect_turn_attachments=collect_turn_attachments_func,
        strip_mention=strip_mention_func,
        run_conversation=cast(RunConversation, hooks.run_conversation),
        chat_provider_resolver=chat_provider_for_turn,
        chat_model_name_resolver=chat_model_name_for_turn,
        persist_prepared_user_message=persist_prepared_user_message,
        write_generated_assets=cast(WriteGeneratedAssets, hooks.write_generated_assets),
        compactor=app.provider_manager.build_compactor(app.llm_semaphore),
        activity_reporter=activity_reporter,
        moderation_service=app.moderation_service,
        usage_store=usage_store,
        image_distillation_store=app.image_distillation_store,
        model_config=getattr(app.provider_manager, "model_config", None),
        resolved_model_name=chat_model_name_for_turn(images=has_images),
        user_activity=app.privacy_barrier.activity,
    )
