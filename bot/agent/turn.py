"""Dependency-injected turn orchestration for Discord response turns.

Turns do not share an in-process cache: context is built fresh for each turn
from the durable channel transcript. Transcript persistence is owned by the
caller (bot.py), which knows the Discord message ids; handle_turn only prepares
and executes.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import shutil
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

from agent.activity import (
    ActivityReporter,
    ActivityUpdate,
    SupportsNarrationSteps,
    SupportsPlanUpdates,
)
from agent.attachments import (
    AttachmentRef,
    CollectedImage,
    TurnImages,
    cleanup_attachment_paths,
    image_byte_hashes,
    image_part_hash,
    message_has_image_attachment,
)
from agent.context import ConversationContext
from agent.core import (
    ConversationRunRequest,
    ConversationRunResult,
    ConversationTurnTimeoutError,
    _await_with_deadline,
    _await_guarded_with_deadline,
    _deadline_from_timeout,
    UserActivityGuard,
    turn_timeout_response,
)
from agent.reply_context import ReplyContext
from workspace import WorkspaceKey, WorkspaceManager, workspace_owner_key
from utils.image_types import normalize_image_data_url
from memory.mutations import user_memory_mutation
from moderation.files import (
    MAX_TEXT_MODERATION_BYTES,
    UNSUPPORTED_MODERATION_FILE_MESSAGE,
    UnsupportedModerationFile,
    text_from_file_bytes,
)
from moderation.types import Direction
from providers.base import LLMProvider
from providers.image_caption import (
    IMAGE_CAPTION_MAX_TOKENS,
    IMAGE_CAPTION_PROMPT_VERSION,
    IMAGE_CAPTION_SYSTEM_PROMPT,
    format_image_caption,
    is_image_caption,
)
from providers.types import (
    ContentPart,
    ContentPartType,
    ConversationMessage,
    GeneratedAsset,
    ProviderCapability,
    ProviderRequest,
)
from tools.registry import ToolRegistry, TurnHandoff
from tools.workspace.common import UserLocks
from trust.tiers import TrustTier
from usage.pricing import price_usage_call
from usage.normalization import LLMUsageCall, UsageBreakdown, normalize_usage

if TYPE_CHECKING:
    from moderation.service import ModerationService
    from storage.conversations import ConversationAccessScope
    from tools.embeds import EmbedSpec
    from tools.threads import ThreadCloseRequest, ThreadRequest

log = logging.getLogger(__name__)

# Captioning runs before transcript persistence, so it gets its own ceiling and
# must leave this much of the whole-turn budget for the durable message write.
_INGEST_IMAGE_CAPTION_TIMEOUT_SECONDS = 60.0
_INGEST_TRANSCRIPT_PERSISTENCE_RESERVE_SECONDS = 5.0


class TurnProvider(Protocol):
    @property
    def provider_key(self) -> str: ...

    @property
    def model(self) -> str: ...

    @property
    def capabilities(self) -> set[ProviderCapability]: ...


class TurnContextManager(Protocol):
    async def build_turn_context(
        self,
        key: str,
        channel_name: str,
        before_discord_message_id: str | None = None,
        *,
        owner_user_id: str | None = None,
        access_scope: ConversationAccessScope = "channel_shared",
    ) -> ConversationContext: ...

    async def add_activated_tools(
        self,
        context: ConversationContext,
        names: set[str],
    ) -> None: ...

    async def has_loaded_message(
        self,
        context: ConversationContext,
        discord_message_id: str,
    ) -> bool: ...


class MemoryPreferenceStore(Protocol):
    async def is_memory_enabled(self, user_id: str) -> bool: ...


class EnsureUserBank(Protocol):
    async def __call__(
        self,
        memory_client: Any,
        user_id: str,
        user_name: str,
    ) -> str | None: ...


class RecallCurrentUserContext(Protocol):
    async def __call__(
        self,
        *,
        memory_client: Any,
        preference_store: MemoryPreferenceStore | None,
        user_id: str,
        user_message: str,
        context: ConversationContext,
        guild_id: str | None,
        budget: str,
        max_tokens: int,
        types: list[str] | None,
    ) -> str: ...


class SkillsIndexBuilder(Protocol):
    def __call__(self) -> str: ...


class UserPersonaLoader(Protocol):
    async def __call__(self, user_id: str) -> str: ...


class ChannelPinnedTools(Protocol):
    """Resolve the operator-pinned searchable tools for the turn's channel.

    The callable owns channel resolution (including mapping a thread back to
    its parent channel) and registry/tier filtering; prepare_turn only merges
    the returned names into the turn's activated-tool set.
    """

    def __call__(self) -> frozenset[str]: ...


class BlockedTools(Protocol):
    """Resolve the operator denylist (guild ∪ channel ``blocked_tools``) for the
    turn's channel. The callable owns channel/guild resolution; prepare_turn
    stashes the returned names on the context so the registry hides and masks
    them this turn.
    """

    def __call__(self) -> frozenset[str]: ...


class ToolConfigs(Protocol):
    """Resolve every spec'd tool's operator config for this turn.

    The callable owns reading ``config/tools/<name>.md`` against the registry's
    declared specs and resolving each over its defaults; prepare_turn only
    stashes the finished mapping on the context, from which the core copies it
    onto ``MessageContext.tool_configs`` for handlers.
    """

    def __call__(self) -> Mapping[str, Mapping[str, Any]]: ...


class CollectTurnImages(Protocol):
    async def __call__(
        self,
        message: Any,
        *,
        store: Any,
        conversation_key: str,
        detail: str,
        images_supported: bool,
        history_hashes: set[str],
        lookback: int,
        max_images: int,
        include_reply_images: bool = True,
    ) -> TurnImages: ...


class CollectReplyContext(Protocol):
    async def __call__(
        self,
        message: Any,
        *,
        bot_user: Any,
        store: Any,
        conversation_key: str,
        detail: str,
        images_supported: bool,
        history_hashes: set[str],
        current_hashes: set[str],
        max_images: int,
        prefetched_images: Sequence[CollectedImage] | None = None,
        allow_bot_authored: bool = False,
    ) -> ReplyContext | None: ...


class CollectTurnAttachments(Protocol):
    def __call__(self, message: Any) -> list[AttachmentRef]: ...


class StripMention(Protocol):
    def __call__(self, content: str, *, bot_user: Any) -> str: ...


class RunConversation(Protocol):
    async def __call__(self, request: ConversationRunRequest) -> ConversationRunResult: ...


class ChatProviderResolver(Protocol):
    def __call__(self, *, images: bool = False) -> TurnProvider: ...


class ChatModelNameResolver(Protocol):
    def __call__(self, *, images: bool = False) -> str: ...


class ImageDistillationCache(Protocol):
    async def get(self, conversation_id: int, cache_key: str) -> tuple[str, str] | None: ...

    async def set(
        self,
        conversation_id: int,
        cache_key: str,
        *,
        model_name: str,
        prompt_version: int,
        description: str,
    ) -> None: ...


class PersistPreparedUserMessage(Protocol):
    async def __call__(
        self,
        source: TurnPreparationInput,
        turn: TurnRequest,
    ) -> None: ...


class CountUserPriorMessages(Protocol):
    async def __call__(
        self, user_id: str, exclude_discord_message_id: str | None, limit: int
    ) -> int: ...


class WriteGeneratedAssets(Protocol):
    def __call__(
        self,
        generated_assets: list[GeneratedAsset],
        *,
        output_dir: Path,
    ) -> list[Path]: ...


class ModerationServiceLike(Protocol):
    enabled: bool

    async def check(self, **kwargs: Any) -> Any: ...

    def refusal_for(self, direction: Direction, *, error: bool = False) -> str: ...


@dataclass(frozen=True)
class TurnPreparationInput:
    raw_content: str
    source_message: Any
    bot_user: Any
    guild_id: str | None
    channel_id: str
    thread_id: str | None
    channel_name: str
    user_id: str
    user_name: str
    trust_tier: TrustTier
    conversation_key: str
    # The thread's parent channel when ``channel_id`` names a thread, else the
    # channel itself. Operator instruction fragments resolve against it so a
    # thread inherits its channel (config/fragments/prompt.py:instruction_fragment_candidates).
    # Entry paths with no live channel object leave it empty, which falls back to
    # ``channel_id``.
    parent_channel_id: str = ""
    guild_name: str = ""
    trigger_discord_message_id: str = ""
    referenced_message_id: str | None = None
    conversation_owner_user_id: str | None = None
    conversation_access_scope: ConversationAccessScope = "channel_shared"
    allow_bot_authored_reply_context: bool = False


@dataclass(frozen=True)
class TurnPreparationConfig:
    user_memory_recall_types: Sequence[str]
    image_detail: str
    recent_image_lookback: int
    max_turn_images: int
    # Inject the new-user onboarding note while the user has fewer than this many prior
    # messages with the bot. 0 disables the feature.
    new_user_onboarding_turns: int = 0
    # Hindsight recall breadth for automatic responding-turn recall. Defaults mirror
    # memory/recall.py (kept as literals to avoid an agent->memory import) so
    # Direct test constructions stay tight without re-specifying them.
    user_memory_recall_budget: str = "mid"
    user_memory_recall_max_tokens: int = 2048


@dataclass(frozen=True)
class TurnExecutionConfig:
    max_iterations: int
    max_tokens: int
    temperature: float | None = None
    bot_name: str = ""
    command_template: str | None = None
    timeout_seconds: float | None = None
    thread_handoff_suggest_after_tool_calls: int = 0


@dataclass(frozen=True)
class TurnRequest:
    content: str
    context: ConversationContext
    trust_tier: TrustTier
    user_id: str
    user_name: str
    guild_id: str | None
    channel_id: str
    thread_id: str | None
    channel_name: str
    # The triggering platform member, kept opaque so the agent layer does not
    # import Discord. Used by permission-sensitive tool resolvers.
    platform_member: Any | None = None
    parent_channel_id: str = ""
    guild_name: str = ""
    trigger_discord_message_id: str = ""
    recalled_memories: str = ""
    skills_index: str = ""
    personal_skills_index: str = ""
    user_persona: str = ""
    is_new_user: bool = False
    input_parts: tuple[ContentPart, ...] = ()
    edit_target_image: ContentPart | None = None
    attachments: tuple[AttachmentRef, ...] = ()
    reply_context: ReplyContext | None = None
    moderation_cleanup_paths: tuple[Path, ...] = ()


@dataclass(frozen=True)
class TurnResult:
    response_text: str
    output_files: tuple[str, ...] = ()
    output_file_descriptions: tuple[tuple[str, str], ...] = ()
    allowed_file_roots: tuple[str | Path, ...] = ()
    workspace_key: WorkspaceKey | None = None
    embed: EmbedSpec | None = None
    thread_request: ThreadRequest | None = None
    thread_close_request: ThreadCloseRequest | None = None
    terminal_handoff: TurnHandoff | None = None
    blocked_by_moderation: bool = False
    # Set by the Discord boundary when the turn produced a reply but no chunk
    # could be delivered (send_response swallows per-chunk HTTP failures).
    delivery_failed: bool = False


@dataclass(frozen=True)
class TurnDependencies:
    """Everything a turn needs, all of it supplied by the caller.

    No field is optional. Production (`app/turn_entry.py:build_turn_dependencies`)
    wires every one, so "this dependency is absent" is not a state the turn code
    should branch on. A missing wire is a construction error, caught by mypy and
    the dataclass, not a turn that silently runs with its denylist empty. Fields
    typed `| None` below carry a value that is genuinely nullable at runtime
    (memory is off, no usage store yet); they are still required arguments.

    Tests build one with `tests.helpers.make_turn_dependencies`, which supplies
    an inert default for every field.
    """

    context_manager: TurnContextManager
    provider: TurnProvider
    registry: object
    attachment_store: object
    workspace_dir: Path
    workspace_manager: WorkspaceManager
    workspace_locks: UserLocks
    llm_semaphore: asyncio.Semaphore
    memory_client: Any | None
    preference_store: MemoryPreferenceStore | None
    ensure_user_bank: EnsureUserBank
    recall_current_user_context: RecallCurrentUserContext
    skills_index_builder: SkillsIndexBuilder
    personal_skills_index_builder: SkillsIndexBuilder
    user_persona_loader: UserPersonaLoader
    # None when the application has no conversation store to count against.
    count_user_prior_messages: CountUserPriorMessages | None
    channel_pinned_tools: ChannelPinnedTools
    blocked_tools: BlockedTools
    tool_configs: ToolConfigs
    collect_turn_images: CollectTurnImages
    collect_reply_context: CollectReplyContext
    collect_turn_attachments: CollectTurnAttachments
    strip_mention: StripMention
    run_conversation: RunConversation
    chat_provider_resolver: ChatProviderResolver
    chat_model_name_resolver: ChatModelNameResolver
    persist_prepared_user_message: PersistPreparedUserMessage
    write_generated_assets: WriteGeneratedAssets
    compactor: Any | None
    activity_reporter: ActivityReporter | None
    # None whenever moderation is disabled (app/moderation.py:build_moderation_service).
    moderation_service: ModerationService | ModerationServiceLike | None
    usage_store: Any | None
    image_distillation_store: ImageDistillationCache | None
    model_config: Any | None
    resolved_model_name: str
    # Child mutable operations take their own privacy-barrier lease so a timed-out
    # worker can finish before an exclusive deletion begins.
    user_activity: UserActivityGuard
    stop_event: asyncio.Event | None = None


async def handle_turn(
    source: TurnPreparationInput,
    *,
    dependencies: TurnDependencies,
    preparation_config: TurnPreparationConfig,
    execution_config: TurnExecutionConfig,
) -> TurnResult | None:
    deadline = _deadline_from_timeout(execution_config.timeout_seconds)
    prepared_turn: TurnRequest | None = None
    usage_recorder: _TurnUsageRecorder | None = None
    try:
        prepared_turn = await _await_with_deadline(
            prepare_turn(
                source,
                dependencies=dependencies,
                config=preparation_config,
                deadline=deadline,
            ),
            deadline,
        )
        if prepared_turn is None:
            return None

        moderation_service = dependencies.moderation_service
        if moderation_service is not None and moderation_service.enabled:
            decision = await _await_with_deadline(
                moderation_service.check(
                    text=_input_moderation_text(prepared_turn),
                    images=_input_moderation_images(prepared_turn),
                    direction=Direction.INPUT,
                    user_id=prepared_turn.user_id,
                    channel_id=prepared_turn.channel_id,
                    thread_id=prepared_turn.thread_id,
                    trust_tier=prepared_turn.trust_tier.value,
                ),
                deadline,
            )
            if decision.blocked:
                return TurnResult(
                    response_text=moderation_service.refusal_for(
                        Direction.INPUT, error=_blocked_by_error(decision)
                    ),
                    blocked_by_moderation=True,
                )
            prepared_turn = _filter_turn_images_after_moderation(
                prepared_turn,
                checked_image_urls=getattr(decision, "checked_image_urls", None),
            )
            (
                prepared_turn,
                attachment_blocked,
                attachment_error,
            ) = await _moderate_input_attachments(
                prepared_turn,
                moderation_service=cast(ModerationServiceLike, moderation_service),
                deadline=deadline,
            )
            if attachment_blocked:
                return TurnResult(
                    response_text=moderation_service.refusal_for(
                        Direction.INPUT, error=attachment_error
                    ),
                    blocked_by_moderation=True,
                )

        prepared_turn = await _await_guarded_with_deadline(
            lambda: _hydrate_recalled_memories_for_turn(
                source,
                prepared_turn,
                dependencies,
                preparation_config,
            ),
            deadline=deadline,
            user_id=prepared_turn.user_id,
            activity_guard=dependencies.user_activity,
        )

        usage_recorder = _TurnUsageRecorder(
            dependencies,
            prepared_turn,
            turn_id=uuid.uuid4().hex,
        )
        # Before persisting, so the description lands on the row carrying the image
        # instead of being rebuilt every later turn, and after moderation so only
        # images that passed are described. Read-only, so no activity lease.
        captioned_turn = await _await_with_deadline(
            _caption_new_images(
                prepared_turn,
                dependencies,
                usage_recorder,
                deadline=deadline,
            ),
            deadline,
        )

        persist_prepared_user_message = dependencies.persist_prepared_user_message
        await _await_guarded_with_deadline(
            lambda: persist_prepared_user_message(source, captioned_turn),
            deadline=deadline,
            user_id=captioned_turn.user_id,
            activity_guard=dependencies.user_activity,
        )

        return await _await_with_deadline(
            execute_turn(
                captioned_turn,
                dependencies=dependencies,
                config=execution_config,
                deadline=deadline,
                usage_recorder=usage_recorder,
            ),
            deadline,
        )
    except ConversationTurnTimeoutError:
        if usage_recorder is not None:
            await usage_recorder.flush()
        if prepared_turn is not None:
            _clear_pending_response_artifacts(prepared_turn.context)
        log.warning(
            "Discord turn timed out after %s seconds during orchestration",
            execution_config.timeout_seconds,
        )
        return TurnResult(
            response_text=turn_timeout_response(execution_config.timeout_seconds),
        )
    finally:
        # The image payloads travel in-memory as base64 content parts; the files
        # written during preparation are needed by nothing after the turn, so
        # always remove them. Leaving them only on the moderation-pass path
        # leaked every attachment to disk (the workspace sweeper never visits
        # the attachment store directory).
        if prepared_turn is not None:
            await cleanup_prepared_moderation_artifacts(prepared_turn)


async def prepare_turn(
    source: TurnPreparationInput,
    *,
    dependencies: TurnDependencies,
    config: TurnPreparationConfig,
    deadline: float | None = None,
) -> TurnRequest | None:
    _raise_if_turn_deadline_expired(deadline)
    content = dependencies.strip_mention(source.raw_content, bot_user=source.bot_user)
    turn_attachments = dependencies.collect_turn_attachments(source.source_message)
    if (
        not content
        and not (config.max_turn_images > 0 and message_has_image_attachment(source.source_message))
        and not turn_attachments
    ):
        # Nothing to act on: no text, no image for the vision path, and no
        # importable attachment. A mention with only a non-image file must
        # still run a turn, because the file is surfaced as ephemeral turn context.
        return None

    ctx = await _await_guarded_with_deadline(
        lambda: dependencies.context_manager.build_turn_context(
            source.conversation_key,
            source.channel_name,
            before_discord_message_id=source.trigger_discord_message_id,
            owner_user_id=(
                source.conversation_owner_user_id
                if source.conversation_access_scope == "channel_shared"
                else source.conversation_owner_user_id or source.user_id
            ),
            access_scope=source.conversation_access_scope,
        ),
        deadline=deadline,
        user_id=source.user_id,
        activity_guard=dependencies.user_activity,
    )
    if config.max_turn_images <= 0:
        # The zero value is a hard image-input kill switch, including images
        # persisted by earlier turns and the captions stored alongside them.
        # Contexts are rebuilt fresh for every turn, so this only filters the
        # in-memory request and never rewrites history.
        ctx.messages = _messages_without_visual_context(ctx.messages)
    ctx.user_id = source.user_id
    ctx.user_name = source.user_name
    ctx.channel_name = source.channel_name
    ctx.add_participant(source.user_id, source.user_name)

    # Operator denylist (guild ∪ channel blocked_tools) for this turn. Stashed on
    # the context so the registry hides and masks these tools; never persisted, so
    # the fragments stay the source of truth and unblocking takes effect next turn.
    blocked = dependencies.blocked_tools()
    ctx.blocked_tools = blocked

    # Per-tool operator config (config/tools/<name>.md), resolved over each
    # tool's declared defaults. Read fresh beside the denylist and stashed the
    # same way, never persisted, so an edit applies on the next turn.
    ctx.tool_configs = dependencies.tool_configs()

    # Guild- and channel-pinned searchable tools join the activated set here,
    # before execute_turn takes its persistence baseline, so they are visible and
    # dispatchable this turn but never written to conversation_activated_tools:
    # the guild/channel fragments stay the single source of truth. A blocked name
    # is never activated (the denylist wins over a pin).
    pinned = dependencies.channel_pinned_tools() - blocked
    if pinned:
        ctx.activated_tools |= pinned

    _raise_if_turn_deadline_expired(deadline)

    collect_turn_images = dependencies.collect_turn_images

    history_messages = ctx.get_history()
    preparation_provider = _provider_for_image_need(
        dependencies,
        needs_image_input=_messages_contain_image_parts(history_messages),
    )
    images_supported = ProviderCapability.IMAGE_INPUT in preparation_provider.capabilities
    history_hashes = (
        await asyncio.to_thread(image_byte_hashes, history_messages) if images_supported else set()
    )
    recent_image_lookback = _recent_image_lookback_for_turn(
        source,
        config,
        has_history=bool(history_messages),
    )
    turn_images: TurnImages | None = None
    try:
        turn_images = await _await_with_deadline(
            collect_turn_images(
                source.source_message,
                store=dependencies.attachment_store,
                conversation_key=ctx.key,
                detail=config.image_detail,
                images_supported=images_supported,
                history_hashes=history_hashes,
                lookback=recent_image_lookback,
                max_images=config.max_turn_images,
                include_reply_images=False,
            ),
            deadline,
        )
        reply_image_budget = max(0, config.max_turn_images - len(turn_images.vision_parts))
        reply_context = await _await_with_deadline(
            _collect_reply_context(
                source,
                dependencies,
                config,
                context=ctx,
                images_supported=images_supported,
                history_hashes=history_hashes,
                current_hashes=(set(turn_images.vision_hashes) if images_supported else set()),
                max_images=reply_image_budget,
                prefetched_images=turn_images.reply_images,
            ),
            deadline,
        )

        is_new_user = await _await_with_deadline(
            _is_new_user_for_turn(source, dependencies, config),
            deadline,
        )
        user_persona = await _await_with_deadline(
            _load_user_persona_for_turn(source.user_id, dependencies),
            deadline,
        )
        skills_index = dependencies.skills_index_builder()
        personal_skills_index = dependencies.personal_skills_index_builder()
        _raise_if_turn_deadline_expired(deadline)

        return TurnRequest(
            content=content,
            context=ctx,
            trust_tier=source.trust_tier,
            user_id=source.user_id,
            user_name=source.user_name,
            guild_id=source.guild_id,
            channel_id=source.channel_id,
            thread_id=source.thread_id,
            channel_name=source.channel_name,
            platform_member=getattr(source.source_message, "author", None),
            parent_channel_id=source.parent_channel_id,
            guild_name=source.guild_name,
            trigger_discord_message_id=source.trigger_discord_message_id,
            is_new_user=is_new_user,
            skills_index=skills_index,
            personal_skills_index=personal_skills_index,
            user_persona=user_persona,
            input_parts=tuple(turn_images.vision_parts),
            edit_target_image=turn_images.edit_target,
            attachments=tuple(turn_attachments),
            reply_context=reply_context,
            moderation_cleanup_paths=tuple(turn_images.cleanup_paths),
        )
    except BaseException:
        if turn_images is not None:
            await _cleanup_moderation_paths(turn_images.cleanup_paths)
        raise


async def _is_new_user_for_turn(
    source: TurnPreparationInput,
    dependencies: TurnDependencies,
    config: TurnPreparationConfig,
) -> bool:
    """Whether to inject the new-user onboarding note: the user has fewer prior messages
    with the bot than the configured threshold. Fails closed (no note) on any error."""
    threshold = config.new_user_onboarding_turns
    count = dependencies.count_user_prior_messages
    if threshold <= 0 or count is None:
        return False
    try:
        # Only the threshold check matters, so cap the count (and the scan) at the threshold.
        prior = await count(source.user_id, source.trigger_discord_message_id or None, threshold)
    except Exception:
        log.warning("new-user message count failed; skipping onboarding note", exc_info=True)
        return False
    return prior < threshold


def _recent_image_lookback_for_turn(
    source: TurnPreparationInput,
    config: TurnPreparationConfig,
    *,
    has_history: bool,
) -> int:
    """Gate ambient recent-image lookup to turns with explicit context.

    A fresh @mention creates a new logical root and should not inherit the
    speaker's last image from unrelated channel traffic. Replies and continued
    roots already pull surrounding conversation context, so the configured
    recent-image window remains available there.
    """
    if config.recent_image_lookback <= 0:
        return 0
    if source.referenced_message_id is not None or has_history:
        return config.recent_image_lookback
    return 0


async def _load_user_persona_for_turn(
    user_id: str,
    dependencies: TurnDependencies,
) -> str:
    try:
        return await dependencies.user_persona_loader(user_id)
    except Exception:
        log.exception("Failed to load persona for user %s", user_id)
        return ""


async def cleanup_prepared_moderation_artifacts(turn: TurnRequest) -> None:
    await _cleanup_moderation_paths(turn.moderation_cleanup_paths)


async def _cleanup_moderation_paths(paths: Sequence[Path]) -> None:
    await cleanup_attachment_paths(paths)


def _raise_if_turn_deadline_expired(deadline: float | None) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        raise ConversationTurnTimeoutError


async def _hydrate_recalled_memories_for_turn(
    source: TurnPreparationInput,
    turn: TurnRequest,
    dependencies: TurnDependencies,
    config: TurnPreparationConfig,
) -> TurnRequest:
    await _ensure_memory_bank_if_enabled(source, dependencies)
    recalled_memories = await _recall_user_context(
        source,
        dependencies,
        config,
        content=turn.content,
        context=turn.context,
    )
    if not recalled_memories:
        return turn
    return replace(turn, recalled_memories=recalled_memories)


async def _ensure_memory_bank_if_enabled(
    source: TurnPreparationInput,
    dependencies: TurnDependencies,
) -> None:
    if dependencies.memory_client is None or dependencies.preference_store is None:
        return
    try:
        async with user_memory_mutation(source.user_id):
            if await dependencies.preference_store.is_memory_enabled(source.user_id):
                await dependencies.ensure_user_bank(
                    dependencies.memory_client,
                    source.user_id,
                    source.user_name,
                )
    except Exception:
        log.exception("Failed to check memory preference for user %s", source.user_id)


async def _recall_user_context(
    source: TurnPreparationInput,
    dependencies: TurnDependencies,
    config: TurnPreparationConfig,
    *,
    content: str,
    context: ConversationContext,
) -> str:
    return await dependencies.recall_current_user_context(
        memory_client=dependencies.memory_client,
        preference_store=dependencies.preference_store,
        user_id=source.user_id,
        user_message=content,
        context=context,
        guild_id=source.guild_id,
        budget=config.user_memory_recall_budget,
        max_tokens=config.user_memory_recall_max_tokens,
        types=list(config.user_memory_recall_types),
    )


async def _collect_reply_context(
    source: TurnPreparationInput,
    dependencies: TurnDependencies,
    config: TurnPreparationConfig,
    *,
    context: ConversationContext,
    images_supported: bool,
    history_hashes: set[str],
    current_hashes: set[str],
    max_images: int,
    prefetched_images: Sequence[CollectedImage] | None = None,
) -> ReplyContext | None:
    if source.referenced_message_id is None:
        return None
    if await dependencies.context_manager.has_loaded_message(
        context,
        source.referenced_message_id,
    ):
        return None
    return await dependencies.collect_reply_context(
        source.source_message,
        bot_user=source.bot_user,
        store=dependencies.attachment_store,
        conversation_key=context.key,
        detail=config.image_detail,
        images_supported=images_supported,
        history_hashes=history_hashes,
        current_hashes=current_hashes,
        max_images=max_images,
        prefetched_images=prefetched_images,
        allow_bot_authored=source.allow_bot_authored_reply_context,
    )


class _TurnUsageRecorder:
    """Persist each call once, including calls completed by detached tools."""

    def __init__(
        self,
        dependencies: TurnDependencies,
        turn: TurnRequest,
        *,
        turn_id: str,
    ) -> None:
        self.calls: list[LLMUsageCall] = []
        self.turn_id = turn_id
        self._store = dependencies.usage_store
        self._model_config = dependencies.model_config
        self._user_id = turn.user_id
        self._user_name = turn.user_name
        self._channel_id = turn.channel_id
        self._guild_id = turn.guild_id
        self._persisted_count = 0
        self._lock = asyncio.Lock()

    def absorb(self, calls: Sequence[LLMUsageCall]) -> None:
        """Merge a custom runner's result without duplicating the shared sink."""

        existing = {id(call) for call in self.calls}
        self.calls.extend(call for call in calls if id(call) not in existing)

    async def record(self, call: LLMUsageCall) -> None:
        """Append and durably record a nested model call before its tool returns."""

        self.calls.append(call)
        await self.flush()

    async def flush(self) -> None:
        async with self._lock:
            end = len(self.calls)
            if self._persisted_count >= end:
                return
            if self._store is None:
                self._persisted_count = end
                return
            pending = self.calls[self._persisted_count : end]
            try:
                await self._store.record_turn(
                    user_id=self._user_id,
                    user_name=self._user_name,
                    channel_id=self._channel_id,
                    guild_id=self._guild_id,
                    calls=[price_usage_call(call, self._model_config) for call in pending],
                    turn_id=self.turn_id,
                )
            except Exception:
                log.warning("usage ledger write failed", exc_info=True)
                return
            self._persisted_count = end


async def execute_turn(
    turn: TurnRequest,
    *,
    dependencies: TurnDependencies,
    config: TurnExecutionConfig,
    deadline: float | None = None,
    usage_recorder: _TurnUsageRecorder | None = None,
) -> TurnResult:
    run_conversation = dependencies.run_conversation
    if deadline is None:
        deadline = _deadline_from_timeout(config.timeout_seconds)

    if usage_recorder is None:
        usage_recorder = _TurnUsageRecorder(
            dependencies,
            turn,
            turn_id=uuid.uuid4().hex,
        )
    distilled = await _distill_images_for_nonvision_model(
        turn,
        dependencies,
        usage_recorder,
        deadline=deadline,
    )
    if distilled is None:
        run_dependencies = _dependencies_for_prepared_turn_provider(turn, dependencies)
    else:
        turn, run_dependencies = distilled

    moderation_service = dependencies.moderation_service
    activity_reporter = dependencies.activity_reporter
    if (
        _should_moderate_output(moderation_service, turn.trust_tier)
        and activity_reporter is not None
        and isinstance(activity_reporter, SupportsNarrationSteps | SupportsPlanUpdates)
    ):
        activity_reporter = _ModeratedActivityReporter(
            activity_reporter,
            moderation_service=moderation_service,
            user_id=turn.user_id,
            channel_id=turn.channel_id,
            thread_id=turn.thread_id,
            trust_tier=turn.trust_tier,
        )

    activation_baseline = set(turn.context.activated_tools)
    usage_sink = usage_recorder.calls
    turn_id = usage_recorder.turn_id
    try:
        run_result = await _await_with_deadline(
            run_conversation(
                request=ConversationRunRequest(
                    user_message=turn.content,
                    context=turn.context,
                    trust_tier=turn.trust_tier,
                    user_name=turn.user_name,
                    user_id=turn.user_id,
                    provider=cast(LLMProvider, run_dependencies.provider),
                    registry=cast(ToolRegistry, run_dependencies.registry),
                    max_iterations=config.max_iterations,
                    max_tokens=config.max_tokens,
                    temperature=config.temperature,
                    channel_name=turn.channel_name,
                    platform_member=turn.platform_member,
                    guild_id=turn.guild_id,
                    guild_name=turn.guild_name,
                    channel_id=turn.channel_id,
                    thread_id=turn.thread_id,
                    parent_channel_id=turn.parent_channel_id,
                    trigger_discord_message_id=turn.trigger_discord_message_id,
                    bot_name=config.bot_name,
                    command_template=config.command_template,
                    recalled_memories=turn.recalled_memories,
                    skills_index=turn.skills_index,
                    personal_skills_index=turn.personal_skills_index,
                    user_persona=turn.user_persona,
                    is_new_user=turn.is_new_user,
                    llm_semaphore=run_dependencies.llm_semaphore,
                    input_parts=list(turn.input_parts),
                    edit_target_image=turn.edit_target_image,
                    attachments=list(turn.attachments),
                    reply_context=turn.reply_context,
                    provider_state={},
                    compactor=run_dependencies.compactor,
                    activity_reporter=activity_reporter,
                    usage_store=run_dependencies.usage_store,
                    timeout_seconds=config.timeout_seconds,
                    thread_handoff_suggest_after_tool_calls=(
                        config.thread_handoff_suggest_after_tool_calls
                    ),
                    deadline_monotonic=deadline,
                    usage_sink=usage_sink,
                    record_usage_call=usage_recorder.record,
                    turn_id=turn_id,
                    user_activity=run_dependencies.user_activity,
                    stop_event=run_dependencies.stop_event,
                )
            ),
            deadline,
        )
    except ConversationTurnTimeoutError:
        # The hard outer wall can win the scheduling race with core's cooperative
        # timeout result (or detach a cancellation-suppressing finalizer). The shared
        # sink is updated immediately after each completed response, so preserve those
        # already-billed calls instead of dropping the entire turn's accounting.
        usage = UsageBreakdown()
        for call in usage_sink:
            usage = usage + call.usage
        handoff = turn.context.pending_terminal_handoff
        run_result = ConversationRunResult(
            text=(
                handoff.response_text
                if handoff is not None
                else turn_timeout_response(config.timeout_seconds)
            ),
            usage=usage,
            llm_calls=list(usage_sink),
            iterations=len(usage_sink),
            timed_out=handoff is None,
            turn_id=turn_id,
            termination_reason="completed" if handoff is not None else "timed_out",
            terminal_handoff=handoff,
        )

    usage_recorder.absorb(run_result.llm_calls)
    if run_result.timed_out:
        # The ReAct loop may have completed and billed one or more provider calls
        # before a later model/tool iteration exhausted the deadline. The expired
        # turn deadline must not discard that already-incurred usage.
        await usage_recorder.flush()
        _clear_pending_response_artifacts(turn.context)
        return TurnResult(response_text=run_result.text)

    # Ledger persistence is not response work. Keep it outside the delivery
    # budget without allowing its latency to consume whatever response budget
    # remained when the provider completed.
    usage_write_started = time.monotonic()
    await usage_recorder.flush()
    if deadline is not None:
        deadline += time.monotonic() - usage_write_started

    if run_result.terminal_handoff is not None:
        thread_request = turn.context.pending_thread_request
        _clear_pending_response_artifacts(turn.context)
        return TurnResult(
            response_text=run_result.text,
            workspace_key=workspace_owner_key(turn.user_id, turn.guild_id),
            thread_request=thread_request,
            terminal_handoff=run_result.terminal_handoff,
        )

    await _stage_pending_response_files(
        turn,
        run_dependencies,
        deadline=deadline,
    )

    if _should_moderate_output(moderation_service, turn.trust_tier):
        assert moderation_service is not None
        # Generic queued workspace file bodies are delivery artifacts, not
        # assistant-authored content. Their optional Discord descriptions are
        # assistant-visible text, so screen those with the reply plus explicitly
        # supported first-class modalities: native generated assets and the embed
        # (including its owned image attachment). The embed's text is assembled
        # from embed= inside the service.
        decision = await _await_with_deadline(
            moderation_service.check(
                text=_output_moderation_text(
                    run_result.text,
                    turn.context.pending_output_file_descriptions,
                ),
                direction=Direction.OUTPUT,
                generated_assets=run_result.generated_assets,
                embed=turn.context.pending_embed,
                embed_attachment=turn.context.pending_embed_attachment,
                user_id=turn.user_id,
                channel_id=turn.channel_id,
                thread_id=turn.thread_id,
                trust_tier=turn.trust_tier.value,
            ),
            deadline,
        )
        if decision.blocked:
            _clear_pending_response_artifacts(turn.context)
            return TurnResult(
                response_text=moderation_service.refusal_for(
                    Direction.OUTPUT, error=_blocked_by_error(decision)
                ),
                blocked_by_moderation=True,
            )

    # Explicit browse_tools loads are unioned in because the baseline already
    # contains channel-pinned names: without it, loading a pinned tool would
    # never persist and unpinning would silently deactivate it mid-conversation.
    newly_activated = (turn.context.activated_tools - activation_baseline) | (
        turn.context.explicitly_loaded_tools & turn.context.activated_tools
    )
    if newly_activated:
        await _await_guarded_with_deadline(
            lambda: dependencies.context_manager.add_activated_tools(
                turn.context,
                newly_activated,
            ),
            deadline=deadline,
            user_id=turn.user_id,
            activity_guard=dependencies.user_activity,
        )

    if run_result.generated_assets:
        write_generated_assets = dependencies.write_generated_assets

        async def write_assets() -> tuple[Path, list[Path]]:
            locks = dependencies.workspace_locks
            async with locks.activity(workspace_owner_key(turn.user_id, turn.guild_id)):
                root = dependencies.workspace_manager.generated_job_dir(
                    turn.context.key,
                    f"native-{uuid.uuid4().hex}",
                    owner_user_id=turn.user_id,
                )
                paths = await asyncio.to_thread(
                    write_generated_assets,
                    run_result.generated_assets,
                    output_dir=root,
                )
                return root, paths

        def cleanup_detached_assets(result: tuple[Path, list[Path]]) -> None:
            generated_root, _paths = result
            shutil.rmtree(generated_root, ignore_errors=True)

        # Offload the base64 decode + file writes (multi-MB for native image
        # output) off the event loop.
        generated_root, asset_paths = await _await_guarded_with_deadline(
            write_assets,
            deadline=deadline,
            user_id=turn.user_id,
            activity_guard=run_dependencies.user_activity,
            on_detached_result=cleanup_detached_assets,
        )
        turn.context.pending_output_files.extend(str(path) for path in asset_paths)
        turn.context.pending_allowed_file_roots.append(str(generated_root.resolve()))
    else:
        asset_paths = []

    pending_files = list(turn.context.pending_output_files)
    pending_descriptions = dict(turn.context.pending_output_file_descriptions)
    pending_roots: list[str | Path] = list(turn.context.pending_allowed_file_roots)
    embed = turn.context.pending_embed
    embed_attachment = turn.context.pending_embed_attachment
    thread_request = turn.context.pending_thread_request
    thread_close_request = turn.context.pending_thread_close_request
    # Materialize the embed-owned image onto the file rails here, at the single
    # boundary, so a replaced/abandoned embed never leaks a stale attachment.
    # Dedup by exact path so a file that is both embedded and queue_file'd is
    # only attached once.
    if embed_attachment is not None and embed_attachment.path not in pending_files:
        pending_files.append(embed_attachment.path)
        if embed_attachment.root not in pending_roots:
            pending_roots.append(embed_attachment.root)

    turn.context.pending_output_files.clear()
    turn.context.pending_output_file_descriptions.clear()
    turn.context.pending_allowed_file_roots.clear()
    turn.context.pending_embed = None
    turn.context.pending_embed_attachment = None
    turn.context.pending_thread_request = None
    turn.context.pending_thread_close_request = None
    try:
        _raise_if_turn_deadline_expired(deadline)
    except ConversationTurnTimeoutError:
        if asset_paths:
            shutil.rmtree(generated_root, ignore_errors=True)
        raise

    return TurnResult(
        response_text=run_result.text,
        output_files=tuple(pending_files),
        output_file_descriptions=tuple(
            (path, pending_descriptions[path])
            for path in pending_files
            if path in pending_descriptions
        ),
        allowed_file_roots=tuple(pending_roots),
        workspace_key=workspace_owner_key(turn.user_id, turn.guild_id),
        embed=embed,
        thread_request=thread_request,
        thread_close_request=thread_close_request,
        terminal_handoff=run_result.terminal_handoff,
    )


async def _stage_pending_response_files(
    turn: TurnRequest,
    dependencies: TurnDependencies,
    *,
    deadline: float | None,
) -> None:
    """Snapshot queued attachments before moderation and delivery.

    Copying under the shared workspace activity lease freezes the delivery bytes,
    preserves containment, and gives embed-image moderation the same owned image
    that Discord later receives. Generic queued files are not moderation inputs.
    """

    manager = dependencies.workspace_manager
    locks = dependencies.workspace_locks
    context = turn.context
    files = list(context.pending_output_files)
    descriptions = dict(context.pending_output_file_descriptions)
    embed_attachment = context.pending_embed_attachment
    if embed_attachment is not None and embed_attachment.path not in files:
        files.append(embed_attachment.path)
    if not files:
        return

    async def stage() -> tuple[list[str], str, Any | None, dict[str, str]]:
        allowed_roots = list(context.pending_allowed_file_roots)
        if embed_attachment is not None and embed_attachment.root not in allowed_roots:
            allowed_roots.append(embed_attachment.root)
        async with locks.activity(workspace_owner_key(turn.user_id, turn.guild_id)):
            return await asyncio.to_thread(
                _stage_response_files_sync,
                manager,
                context.key,
                turn.user_id,
                files,
                allowed_roots,
                embed_attachment,
                descriptions,
            )

    (
        staged_files,
        staged_root,
        staged_embed,
        staged_descriptions,
    ) = await _await_guarded_with_deadline(
        stage,
        deadline=deadline,
        user_id=turn.user_id,
        activity_guard=dependencies.user_activity,
    )
    context.pending_output_files[:] = staged_files
    context.pending_output_file_descriptions = staged_descriptions
    context.pending_allowed_file_roots[:] = [staged_root]
    context.pending_embed_attachment = staged_embed


def _stage_response_files_sync(
    workspace_manager: Any,
    context_key: str,
    user_id: str,
    files: list[str],
    allowed_roots: Sequence[str | Path],
    embed_attachment: Any | None,
    descriptions: Mapping[str, str],
) -> tuple[list[str], str, Any | None, dict[str, str]]:
    roots = [Path(root).resolve(strict=False) for root in allowed_roots]
    job_dir = workspace_manager.generated_job_dir(
        context_key,
        f"delivery-{uuid.uuid4().hex}",
        owner_user_id=user_id,
    )
    staged: list[str] = []
    staged_descriptions: dict[str, str] = {}
    mapping: dict[str, Path] = {}
    try:
        for index, raw in enumerate(dict.fromkeys(files), start=1):
            source = Path(raw)
            if source.is_symlink() or not source.is_file():
                raise ValueError("Queued output file is unavailable")
            resolved = source.resolve(strict=True)
            if not any(resolved.is_relative_to(root) for root in roots):
                raise ValueError("Queued output file is outside its allowed workspace")
            destination = job_dir / source.name
            if destination.exists():
                destination = job_dir / f"{index}-{source.name}"
            shutil.copyfile(resolved, destination)
            destination.chmod(0o600)
            destination_text = str(destination)
            staged.append(destination_text)
            mapping[str(source)] = destination
            description = descriptions.get(str(source))
            if description:
                staged_descriptions[destination_text] = description

        staged_embed = embed_attachment
        if embed_attachment is not None:
            destination = mapping[str(Path(embed_attachment.path))]
            staged_embed = replace(
                embed_attachment,
                path=str(destination),
                root=str(job_dir.resolve()),
                filename=destination.name,
            )
        return staged, str(job_dir.resolve()), staged_embed, staged_descriptions
    except Exception:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise


def _parts_carry_image_caption(parts: Sequence[ContentPart]) -> bool:
    return any(
        part.type is ContentPartType.TEXT and is_image_caption(part.text or "") for part in parts
    )


def _images_for_distillation(
    turn: TurnRequest,
) -> list[tuple[str, str, ContentPart]]:
    # Skip images that already sit beside a caption: describing them again would put
    # the same picture in the request twice, once stored and once freshly distilled.
    candidates: list[tuple[str, ContentPart]] = []
    for message_index, message in enumerate(turn.context.get_history(), start=1):
        if _parts_carry_image_caption(message.content):
            continue
        for part in message.content:
            if part.type is ContentPartType.IMAGE:
                candidates.append((f"prior conversation message {message_index}", part))
    if not _parts_carry_image_caption(turn.input_parts):
        for part in turn.input_parts:
            if part.type is ContentPartType.IMAGE:
                candidates.append(("current user message", part))
    if turn.reply_context is not None:
        for part in turn.reply_context.image_parts:
            if part.type is ContentPartType.IMAGE:
                candidates.append(("message being replied to", part))

    images: list[tuple[str, str, ContentPart]] = []
    seen_hashes: set[str] = set()
    for source, part in candidates:
        image_hash = image_part_hash(part)
        if image_hash is None:
            raise ValueError("image input could not be hashed")
        if image_hash in seen_hashes:
            continue
        seen_hashes.add(image_hash)
        images.append((image_hash, source, part))
    return images


def _image_distillation_cache_key(
    provider_model: str,
    images: Sequence[tuple[str, str, ContentPart]],
) -> str:
    digest = hashlib.sha256()
    digest.update(f"v{IMAGE_CAPTION_PROMPT_VERSION}\0{provider_model}\0".encode())
    for image_hash, _source, _part in images:
        digest.update(image_hash.encode())
        digest.update(b"\0")
    return digest.hexdigest()


async def _request_image_distillation(
    turn: TurnRequest,
    dependencies: TurnDependencies,
    provider: TurnProvider,
    images: Sequence[tuple[str, str, ContentPart]],
    *,
    deadline: float | None,
) -> Any:
    parts = [
        ContentPart.from_text("Produce the visual-context transcription for these numbered images.")
    ]
    for index, (_image_hash, source, image_part) in enumerate(images, start=1):
        parts.append(ContentPart.from_text(f"Image {index} ({source}):"))
        parts.append(image_part)

    request = ProviderRequest(
        conversation_id=turn.context.db_conversation_id,
        system_prompt=IMAGE_CAPTION_SYSTEM_PROMPT,
        messages=[],
        current_user_parts=parts,
        tools=[],
        max_tokens=IMAGE_CAPTION_MAX_TOKENS,
        temperature=None,
        requested_capabilities={ProviderCapability.IMAGE_INPUT},
        reasoning_enabled=False,
    )
    llm_provider = cast(LLMProvider, provider)

    async def _call() -> Any:
        async with dependencies.llm_semaphore:
            return await llm_provider.run_turn(request)

    return await _await_with_deadline(_call(), deadline)


def _turn_with_distilled_images(
    turn: TurnRequest,
    *,
    description: str | None,
) -> TurnRequest:
    # None when every image already carries a persisted caption: nothing new to say,
    # but the image parts still have to go so the turn can run on the text model.
    context = replace(
        turn.context,
        messages=_messages_without_image_parts(turn.context.get_history()),
    )
    filtered_input_parts = [
        part for part in turn.input_parts if part.type is not ContentPartType.IMAGE
    ]
    if description is not None:
        filtered_input_parts.append(ContentPart.from_text(format_image_caption(description)))
    input_parts = tuple(filtered_input_parts)
    reply_context = turn.reply_context
    if reply_context is not None and reply_context.image_parts:
        reply_context = replace(reply_context, image_parts=())
    return replace(
        turn,
        context=context,
        input_parts=input_parts,
        reply_context=reply_context,
        edit_target_image=None,
    )


async def _describe_images(
    turn: TurnRequest,
    dependencies: TurnDependencies,
    usage_recorder: _TurnUsageRecorder,
    image_provider: TurnProvider,
    images: Sequence[tuple[str, str, ContentPart]],
    *,
    deadline: float | None,
) -> str:
    """Describe `images`, reusing this conversation's cached description if present.

    Shared by the ingest pass and the in-flight pass that handles what it missed.
    """
    cache = dependencies.image_distillation_store
    if cache is None:
        raise RuntimeError("image distillation cache is not configured")
    cache_key = _image_distillation_cache_key(image_provider.model, images)
    try:
        cached = await cache.get(turn.context.db_conversation_id, cache_key)
    except Exception:
        log.warning("Image distillation cache read failed", exc_info=True)
        cached = None
    if cached is not None:
        description, _cached_model = cached
        return description

    response = await _request_image_distillation(
        turn,
        dependencies,
        image_provider,
        images,
        deadline=deadline,
    )
    description = (response.content or "").strip()
    if not description:
        raise RuntimeError("image distillation returned no description")
    provider_model = response.model or image_provider.model
    await usage_recorder.record(
        LLMUsageCall(
            model=provider_model,
            pricing_model=response.pricing_model or image_provider.model,
            role="image_distillation",
            usage=normalize_usage(response.usage),
        )
    )
    try:
        await cache.set(
            turn.context.db_conversation_id,
            cache_key,
            model_name=provider_model,
            prompt_version=IMAGE_CAPTION_PROMPT_VERSION,
            description=description,
        )
    except Exception:
        log.warning("Image distillation cache write failed", exc_info=True)
    return description


async def _caption_new_images(
    turn: TurnRequest,
    dependencies: TurnDependencies,
    usage_recorder: _TurnUsageRecorder,
    *,
    deadline: float | None,
) -> TurnRequest:
    """Describe this message's own images so the caption can be stored beside them.

    Storage evicts the oldest image parts once a conversation passes its image cap,
    and the caption on the row is what is left. Anything older was captioned when it
    arrived. Returns the turn unchanged when there is nothing to describe or the call
    fails, leaving `_distill_images_for_nonvision_model` to handle it in flight.
    """
    resolver = dependencies.chat_provider_resolver
    if dependencies.image_distillation_store is None:
        return turn
    # A falsy conversation id means the row is never written, so the description
    # could not be stored and its cache row would have nothing to hang off.
    if not turn.context.db_conversation_id:
        return turn

    images: list[tuple[str, str, ContentPart]] = []
    for part in turn.input_parts:
        if part.type is not ContentPartType.IMAGE:
            continue
        image_hash = image_part_hash(part)
        if image_hash is None:
            return turn
        images.append((image_hash, "current user message", part))
    if not images:
        return turn

    if ProviderCapability.IMAGE_INPUT in resolver(images=False).capabilities:
        return turn
    image_provider = resolver(images=True)
    if ProviderCapability.IMAGE_INPUT not in image_provider.capabilities:
        return turn

    # Best effort: bound the entire caption/cache path early enough that a stalled
    # vision model cannot consume the budget the transcript write below needs.
    caption_deadline = _deadline_from_timeout(_INGEST_IMAGE_CAPTION_TIMEOUT_SECONDS)
    if deadline is not None and caption_deadline is not None:
        latest_caption_deadline = deadline - _INGEST_TRANSCRIPT_PERSISTENCE_RESERVE_SECONDS
        if latest_caption_deadline <= time.monotonic():
            return turn
        caption_deadline = min(latest_caption_deadline, caption_deadline)

    try:
        description = await _await_with_deadline(
            _describe_images(
                turn,
                dependencies,
                usage_recorder,
                image_provider,
                images,
                deadline=caption_deadline,
            ),
            caption_deadline,
        )
    except Exception:
        log.warning(
            "Image captioning failed; leaving the images to in-flight distillation",
            exc_info=True,
        )
        return turn

    caption = ContentPart.from_text(format_image_caption(description))
    return replace(turn, input_parts=(*turn.input_parts, caption))


async def _distill_images_for_nonvision_model(
    turn: TurnRequest,
    dependencies: TurnDependencies,
    usage_recorder: _TurnUsageRecorder,
    *,
    deadline: float | None,
) -> tuple[TurnRequest, TurnDependencies] | None:
    cache = dependencies.image_distillation_store
    resolver = dependencies.chat_provider_resolver
    if cache is None or not _prepared_turn_needs_image_input(turn):
        return None

    text_provider = resolver(images=False)
    if ProviderCapability.IMAGE_INPUT in text_provider.capabilities:
        return None
    image_provider = resolver(images=True)
    if ProviderCapability.IMAGE_INPUT not in image_provider.capabilities:
        return None

    try:
        images = _images_for_distillation(turn)
        description = (
            await _describe_images(
                turn,
                dependencies,
                usage_recorder,
                image_provider,
                images,
                deadline=deadline,
            )
            if images
            else None
        )
        distilled_turn = _turn_with_distilled_images(turn, description=description)
        resolved_model_name = dependencies.chat_model_name_resolver(images=False)
        return distilled_turn, replace(
            dependencies,
            provider=text_provider,
            resolved_model_name=resolved_model_name,
        )
    except Exception:
        log.warning(
            "Image distillation failed; preserving image-capable routing",
            exc_info=True,
        )
        return None


def _dependencies_for_prepared_turn_provider(
    turn: TurnRequest,
    dependencies: TurnDependencies,
) -> TurnDependencies:
    needs_image_input = _prepared_turn_needs_image_input(turn)
    provider = _provider_for_image_need(
        dependencies,
        needs_image_input=needs_image_input,
    )
    if provider is dependencies.provider:
        return dependencies

    resolved_model_name = (
        dependencies.chat_model_name_resolver(images=True)
        if needs_image_input
        else dependencies.resolved_model_name
    )
    return replace(
        dependencies,
        provider=provider,
        resolved_model_name=resolved_model_name,
    )


def _provider_for_image_need(
    dependencies: TurnDependencies,
    *,
    needs_image_input: bool,
) -> TurnProvider:
    if not needs_image_input:
        return dependencies.provider
    if ProviderCapability.IMAGE_INPUT in dependencies.provider.capabilities:
        return dependencies.provider
    return dependencies.chat_provider_resolver(images=True)


def _prepared_turn_needs_image_input(turn: TurnRequest) -> bool:
    if _messages_contain_image_parts(turn.context.get_history()):
        return True
    if _content_parts_contain_image(turn.input_parts):
        return True
    return turn.reply_context is not None and _content_parts_contain_image(
        turn.reply_context.image_parts
    )


def _messages_contain_image_parts(messages: Sequence[ConversationMessage]) -> bool:
    return any(_content_parts_contain_image(message.content) for message in messages)


def _messages_without_visual_context(
    messages: Sequence[ConversationMessage],
) -> list[ConversationMessage]:
    """Drop images and the stored captions that stand in for them.

    A zero image budget kills image input, so the description goes with the pixels.
    """
    filtered_messages: list[ConversationMessage] = []
    for message in messages:
        filtered_content = [
            part
            for part in message.content
            if part.type is not ContentPartType.IMAGE
            and not (part.type is ContentPartType.TEXT and is_image_caption(part.text or ""))
        ]
        if not filtered_content:
            continue
        filtered_messages.append(
            message
            if len(filtered_content) == len(message.content)
            else replace(message, content=filtered_content)
        )
    return filtered_messages


def _messages_without_image_parts(
    messages: Sequence[ConversationMessage],
) -> list[ConversationMessage]:
    filtered_messages: list[ConversationMessage] = []
    for message in messages:
        filtered_content = [
            part for part in message.content if part.type is not ContentPartType.IMAGE
        ]
        if not filtered_content:
            continue
        filtered_messages.append(
            message
            if len(filtered_content) == len(message.content)
            else replace(message, content=filtered_content)
        )
    return filtered_messages


def _content_parts_contain_image(parts: Sequence[ContentPart]) -> bool:
    return any(part.type is ContentPartType.IMAGE for part in parts)


def _output_moderation_text(text: str, descriptions: Mapping[str, str]) -> str:
    if not descriptions:
        return text
    attachment_text = "\n".join(f"- {value}" for value in descriptions.values())
    prefix = f"{text}\n\n" if text else ""
    return f"{prefix}Attachment descriptions:\n{attachment_text}"


def _should_moderate_output(
    moderation_service: ModerationService | ModerationServiceLike | None,
    trust_tier: TrustTier,
) -> bool:
    if moderation_service is None or not moderation_service.enabled:
        return False
    exempt_tier = _output_moderation_exempt_tier(moderation_service)
    return exempt_tier is None or trust_tier < exempt_tier


def _output_moderation_exempt_tier(
    moderation_service: ModerationService | ModerationServiceLike,
) -> TrustTier | None:
    value = getattr(moderation_service, "output_exempt_tier", None)
    return value if isinstance(value, TrustTier) else None


class _ModeratedActivityReporter:
    def __init__(
        self,
        delegate: Any,
        *,
        moderation_service: ModerationServiceLike,
        user_id: str,
        channel_id: str,
        thread_id: str | None,
        trust_tier: TrustTier,
    ) -> None:
        self._delegate = delegate
        self._moderation_service = moderation_service
        self._user_id = user_id
        self._channel_id = channel_id
        self._thread_id = thread_id
        self._trust_tier = trust_tier

    async def __call__(self, update: ActivityUpdate) -> None:
        await self._delegate(update)

    async def commit_step(self, narration: str, tool_names: list[str]) -> None:
        # The wrapper is created for reporters supporting either protocol, so the
        # delegate is not guaranteed to take narration steps.
        if not isinstance(self._delegate, SupportsNarrationSteps):
            return
        text = (narration or "").strip()
        if text:
            try:
                decision = await self._moderation_service.check(
                    text=text,
                    direction=Direction.OUTPUT,
                    user_id=self._user_id,
                    channel_id=self._channel_id,
                    thread_id=self._thread_id,
                    trust_tier=self._trust_tier.value,
                )
            except Exception:
                log.warning("Narration moderation failed; suppressing narration", exc_info=True)
                return
            if decision.blocked:
                return
        await self._delegate.commit_step(narration, list(tool_names))

    async def update_plan(self, steps: list[dict[str, str]]) -> None:
        # Defining this method makes the wrapper satisfy SupportsPlanUpdates even
        # when its delegate is narration-only, so guard on the delegate first.
        if not isinstance(self._delegate, SupportsPlanUpdates):
            return
        text = _join_moderation_text(*(step.get("content", "") for step in steps))
        if text:
            try:
                decision = await self._moderation_service.check(
                    text=text,
                    direction=Direction.OUTPUT,
                    user_id=self._user_id,
                    channel_id=self._channel_id,
                    thread_id=self._thread_id,
                    trust_tier=self._trust_tier.value,
                )
            except Exception:
                log.warning("Plan moderation failed; suppressing plan update", exc_info=True)
                return
            if decision.blocked:
                return
        await self._delegate.update_plan([dict(step) for step in steps])


def _join_moderation_text(*chunks: str) -> str:
    return "\n\n".join(chunk.strip() for chunk in chunks if chunk.strip())


def _input_moderation_text(turn: TurnRequest) -> str:
    reply_text = turn.reply_context.text if turn.reply_context is not None else ""
    return _join_moderation_text(turn.content, reply_text)


def _input_moderation_images(turn: TurnRequest) -> list[ContentPart]:
    images = list(turn.input_parts)
    if turn.edit_target_image is not None:
        images.append(turn.edit_target_image)
    if turn.reply_context is not None:
        images.extend(turn.reply_context.image_parts)
    return images


def _blocked_by_error(decision: Any) -> bool:
    """Whether a block came from the check failing rather than a category match.

    Read defensively: ModerationServiceLike.check is typed Any, so stand-in
    services in tests and plugins need not carry the field.
    """
    return bool(getattr(decision, "error", False))


async def _moderate_input_attachments(
    turn: TurnRequest,
    *,
    moderation_service: ModerationServiceLike,
    deadline: float | None,
) -> tuple[TurnRequest, bool, bool]:
    """Screen each ambient non-image attachment before tools can read it.

    The exact checked bytes replace the remote Discord source, preventing a later
    ``import_attachment`` read from observing different content. The configured
    backend accepts UTF-8 text only for non-image files. Oversized/binary files stay
    visible as metadata so the model can explain the limitation, but their source is
    removed and reads fail with a stable user-facing reason.
    """
    screened: list[AttachmentRef] = []
    blocked = False
    blocked_by_error = False
    for attachment in turn.attachments:
        if attachment.size > MAX_TEXT_MODERATION_BYTES:
            screened.append(
                replace(
                    attachment,
                    source=None,
                    cached_payload=None,
                    unavailable_reason=UNSUPPORTED_MODERATION_FILE_MESSAGE,
                )
            )
            continue
        try:
            payload = await _await_with_deadline(attachment.read(), deadline)
        except ConversationTurnTimeoutError:
            raise
        except Exception:
            log.warning(
                "Could not read input attachment for moderation: %s",
                attachment.filename,
                exc_info=True,
            )
            screened.append(
                replace(
                    attachment,
                    source=None,
                    cached_payload=None,
                    unavailable_reason=(
                        "This attachment could not be read for content moderation."
                    ),
                )
            )
            continue

        try:
            text = text_from_file_bytes(attachment.filename, payload)
        except UnsupportedModerationFile:
            log.info(
                "Withholding unmoderatable ambient attachment from tools: %s",
                attachment.filename,
            )
            screened.append(
                replace(
                    attachment,
                    source=None,
                    cached_payload=None,
                    unavailable_reason=UNSUPPORTED_MODERATION_FILE_MESSAGE,
                )
            )
            continue

        checked_attachment = replace(
            attachment,
            source=None,
            cached_payload=payload,
            unavailable_reason="",
        )
        screened.append(checked_attachment)
        if not text:
            continue
        decision = await _await_with_deadline(
            moderation_service.check(
                text=text,
                direction=Direction.INPUT,
                user_id=turn.user_id,
                channel_id=turn.channel_id,
                thread_id=turn.thread_id,
                trust_tier=turn.trust_tier.value,
            ),
            deadline,
        )
        if decision.blocked:
            blocked = True
            blocked_by_error = _blocked_by_error(decision)
            break

    return replace(turn, attachments=tuple(screened)), blocked, blocked_by_error


def _filter_turn_images_after_moderation(
    turn: TurnRequest,
    *,
    checked_image_urls: tuple[str, ...] | None,
) -> TurnRequest:
    if checked_image_urls is None:
        return turn
    allowed = set(checked_image_urls)
    input_parts = tuple(
        part for part in turn.input_parts if _content_part_image_urls(part) & allowed
    )
    edit_target_image = (
        turn.edit_target_image
        if _content_part_image_urls(turn.edit_target_image) & allowed
        else None
    )
    reply_context = turn.reply_context
    if reply_context is not None:
        reply_images = tuple(
            part for part in reply_context.image_parts if _content_part_image_urls(part) & allowed
        )
        reply_context = replace(reply_context, image_parts=reply_images)
    if (
        input_parts == turn.input_parts
        and edit_target_image == turn.edit_target_image
        and reply_context == turn.reply_context
    ):
        return turn
    return replace(
        turn,
        input_parts=input_parts,
        edit_target_image=edit_target_image,
        reply_context=reply_context,
    )


def _content_part_image_urls(part: ContentPart | None) -> set[str]:
    if part is None or not part.image_url:
        return set()
    normalized, _media_type = normalize_image_data_url(part.image_url, part.media_type)
    return {part.image_url, normalized}


def _clear_pending_response_artifacts(context: ConversationContext) -> None:
    context.pending_output_files.clear()
    context.pending_output_file_descriptions.clear()
    context.pending_allowed_file_roots.clear()
    context.pending_embed = None
    context.pending_embed_attachment = None
    context.pending_thread_request = None
    context.pending_thread_close_request = None
    context.pending_terminal_handoff = None
