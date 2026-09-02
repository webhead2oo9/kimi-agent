from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from agent.attachments import AttachmentRef, TurnImages
from agent.context import ConversationContext
from agent.discord_references import DiscordReferenceHint
from agent.reply_context import ReplyContext
from agent.turn import (
    TurnDependencies,
    TurnPreparationConfig,
    TurnPreparationInput,
    prepare_turn,
)
from providers.image_caption import format_image_caption
from providers.types import ContentPart, ConversationMessage, ProviderCapability
from tests.helpers import RecordingEnsureUserBank, RecordingRecall, make_turn_dependencies
from trust.tiers import TrustTier


class FakeContextManager:
    def __init__(self, context: ConversationContext) -> None:
        self.context = context
        self.calls: list[dict[str, Any]] = []

    async def build_turn_context(
        self,
        key: str,
        channel_name: str = "",
        before_discord_message_id: str | None = None,
        **access: Any,
    ) -> ConversationContext:
        self.calls.append(
            {
                "key": key,
                "channel_name": channel_name,
                "before_discord_message_id": before_discord_message_id,
                **access,
            }
        )
        return self.context

    async def add_activated_tools(
        self,
        context: ConversationContext,
        names: set[str],
    ) -> None:
        _ = (context, names)

    async def has_loaded_message(
        self,
        context: ConversationContext,
        discord_message_id: str,
    ) -> bool:
        return any(
            message.source_discord_message_id == discord_message_id for message in context.messages
        )


class FakePreferenceStore:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled
        self.calls: list[str] = []

    async def is_memory_enabled(self, user_id: str) -> bool:
        self.calls.append(user_id)
        return self.enabled


class FakeProvider:
    provider_key = "openai_responses"
    model = "gpt-5.4"

    def __init__(self, capabilities: set[ProviderCapability] | None = None) -> None:
        self.capabilities = capabilities or {ProviderCapability.TEXT}


class RecordingCollectImages:
    def __init__(self, result: TurnImages | None = None) -> None:
        self.result = result or TurnImages(vision_parts=[], edit_target=None)
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, message: Any, **kwargs: Any) -> TurnImages:
        self.calls.append({"message": message, **kwargs})
        return self.result


class RecordingCollectReplyContext:
    def __init__(self, result: ReplyContext | None = None) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, message: Any, **kwargs: Any) -> ReplyContext | None:
        self.calls.append({"message": message, **kwargs})
        return self.result


class RecordingStripMention:
    def __init__(self, result: str = "hello") -> None:
        self.result = result
        self.calls: list[tuple[str, Any]] = []

    def __call__(self, content: str, *, bot_user: Any) -> str:
        self.calls.append((content, bot_user))
        return self.result


def _input(
    message: Any | None = None,
    *,
    referenced_message_id: str | None = None,
    channel_id: str = "100",
    thread_id: str | None = None,
    parent_channel_id: str = "",
    allow_bot_authored_reply_context: bool = False,
    conversation_owner_user_id: str | None = "123",
) -> TurnPreparationInput:
    return TurnPreparationInput(
        raw_content="<@999> hello",
        source_message=message or object(),
        bot_user=object(),
        guild_id="999",
        channel_id=channel_id,
        thread_id=thread_id,
        parent_channel_id=parent_channel_id,
        channel_name="general",
        user_id="123",
        user_name="Alice",
        trust_tier=TrustTier.MEMBER,
        conversation_key="999:100:main",
        trigger_discord_message_id="555",
        referenced_message_id=referenced_message_id,
        conversation_owner_user_id=conversation_owner_user_id,
        allow_bot_authored_reply_context=allow_bot_authored_reply_context,
    )


def _config() -> TurnPreparationConfig:
    return TurnPreparationConfig(
        user_memory_recall_types=("world", "experience"),
        image_detail="high",
        recent_image_lookback=3,
        max_turn_images=2,
    )


def _dependencies(
    *,
    context: ConversationContext | None = None,
    provider: FakeProvider | None = None,
    preference_store: FakePreferenceStore | None = None,
    memory_client: Any | None = None,
    ensure_user_bank: RecordingEnsureUserBank | None = None,
    recall: RecordingRecall | None = None,
    collect_images: RecordingCollectImages | None = None,
    collect_reply_context: RecordingCollectReplyContext | None = None,
    collect_attachments_result: tuple[AttachmentRef, ...] = (),
    strip_mention: RecordingStripMention | None = None,
    skills_index_builder: Any | None = None,
    personal_skills_index_builder: Any | None = None,
    user_persona_loader: Any | None = None,
    channel_pinned_tools: Any | None = None,
    blocked_tools: Any | None = None,
    tool_configs: Any | None = None,
    resolve_discord_references: Any | None = None,
    chat_provider_resolver: Any | None = None,
) -> tuple[TurnDependencies, FakeContextManager]:
    manager = FakeContextManager(context or ConversationContext(key="guild:100:main"))
    # Only what a test names is overridden; make_turn_dependencies supplies an
    # inert default for every other field.
    optional: dict[str, Any] = {
        "ensure_user_bank": ensure_user_bank,
        "user_persona_loader": user_persona_loader,
        "chat_provider_resolver": chat_provider_resolver,
        "channel_pinned_tools": channel_pinned_tools,
        "blocked_tools": blocked_tools,
        "tool_configs": tool_configs,
        "resolve_discord_references": resolve_discord_references,
    }
    dependencies = make_turn_dependencies(
        context_manager=manager,
        provider=provider or FakeProvider(),
        memory_client=memory_client,
        preference_store=preference_store,
        recall_current_user_context=recall or RecordingRecall(),
        skills_index_builder=skills_index_builder or (lambda: "## Skills"),
        personal_skills_index_builder=(
            personal_skills_index_builder or (lambda: "## Personal Skills")
        ),
        collect_turn_images=collect_images or RecordingCollectImages(),
        collect_reply_context=collect_reply_context or RecordingCollectReplyContext(),
        collect_turn_attachments=lambda message: list(collect_attachments_result),
        strip_mention=strip_mention or RecordingStripMention(),
        **{key: value for key, value in optional.items() if value is not None},
    )
    return dependencies, manager


@pytest.mark.asyncio
async def test_prepare_turn_resolves_discord_hints_without_changing_user_content() -> None:
    hint = DiscordReferenceHint(
        source="channel_mention",
        channel_id="222",
        channel_name="support",
    )
    seen: list[str] = []

    async def resolve(content: str) -> tuple[DiscordReferenceHint, ...]:
        seen.append(content)
        return (hint,)

    dependencies, _manager = _dependencies(resolve_discord_references=resolve)

    prepared = await prepare_turn(_input(), dependencies=dependencies, config=_config())

    assert prepared is not None
    assert prepared.content == "hello"
    assert prepared.discord_reference_hints == (hint,)
    assert seen == ["hello"]


@pytest.mark.asyncio
async def test_prepare_turn_carries_the_thread_scope_onto_the_request() -> None:
    """The parent channel id has to survive prepare_turn to reach the prompt.

    Losing this field silently resolves per-channel instructions against the
    thread id.
    """
    dependencies, _manager = _dependencies()

    prepared = await prepare_turn(
        _input(channel_id="77", thread_id="77", parent_channel_id="20"),
        dependencies=dependencies,
        config=_config(),
    )

    assert prepared is not None
    assert (prepared.channel_id, prepared.thread_id) == ("77", "77")
    assert prepared.parent_channel_id == "20"


@pytest.mark.asyncio
async def test_prepare_turn_updates_context_metadata() -> None:
    context = ConversationContext(key="guild:100:main")
    dependencies, manager = _dependencies(context=context)

    prepared = await prepare_turn(
        _input(),
        dependencies=dependencies,
        config=_config(),
    )

    assert prepared is not None
    assert prepared.content == "hello"
    assert prepared.context is context
    assert prepared.skills_index == "## Skills"
    assert prepared.personal_skills_index == "## Personal Skills"
    assert prepared.user_persona == ""
    assert manager.calls == [
        {
            "key": "999:100:main",
            "channel_name": "general",
            "before_discord_message_id": "555",
            "owner_user_id": "123",
            "access_scope": "channel_shared",
        }
    ]
    assert context.user_id == "123"
    assert context.user_name == "Alice"
    assert context.channel_name == "general"
    assert context.participants == {"123": "Alice"}


@pytest.mark.asyncio
async def test_prepare_turn_preserves_unknown_owner_for_shared_root() -> None:
    dependencies, manager = _dependencies()

    prepared = await prepare_turn(
        _input(conversation_owner_user_id=None),
        dependencies=dependencies,
        config=_config(),
    )

    assert prepared is not None
    assert manager.calls[0]["owner_user_id"] is None


@pytest.mark.asyncio
async def test_prepare_turn_merges_channel_pinned_tools() -> None:
    context = ConversationContext(
        key="guild:100:main",
        activated_tools={"openalex_lookup"},
    )
    dependencies, _manager = _dependencies(
        context=context,
        channel_pinned_tools=lambda: frozenset({"move_to_thread", "leave_thread"}),
    )

    prepared = await prepare_turn(
        _input(),
        dependencies=dependencies,
        config=_config(),
    )

    assert prepared is not None
    assert prepared.context.activated_tools == {
        "openalex_lookup",
        "move_to_thread",
        "leave_thread",
    }


@pytest.mark.asyncio
async def test_prepare_turn_without_pins_keeps_activated_tools() -> None:
    context = ConversationContext(
        key="guild:100:main",
        activated_tools={"openalex_lookup"},
    )
    dependencies, _manager = _dependencies(
        context=context,
        channel_pinned_tools=lambda: frozenset(),
    )

    prepared = await prepare_turn(
        _input(),
        dependencies=dependencies,
        config=_config(),
    )

    assert prepared is not None
    assert prepared.context.activated_tools == {"openalex_lookup"}


@pytest.mark.asyncio
async def test_prepare_turn_sets_blocked_tools_on_context() -> None:
    context = ConversationContext(key="guild:100:main")
    dependencies, _manager = _dependencies(
        context=context,
        blocked_tools=lambda: frozenset({"get_steam_game_info", "blocked_tool"}),
    )

    prepared = await prepare_turn(_input(), dependencies=dependencies, config=_config())

    assert prepared is not None
    assert prepared.context.blocked_tools == frozenset({"get_steam_game_info", "blocked_tool"})


@pytest.mark.asyncio
async def test_prepare_turn_blocked_tool_is_not_activated_by_a_pin() -> None:
    # The denylist wins over a pin: a name that is both pinned and blocked must
    # never reach the activated set.
    context = ConversationContext(key="guild:100:main")
    dependencies, _manager = _dependencies(
        context=context,
        channel_pinned_tools=lambda: frozenset({"move_to_thread", "blocked_tool"}),
        blocked_tools=lambda: frozenset({"blocked_tool"}),
    )

    prepared = await prepare_turn(_input(), dependencies=dependencies, config=_config())

    assert prepared is not None
    assert prepared.context.activated_tools == {"move_to_thread"}
    assert prepared.context.blocked_tools == frozenset({"blocked_tool"})


@pytest.mark.asyncio
async def test_prepare_turn_without_blocked_dependency_defaults_empty() -> None:
    context = ConversationContext(key="guild:100:main")
    dependencies, _manager = _dependencies(context=context)

    prepared = await prepare_turn(_input(), dependencies=dependencies, config=_config())

    assert prepared is not None
    assert prepared.context.blocked_tools == frozenset()


@pytest.mark.asyncio
async def test_prepare_turn_stashes_resolved_tool_configs_on_context() -> None:
    context = ConversationContext(key="guild:100:main")
    resolved = {"internet_search": {"mode": "blend", "search_providers": ("brave",)}}
    dependencies, _manager = _dependencies(
        context=context,
        tool_configs=lambda: resolved,
    )

    prepared = await prepare_turn(_input(), dependencies=dependencies, config=_config())

    assert prepared is not None
    assert prepared.context.tool_configs == resolved


@pytest.mark.asyncio
async def test_prepare_turn_without_tool_config_dependency_defaults_empty() -> None:
    context = ConversationContext(key="guild:100:main")
    dependencies, _manager = _dependencies(context=context)

    prepared = await prepare_turn(_input(), dependencies=dependencies, config=_config())

    assert prepared is not None
    assert prepared.context.tool_configs == {}


@pytest.mark.asyncio
async def test_prepare_turn_uses_personal_skills_index_builder() -> None:
    calls: list[str] = []

    def personal_builder() -> str:
        calls.append("called")
        return "- **quest-setup**: Setup steps."

    dependencies, _ = _dependencies(personal_skills_index_builder=personal_builder)

    prepared = await prepare_turn(
        _input(),
        dependencies=dependencies,
        config=_config(),
    )

    assert prepared is not None
    assert prepared.personal_skills_index == "- **quest-setup**: Setup steps."
    assert calls == ["called"]


@pytest.mark.asyncio
async def test_prepare_turn_uses_user_persona_loader() -> None:
    calls: list[str] = []

    async def load_persona(user_id: str) -> str:
        calls.append(user_id)
        return "Roleplay as a friendly space mechanic."

    dependencies, _ = _dependencies(user_persona_loader=load_persona)

    prepared = await prepare_turn(
        _input(),
        dependencies=dependencies,
        config=_config(),
    )

    assert prepared is not None
    assert prepared.user_persona == "Roleplay as a friendly space mechanic."
    assert calls == ["123"]


@pytest.mark.asyncio
async def test_prepare_turn_defers_memory_lookup_until_after_input_moderation() -> None:
    ensure_user_bank = RecordingEnsureUserBank()
    preference_store = FakePreferenceStore(enabled=False)
    recall = RecordingRecall(result="- Alice prefers short replies.")
    dependencies, _ = _dependencies(
        memory_client=object(),
        preference_store=preference_store,
        ensure_user_bank=ensure_user_bank,
        recall=recall,
    )

    prepared = await prepare_turn(_input(), dependencies=dependencies, config=_config())

    assert prepared is not None
    assert prepared.recalled_memories == ""
    assert preference_store.calls == []
    assert ensure_user_bank.calls == []
    assert recall.calls == []


@pytest.mark.asyncio
async def test_prepare_turn_proceeds_for_image_only_message() -> None:
    vision_part = ContentPart.from_image_url(
        url="data:image/png;base64,ZGVm",
        media_type="image/png",
    )
    collect_images = RecordingCollectImages(
        TurnImages(vision_parts=[vision_part], edit_target=None)
    )
    message = SimpleNamespace(attachments=[SimpleNamespace(content_type="image/png")])
    dependencies, _ = _dependencies(
        provider=FakeProvider({ProviderCapability.TEXT, ProviderCapability.IMAGE_INPUT}),
        collect_images=collect_images,
        strip_mention=RecordingStripMention(result=""),
    )

    prepared = await prepare_turn(
        _input(message),
        dependencies=dependencies,
        config=_config(),
    )

    assert prepared is not None
    assert prepared.content == ""
    assert prepared.input_parts == (vision_part,)


@pytest.mark.asyncio
async def test_prepare_turn_returns_none_for_empty_message_without_images() -> None:
    message = SimpleNamespace(attachments=[])
    dependencies, _ = _dependencies(
        strip_mention=RecordingStripMention(result=""),
    )

    prepared = await prepare_turn(
        _input(message),
        dependencies=dependencies,
        config=_config(),
    )

    assert prepared is None


@pytest.mark.asyncio
async def test_prepare_turn_collects_images_with_context_key_and_history_hashes() -> None:
    context = ConversationContext(key="guild:100:main")
    context.messages.append(
        ConversationMessage(
            role="user",
            content=[
                ContentPart.from_image_url(
                    url="data:image/png;base64,YWJj",
                    media_type="image/png",
                )
            ],
        )
    )
    vision_part = ContentPart.from_image_url(
        url="data:image/png;base64,ZGVm",
        media_type="image/png",
    )
    collect_images = RecordingCollectImages(
        TurnImages(vision_parts=[vision_part], edit_target=vision_part)
    )
    attachment = AttachmentRef(
        filename="notes.txt",
        size=12,
        content_type="text/plain",
        source=None,
    )
    message = object()
    dependencies, _ = _dependencies(
        context=context,
        provider=FakeProvider({ProviderCapability.TEXT, ProviderCapability.IMAGE_INPUT}),
        collect_images=collect_images,
        collect_attachments_result=(attachment,),
    )

    prepared = await prepare_turn(
        _input(message),
        dependencies=dependencies,
        config=_config(),
    )

    assert prepared is not None
    assert prepared.input_parts == (vision_part,)
    assert prepared.edit_target_image == vision_part
    assert prepared.attachments == (attachment,)

    [call] = collect_images.calls
    assert call["message"] is message
    assert call["conversation_key"] == "guild:100:main"
    assert call["detail"] == "high"
    assert call["images_supported"] is True
    assert call["history_hashes"]
    assert call["lookback"] == 3
    assert call["max_images"] == 2
    assert call["include_reply_images"] is False


@pytest.mark.asyncio
async def test_prepare_turn_routes_stored_image_history_for_image_collection() -> None:
    context = ConversationContext(key="guild:100:main")
    context.messages.append(
        ConversationMessage(
            role="user",
            content=[
                ContentPart.from_text("Alice: look at this"),
                ContentPart.from_image_url(
                    url="data:image/png;base64,YWJj",
                    media_type="image/png",
                ),
            ],
        )
    )
    text_provider = FakeProvider({ProviderCapability.TEXT})
    image_provider = FakeProvider({ProviderCapability.TEXT, ProviderCapability.IMAGE_INPUT})
    collect_images = RecordingCollectImages()
    dependencies, _ = _dependencies(
        context=context,
        provider=text_provider,
        collect_images=collect_images,
        chat_provider_resolver=lambda *, images=False: image_provider,
    )

    prepared = await prepare_turn(
        _input(object()),
        dependencies=dependencies,
        config=_config(),
    )

    assert prepared is not None
    [call] = collect_images.calls
    assert call["images_supported"] is True
    assert call["history_hashes"]


@pytest.mark.asyncio
async def test_zero_image_budget_strips_persisted_images_and_uses_text_provider() -> None:
    context = ConversationContext(
        key="guild:100:main",
        messages=[
            ConversationMessage(
                role="user",
                content=[
                    ContentPart.from_text("Alice: retained text"),
                    ContentPart.from_image_url(
                        url="data:image/png;base64,YWJj",
                        media_type="image/png",
                    ),
                    # A stored caption describes the image, so a kill switch that
                    # dropped only the pixels would still feed the model its contents.
                    ContentPart.from_text(format_image_caption("Image 1: a stop sign.")),
                ],
            )
        ],
    )
    provider_resolutions: list[bool] = []
    collect_images = RecordingCollectImages()
    dependencies, _ = _dependencies(
        context=context,
        provider=FakeProvider({ProviderCapability.TEXT}),
        collect_images=collect_images,
        chat_provider_resolver=lambda *, images=False: provider_resolutions.append(images),
    )

    prepared = await prepare_turn(
        _input(object()),
        dependencies=dependencies,
        config=replace(_config(), max_turn_images=0),
    )

    assert prepared is not None
    assert provider_resolutions == []
    [history_message] = prepared.context.get_history()
    assert history_message.content == [ContentPart.from_text("Alice: retained text")]
    [call] = collect_images.calls
    assert call["images_supported"] is False
    assert call["history_hashes"] == set()
    assert call["max_images"] == 0


@pytest.mark.asyncio
async def test_prepare_turn_disables_recent_image_scan_for_fresh_mention() -> None:
    collect_images = RecordingCollectImages()
    dependencies, _ = _dependencies(collect_images=collect_images)

    prepared = await prepare_turn(
        _input(object()),
        dependencies=dependencies,
        config=_config(),
    )

    assert prepared is not None
    [call] = collect_images.calls
    assert call["lookback"] == 0


@pytest.mark.asyncio
async def test_prepare_turn_keeps_recent_image_scan_for_reply() -> None:
    collect_images = RecordingCollectImages()
    dependencies, _ = _dependencies(collect_images=collect_images)

    prepared = await prepare_turn(
        _input(object(), referenced_message_id="444"),
        dependencies=dependencies,
        config=_config(),
    )

    assert prepared is not None
    [call] = collect_images.calls
    assert call["lookback"] == 3


@pytest.mark.asyncio
async def test_prepare_turn_sets_reply_context_when_referenced_message_is_not_stored() -> None:
    context = ConversationContext(key="guild:100:main", db_conversation_id=55)
    reply_image = ContentPart.from_image_url(
        url="data:image/png;base64,YWJj",
        media_type="image/png",
    )
    collect_reply = RecordingCollectReplyContext(
        ReplyContext(
            referenced_message_id="444",
            author_name="Bob",
            text="the thing Alice replied to",
            image_parts=(reply_image,),
        )
    )
    dependencies, manager = _dependencies(
        context=context,
        provider=FakeProvider({ProviderCapability.TEXT, ProviderCapability.IMAGE_INPUT}),
        collect_reply_context=collect_reply,
    )
    source_message = object()

    prepared = await prepare_turn(
        _input(source_message, referenced_message_id="444"),
        dependencies=dependencies,
        config=_config(),
    )

    assert prepared is not None
    assert prepared.reply_context == collect_reply.result
    [call] = collect_reply.calls
    assert call["message"] is source_message
    assert call["bot_user"] is not None
    assert call["conversation_key"] == "guild:100:main"
    assert call["detail"] == "high"
    assert call["images_supported"] is True
    assert call["max_images"] == 2


@pytest.mark.asyncio
async def test_prepare_turn_propagates_bot_reply_context_permission() -> None:
    collect_images = RecordingCollectImages()
    collect_reply = RecordingCollectReplyContext()
    dependencies, _ = _dependencies(
        collect_images=collect_images,
        collect_reply_context=collect_reply,
    )
    source = _input(
        object(),
        referenced_message_id="444",
        allow_bot_authored_reply_context=True,
    )

    prepared = await prepare_turn(
        source,
        dependencies=dependencies,
        config=_config(),
    )

    assert prepared is not None
    [image_call] = collect_images.calls
    assert image_call["bot_user"] is source.bot_user
    assert image_call["allow_bot_authored"] is True
    [reply_call] = collect_reply.calls
    assert reply_call["allow_bot_authored"] is True


@pytest.mark.asyncio
async def test_prepare_turn_limits_reply_context_to_remaining_image_budget() -> None:
    context = ConversationContext(key="guild:100:main", db_conversation_id=55)
    current_image = ContentPart.from_image_url(
        url="data:image/png;base64,Y3VycmVudA==",
        media_type="image/png",
    )
    collect_images = RecordingCollectImages(
        TurnImages(vision_parts=[current_image], edit_target=current_image)
    )
    collect_reply = RecordingCollectReplyContext()
    dependencies, _ = _dependencies(
        context=context,
        provider=FakeProvider({ProviderCapability.TEXT, ProviderCapability.IMAGE_INPUT}),
        collect_images=collect_images,
        collect_reply_context=collect_reply,
    )

    prepared = await prepare_turn(
        _input(object(), referenced_message_id="444"),
        dependencies=dependencies,
        config=_config(),
    )

    assert prepared is not None
    [call] = collect_reply.calls
    assert call["max_images"] == 1


@pytest.mark.asyncio
async def test_prepare_turn_skips_reply_context_when_reference_already_stored() -> None:
    context = ConversationContext(
        key="guild:100:main",
        db_conversation_id=55,
        messages=[
            ConversationMessage(
                role="user",
                content=[ContentPart.from_text("Bob: already stored")],
                source_discord_message_id="444",
            )
        ],
    )
    collect_reply = RecordingCollectReplyContext(
        ReplyContext(
            referenced_message_id="444",
            author_name="Bob",
            text="already stored",
        )
    )
    dependencies, _ = _dependencies(
        context=context,
        collect_reply_context=collect_reply,
    )

    prepared = await prepare_turn(
        _input(object(), referenced_message_id="444"),
        dependencies=dependencies,
        config=_config(),
    )

    assert prepared is not None
    assert prepared.reply_context is None
    assert collect_reply.calls == []


@pytest.mark.asyncio
async def test_prepare_turn_collects_reply_context_when_stored_reference_was_evicted() -> None:
    context = ConversationContext(
        key="guild:100:main",
        db_conversation_id=55,
        messages=[
            ConversationMessage(
                role="assistant",
                content=[ContentPart.from_text("newer reply")],
                source_discord_message_id="999",
            )
        ],
    )
    collect_reply = RecordingCollectReplyContext(
        ReplyContext(
            referenced_message_id="444",
            author_name="Bob",
            text="older persisted message",
        )
    )
    dependencies, _ = _dependencies(
        context=context,
        collect_reply_context=collect_reply,
    )

    prepared = await prepare_turn(
        _input(object(), referenced_message_id="444"),
        dependencies=dependencies,
        config=_config(),
    )

    assert prepared is not None
    assert prepared.reply_context == collect_reply.result
    assert len(collect_reply.calls) == 1


@pytest.mark.asyncio
async def test_prepare_turn_proceeds_for_non_image_attachment_only_message() -> None:
    # "@bot" + a PDF/txt and no text must still run a turn: the file is
    # surfaced as importable turn context, exactly like the image-only case.
    attachment = AttachmentRef(
        filename="notes.txt",
        size=12,
        content_type="text/plain",
        source=None,
    )
    message = SimpleNamespace(attachments=[SimpleNamespace(content_type="text/plain")])
    dependencies, _ = _dependencies(
        strip_mention=RecordingStripMention(result=""),
        collect_attachments_result=(attachment,),
    )

    prepared = await prepare_turn(
        _input(message),
        dependencies=dependencies,
        config=_config(),
    )

    assert prepared is not None
    assert prepared.content == ""
    assert prepared.attachments == (attachment,)


@pytest.mark.asyncio
async def test_prepare_turn_removes_validated_image_from_generic_attachment_path() -> None:
    source = SimpleNamespace(
        filename="photo.png",
        content_type="application/octet-stream",
    )
    attachment = AttachmentRef(
        filename=source.filename,
        size=12,
        content_type=source.content_type,
        source=source,
    )
    image_part = ContentPart.from_image_url(
        url="data:image/png;base64,YWJj",
        media_type="image/png",
    )
    collect_images = RecordingCollectImages(
        TurnImages(
            vision_parts=[image_part],
            edit_target=None,
            current_attachment_source_ids=frozenset({id(source)}),
        )
    )
    dependencies, _ = _dependencies(
        collect_images=collect_images,
        collect_attachments_result=(attachment,),
    )

    prepared = await prepare_turn(
        _input(SimpleNamespace(attachments=[source])),
        dependencies=dependencies,
        config=_config(),
    )

    assert prepared is not None
    assert prepared.input_parts == (image_part,)
    assert prepared.attachments == ()


@pytest.mark.asyncio
async def test_zero_image_budget_rejects_image_only_turn_before_context_or_reads() -> None:
    image = SimpleNamespace(content_type="image/png", filename="pic.png")
    message = SimpleNamespace(attachments=[image])
    collect_images = RecordingCollectImages()
    dependencies, manager = _dependencies(
        strip_mention=RecordingStripMention(result=""),
        collect_images=collect_images,
    )

    prepared = await prepare_turn(
        _input(message),
        dependencies=dependencies,
        config=replace(_config(), max_turn_images=0),
    )

    assert prepared is None
    assert manager.calls == []
    assert collect_images.calls == []


@pytest.mark.asyncio
async def test_zero_image_budget_rejects_generic_image_candidate_attachment() -> None:
    source = SimpleNamespace(
        content_type="application/octet-stream",
        filename="pic.png",
    )
    attachment = AttachmentRef(
        filename=source.filename,
        size=12,
        content_type=source.content_type,
        source=source,
    )
    message = SimpleNamespace(attachments=[source])
    collect_images = RecordingCollectImages()
    dependencies, manager = _dependencies(
        strip_mention=RecordingStripMention(result=""),
        collect_images=collect_images,
        collect_attachments_result=(attachment,),
    )

    prepared = await prepare_turn(
        _input(message),
        dependencies=dependencies,
        config=replace(_config(), max_turn_images=0),
    )

    assert prepared is None
    assert manager.calls == []
    assert collect_images.calls == []


# --- New-user onboarding detection -------------------------------------------


@pytest.mark.asyncio
async def test_prepare_turn_flags_new_user_below_threshold() -> None:
    from dataclasses import replace

    calls: list[tuple[str, str | None, int]] = []

    async def count(user_id: str, exclude_discord_message_id: str | None, limit: int) -> int:
        calls.append((user_id, exclude_discord_message_id, limit))
        return 2  # below threshold

    deps, _ = _dependencies()
    deps = replace(deps, count_user_prior_messages=count)
    cfg = replace(_config(), new_user_onboarding_turns=5)

    prepared = await prepare_turn(_input(), dependencies=deps, config=cfg)

    assert prepared is not None
    assert prepared.is_new_user is True
    # Defensively excludes the current trigger id, and caps the count at the threshold.
    assert calls == [("123", "555", 5)]


@pytest.mark.asyncio
async def test_prepare_turn_not_new_at_or_above_threshold() -> None:
    from dataclasses import replace

    async def count(user_id: str, exclude_discord_message_id: str | None, limit: int) -> int:
        return 5  # at threshold -> established

    deps, _ = _dependencies()
    deps = replace(deps, count_user_prior_messages=count)
    cfg = replace(_config(), new_user_onboarding_turns=5)

    prepared = await prepare_turn(_input(), dependencies=deps, config=cfg)

    assert prepared is not None
    assert prepared.is_new_user is False


@pytest.mark.asyncio
async def test_prepare_turn_onboarding_disabled_skips_count() -> None:
    from dataclasses import replace

    called = False

    async def count(user_id: str, exclude_discord_message_id: str | None, limit: int) -> int:
        nonlocal called
        called = True
        return 0

    deps, _ = _dependencies()
    deps = replace(deps, count_user_prior_messages=count)
    cfg = _config()  # new_user_onboarding_turns defaults to 0 (disabled)

    prepared = await prepare_turn(_input(), dependencies=deps, config=cfg)

    assert prepared is not None
    assert prepared.is_new_user is False
    assert called is False  # disabled -> no DB query at all
