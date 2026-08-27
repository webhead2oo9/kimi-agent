"""Exercises agent/turn.py's execute_turn: the orchestration between routing
and the core loop, staging workspace outputs, timeouts, and usage
persistence around one turn. Isolated from both neighbors so a timeout or
partial-result bug does not need a live Discord message or a real model
call to reproduce.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from agent.activity import ActivityReporter, ActivityUpdate
from agent.attachments import AttachmentRef
from agent.context import ConversationContext
from agent.core import ConversationRunRequest, ConversationRunResult
from agent.reply_context import ReplyContext
from agent.turn import (
    RunConversation,
    TurnDependencies,
    TurnExecutionConfig,
    TurnRequest,
    execute_turn,
)
from workspace import WorkspaceKey, WorkspaceManager
from config.model_config import ModelPricing
from moderation.types import Direction, ModerationDecision
from providers.assets import write_generated_assets
from providers.image_caption import format_image_caption, is_image_caption
from providers.types import (
    ContentPart,
    ContentPartType,
    ConversationMessage,
    GeneratedAsset,
    ProviderCapability,
    ProviderRequest,
    ProviderResponse,
)
from tests.helpers import StubContextManager, StubProvider, make_turn_dependencies
from tools.embeds import EmbedAttachment, EmbedSpec
from tools.registry import TurnHandoff
from tools.threads import ThreadRequest
from tools.workspace.common import UserLocks
from trust.tiers import TrustTier
from usage.normalization import LLMUsageCall, UsageBreakdown


def _provider() -> StubProvider:
    return StubProvider(
        provider_key="openai_responses",
        model="gpt-5.4",
        capabilities={ProviderCapability.TEXT},
    )


class RecordingRunConversation:
    def __init__(self, result: ConversationRunResult) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self,
        request: ConversationRunRequest | None = None,
        **kwargs: Any,
    ) -> ConversationRunResult:
        if request is not None:
            if kwargs:
                raise AssertionError("request and direct kwargs should not be mixed")
            self.calls.append(request.__dict__)
        else:
            self.calls.append(kwargs)
        return self.result


class RecordingAssetWriter:
    def __init__(self, paths: list[Path]) -> None:
        self.paths = paths
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        generated_assets: list[GeneratedAsset],
        *,
        output_dir: Path,
    ) -> list[Path]:
        self.calls.append(
            {
                "generated_assets": generated_assets,
                "output_dir": output_dir,
            }
        )
        return [output_dir / path.name for path in self.paths]


class RecordingImageDistillationCache:
    def __init__(self) -> None:
        self.values: dict[tuple[int, str], tuple[str, str]] = {}
        self.writes: list[dict[str, Any]] = []

    async def get(self, conversation_id: int, cache_key: str) -> tuple[str, str] | None:
        return self.values.get((conversation_id, cache_key))

    async def set(
        self,
        conversation_id: int,
        cache_key: str,
        *,
        model_name: str,
        prompt_version: int,
        description: str,
    ) -> None:
        self.values[(conversation_id, cache_key)] = (description, model_name)
        self.writes.append(
            {
                "conversation_id": conversation_id,
                "cache_key": cache_key,
                "model_name": model_name,
                "prompt_version": prompt_version,
                "description": description,
            }
        )


class RecordingModerationService:
    enabled = True

    def __init__(
        self,
        blocked: bool = False,
        *,
        blocked_text: str | None = None,
        output_exempt_tier: TrustTier | None = None,
    ) -> None:
        self.blocked = blocked
        self.blocked_text = blocked_text
        self.output_exempt_tier = output_exempt_tier
        self.calls: list[dict[str, Any]] = []

    async def check(self, **kwargs: Any) -> ModerationDecision:
        self.calls.append(kwargs)
        blocked = self.blocked or (
            self.blocked_text is not None and self.blocked_text in str(kwargs.get("text", ""))
        )
        return ModerationDecision(
            blocked=blocked,
            matched_categories=["violence/graphic"] if blocked else [],
        )

    def refusal_for(self, direction: Direction, *, error: bool = False) -> str:
        _ = direction
        return "output blocked"


def _turn_request(
    context: ConversationContext,
    *,
    image_part: ContentPart | None = None,
    attachment: AttachmentRef | None = None,
    reply_context: ReplyContext | None = None,
    trust_tier: TrustTier = TrustTier.REGULAR,
    channel_id: str = "100",
    thread_id: str | None = None,
    parent_channel_id: str = "",
) -> TurnRequest:
    return TurnRequest(
        content="draw this",
        context=context,
        trust_tier=trust_tier,
        user_id="123",
        user_name="Alice",
        guild_id="999",
        channel_id=channel_id,
        thread_id=thread_id,
        parent_channel_id=parent_channel_id,
        channel_name="general",
        trigger_discord_message_id="777",
        recalled_memories="- Alice likes concise replies.",
        skills_index="## Skills",
        personal_skills_index="## Personal Skills",
        user_persona="Roleplay as a friendly space mechanic.",
        input_parts=(image_part,) if image_part else (),
        edit_target_image=image_part,
        attachments=(attachment,) if attachment else (),
        reply_context=reply_context,
    )


def _dependencies(
    *,
    workspace_dir: Path,
    run_conversation: RunConversation,
    asset_writer: RecordingAssetWriter | None = None,
    activity_reporter: ActivityReporter | None = None,
    context_manager: StubContextManager | None = None,
    moderation_service: RecordingModerationService | None = None,
    provider: StubProvider | None = None,
    chat_provider_resolver: Any | None = None,
    chat_model_name_resolver: Any | None = None,
    usage_store: Any | None = None,
    image_distillation_store: Any | None = None,
    model_config: Any | None = None,
    resolved_model_name: str = "",
    workspace_manager: WorkspaceManager | None = None,
    workspace_locks: UserLocks | None = None,
) -> TurnDependencies:
    # Only the dependencies a test actually names are overridden; the rest come
    # from make_turn_dependencies' inert defaults.
    optional: dict[str, Any] = {
        "chat_provider_resolver": chat_provider_resolver,
        "chat_model_name_resolver": chat_model_name_resolver,
        "write_generated_assets": asset_writer,
        "moderation_service": moderation_service,
        "workspace_manager": workspace_manager,
        "workspace_locks": workspace_locks,
    }
    return make_turn_dependencies(
        workspace_dir=workspace_dir,
        context_manager=context_manager or StubContextManager(),
        provider=provider or _provider(),
        run_conversation=run_conversation,
        activity_reporter=activity_reporter,
        usage_store=usage_store,
        image_distillation_store=image_distillation_store,
        model_config=model_config,
        resolved_model_name=resolved_model_name,
        **{key: value for key, value in optional.items() if value is not None},
    )


def _config() -> TurnExecutionConfig:
    return TurnExecutionConfig(max_iterations=7, max_tokens=1234)


@pytest.mark.asyncio
async def test_terminal_handoff_bypasses_deadline_sensitive_output_work(tmp_path: Path) -> None:
    context = ConversationContext(key="guild:100:main")
    request = ThreadRequest(name="Coding work")
    context.pending_thread_request = request
    context.pending_output_files.append("stale.txt")
    moderation = RecordingModerationService(blocked=True)
    handoff = TurnHandoff(
        response_text="Coding task `task-123` was queued.",
        reason="coding_task",
        task_id="task-123",
    )

    result = await execute_turn(
        _turn_request(context),
        dependencies=_dependencies(
            workspace_dir=tmp_path / "workspace",
            moderation_service=moderation,
            run_conversation=RecordingRunConversation(
                ConversationRunResult(
                    text=handoff.response_text,
                    terminal_handoff=handoff,
                )
            ),
        ),
        config=_config(),
    )

    assert result.response_text == handoff.response_text
    assert result.terminal_handoff == handoff
    assert result.thread_request == request
    assert result.output_files == ()
    assert moderation.calls == []


@pytest.mark.asyncio
async def test_outer_deadline_recovers_handoff_committed_by_core(tmp_path: Path) -> None:
    context = ConversationContext(key="guild:100:main")
    thread_request = ThreadRequest(name="Coding work")
    handoff = TurnHandoff(
        response_text="Coding task `task-123` was queued.",
        reason="coding_task",
        task_id="task-123",
    )

    async def run_until_cancelled(
        request: ConversationRunRequest | None = None,
        **_kwargs: Any,
    ) -> ConversationRunResult:
        assert request is not None
        request.context.pending_thread_request = thread_request
        request.context.pending_terminal_handoff = handoff
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    result = await execute_turn(
        _turn_request(context),
        dependencies=_dependencies(
            workspace_dir=tmp_path / "workspace",
            run_conversation=run_until_cancelled,
        ),
        config=replace(_config(), timeout_seconds=0.01),
    )

    assert result.response_text == handoff.response_text
    assert result.terminal_handoff == handoff
    assert result.thread_request == thread_request


@pytest.mark.asyncio
async def test_execute_turn_stages_workspace_outputs_before_delivery(tmp_path: Path) -> None:
    manager = WorkspaceManager(tmp_path / "workspace")
    locks = UserLocks()
    context = ConversationContext(key="guild:100:main")
    source = manager.user_files_dir(WorkspaceKey("user123__guild")) / "report.txt"
    source.write_text("moderated bytes", encoding="utf-8")
    context.pending_output_files.append(str(source))
    context.pending_allowed_file_roots.append(str(source.parent))

    result = await execute_turn(
        _turn_request(context),
        dependencies=_dependencies(
            workspace_dir=tmp_path / "workspace",
            workspace_manager=manager,
            workspace_locks=locks,
            run_conversation=RecordingRunConversation(ConversationRunResult(text="attached")),
        ),
        config=_config(),
    )

    staged = Path(result.output_files[0])
    assert staged != source
    assert staged.read_text(encoding="utf-8") == "moderated bytes"
    assert staged.parent.name.startswith("delivery-")
    assert (staged.parent / ".owner-user-id").read_text(encoding="utf-8") == "123"
    assert result.allowed_file_roots == (str(staged.parent.resolve()),)


@pytest.mark.asyncio
async def test_execute_turn_records_partial_usage_from_timed_out_run(
    tmp_path: Path,
) -> None:
    class UsageStore:
        def __init__(self) -> None:
            self.rows: list[dict[str, Any]] = []

        async def record_turn(self, **kwargs: Any) -> None:
            self.rows.append(kwargs)

    store = UsageStore()
    run_conversation = RecordingRunConversation(
        ConversationRunResult(
            text="timed out",
            usage=UsageBreakdown(input_tokens=120, output_tokens=30),
            iterations=2,
            llm_calls=[
                LLMUsageCall(
                    model="gpt-5.4",
                    role="chat",
                    usage=UsageBreakdown(input_tokens=120, output_tokens=30),
                ),
                LLMUsageCall(
                    model="gpt-5.4",
                    role="chat",
                    usage=UsageBreakdown(),
                ),
            ],
            timed_out=True,
        )
    )

    result = await execute_turn(
        _turn_request(ConversationContext(key="guild:100:main")),
        dependencies=_dependencies(
            workspace_dir=tmp_path,
            run_conversation=run_conversation,
            usage_store=store,
            model_config=SimpleNamespace(
                models={"gpt-5.4": SimpleNamespace(pricing=ModelPricing(input=1.0, output=2.0))}
            ),
            resolved_model_name="gpt-5.4",
        ),
        config=_config(),
    )

    assert result.response_text == "timed out"
    assert len(store.rows) == 1
    calls = store.rows[0]["calls"]
    assert len(calls) == 2
    assert calls[0].usage == UsageBreakdown(input_tokens=120, output_tokens=30)


@pytest.mark.asyncio
async def test_outer_timeout_preserves_completed_call_from_shared_sink(
    tmp_path: Path,
) -> None:
    class UsageStore:
        def __init__(self) -> None:
            self.rows: list[dict[str, Any]] = []

        async def record_turn(self, **kwargs: Any) -> None:
            self.rows.append(kwargs)

    class SuppressingRun:
        request: ConversationRunRequest | None = None

        async def __call__(
            self,
            request: ConversationRunRequest | None = None,
            **kwargs: Any,
        ) -> ConversationRunResult:
            assert request is not None and not kwargs
            assert request.usage_sink is not None
            self.request = request
            request.usage_sink.append(
                LLMUsageCall(
                    model="gpt-5.4",
                    role="chat",
                    usage=UsageBreakdown(input_tokens=40, output_tokens=5),
                )
            )
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                # Simulate cancellation cleanup/finalization completing just after
                # the hard outer wall has already returned its timeout result.
                return ConversationRunResult(text="late cleanup")
            raise AssertionError("sleep should have been cancelled")

    store = UsageStore()
    runner = SuppressingRun()
    result = await execute_turn(
        _turn_request(ConversationContext(key="guild:100:main")),
        dependencies=_dependencies(
            workspace_dir=tmp_path,
            run_conversation=runner,
            usage_store=store,
            model_config=SimpleNamespace(
                models={
                    "gpt-5.4": SimpleNamespace(
                        model="gpt-5.4",
                        pricing=ModelPricing(input=1.0, output=2.0),
                    )
                }
            ),
            resolved_model_name="gpt-5.4",
        ),
        config=TurnExecutionConfig(
            max_iterations=7,
            max_tokens=1234,
            timeout_seconds=0.01,
        ),
    )

    assert "timed out" in result.response_text.lower()
    assert runner.request is not None
    assert len(store.rows) == 1
    assert store.rows[0]["turn_id"] == runner.request.turn_id
    [call] = store.rows[0]["calls"]
    assert call.usage == UsageBreakdown(input_tokens=40, output_tokens=5)


@pytest.mark.asyncio
async def test_model_tool_usage_completed_after_timeout_is_persisted(
    tmp_path: Path,
) -> None:
    class UsageStore:
        def __init__(self) -> None:
            self.rows: list[dict[str, Any]] = []

        async def record_turn(self, **kwargs: Any) -> None:
            self.rows.append(kwargs)

    class DelayedModelToolRun:
        def __init__(self) -> None:
            self.release = asyncio.Event()
            self.task: asyncio.Task[None] | None = None
            self.turn_id = ""

        async def __call__(
            self,
            request: ConversationRunRequest | None = None,
            **kwargs: Any,
        ) -> ConversationRunResult:
            assert request is not None and not kwargs
            record_usage_call = request.record_usage_call
            assert record_usage_call is not None
            self.turn_id = request.turn_id

            async def finish_model_tool() -> None:
                await self.release.wait()
                await record_usage_call(
                    LLMUsageCall(
                        model="gemini-served",
                        pricing_model="gemini-priced",
                        role="video_analysis",
                        usage=UsageBreakdown(input_tokens=70, output_tokens=9),
                    )
                )

            self.task = asyncio.create_task(finish_model_tool())
            return ConversationRunResult(
                text="timed out",
                timed_out=True,
                turn_id=request.turn_id,
            )

    store = UsageStore()
    runner = DelayedModelToolRun()
    result = await execute_turn(
        _turn_request(ConversationContext(key="guild:100:main")),
        dependencies=_dependencies(
            workspace_dir=tmp_path,
            run_conversation=runner,
            usage_store=store,
        ),
        config=_config(),
    )

    assert result.response_text == "timed out"
    assert store.rows == []
    assert runner.task is not None
    runner.release.set()
    await asyncio.wait_for(runner.task, timeout=1)

    assert len(store.rows) == 1
    assert store.rows[0]["turn_id"] == runner.turn_id
    [call] = store.rows[0]["calls"]
    assert call.role == "video_analysis"
    assert call.usage == UsageBreakdown(input_tokens=70, output_tokens=9)


@pytest.mark.asyncio
async def test_normal_usage_persistence_outlives_response_deadline(tmp_path: Path) -> None:
    class SlowUsageStore:
        def __init__(self) -> None:
            self.rows: list[dict[str, Any]] = []

        async def record_turn(self, **kwargs: Any) -> None:
            await asyncio.sleep(0.02)
            self.rows.append(kwargs)

    store = SlowUsageStore()
    usage = UsageBreakdown(input_tokens=10, output_tokens=2)
    run_conversation = RecordingRunConversation(
        ConversationRunResult(
            text="ok",
            usage=usage,
            llm_calls=[LLMUsageCall(model="gpt-5.4", role="chat", usage=usage)],
            iterations=1,
            turn_id="turn-usage-deadline",
        )
    )

    result = await execute_turn(
        _turn_request(ConversationContext(key="guild:100:main")),
        dependencies=_dependencies(
            workspace_dir=tmp_path,
            run_conversation=run_conversation,
            usage_store=store,
            model_config=SimpleNamespace(
                models={
                    "gpt-5.4": SimpleNamespace(
                        model="gpt-5.4",
                        pricing=ModelPricing(input=1.0, output=2.0),
                    )
                }
            ),
            resolved_model_name="gpt-5.4",
        ),
        config=TurnExecutionConfig(
            max_iterations=7,
            max_tokens=1234,
            timeout_seconds=0.005,
        ),
    )

    assert result.response_text == "ok"
    assert len(store.rows) == 1
    assert store.rows[0]["turn_id"] == run_conversation.calls[0]["turn_id"]


@pytest.mark.asyncio
async def test_execute_turn_passes_prepared_state_to_run_conversation(
    tmp_path: Path,
) -> None:
    context = ConversationContext(key="guild:100:main", db_conversation_id=55)
    image_part = ContentPart.from_image_url(
        url="data:image/png;base64,YWJj",
        media_type="image/png",
    )
    attachment = AttachmentRef(
        filename="notes.txt",
        size=10,
        content_type="text/plain",
        source=None,
    )
    run_conversation = RecordingRunConversation(
        ConversationRunResult(
            text="ok",
            provider_state={"latest_response_id": "resp_new"},
        )
    )

    result = await execute_turn(
        _turn_request(context, image_part=image_part, attachment=attachment),
        dependencies=_dependencies(
            workspace_dir=tmp_path,
            run_conversation=run_conversation,
        ),
        config=replace(_config(), thread_handoff_suggest_after_tool_calls=9),
    )

    assert result.response_text == "ok"

    [call] = run_conversation.calls
    assert call["user_message"] == "draw this"
    assert call["context"] is context
    assert call["trust_tier"] is TrustTier.REGULAR
    assert call["user_name"] == "Alice"
    assert call["user_id"] == "123"
    assert call["provider_state"] == {}
    assert call["recalled_memories"] == "- Alice likes concise replies."
    assert call["skills_index"] == "## Skills"
    assert call["personal_skills_index"] == "## Personal Skills"
    assert call["user_persona"] == "Roleplay as a friendly space mechanic."
    assert call["input_parts"] == [image_part]
    assert call["edit_target_image"] == image_part
    assert call["attachments"] == [attachment]
    assert call["reply_context"] is None
    assert call["max_iterations"] == 7
    assert call["max_tokens"] == 1234
    assert call["thread_handoff_suggest_after_tool_calls"] == 9
    assert call["trigger_discord_message_id"] == "777"


@pytest.mark.asyncio
async def test_execute_turn_forwards_the_thread_scope(tmp_path: Path) -> None:
    """Pin the middle field-copy boundary in the thread-instructions chain.

    Losing ``parent_channel_id`` between ``prepare_turn`` and
    ``build_system_prompt`` silently resolves instructions against the thread id.
    """
    run_conversation = RecordingRunConversation(ConversationRunResult(text="ok"))

    await execute_turn(
        _turn_request(
            ConversationContext(key="guild:100:main", db_conversation_id=55),
            channel_id="77",
            thread_id="77",
            parent_channel_id="20",
        ),
        dependencies=_dependencies(
            workspace_dir=tmp_path,
            run_conversation=run_conversation,
        ),
        config=_config(),
    )

    [call] = run_conversation.calls
    assert call["channel_id"] == "77"
    assert call["thread_id"] == "77"
    assert call["parent_channel_id"] == "20"


@pytest.mark.asyncio
async def test_execute_turn_routes_stored_image_history_to_image_provider(
    tmp_path: Path,
) -> None:
    context = ConversationContext(key="guild:100:main", db_conversation_id=55)
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
    text_provider = StubProvider(
        provider_key="text",
        model="text-model",
        capabilities={ProviderCapability.TEXT},
    )
    image_provider = StubProvider(
        provider_key="vision",
        model="vision-model",
        capabilities={ProviderCapability.TEXT, ProviderCapability.IMAGE_INPUT},
    )
    resolver_calls: list[bool] = []

    def resolve_chat_provider(*, images: bool = False) -> StubProvider:
        resolver_calls.append(images)
        return image_provider if images else text_provider

    run_conversation = RecordingRunConversation(ConversationRunResult(text="ok"))

    result = await execute_turn(
        _turn_request(context),
        dependencies=_dependencies(
            workspace_dir=tmp_path,
            run_conversation=run_conversation,
            provider=text_provider,
            chat_provider_resolver=resolve_chat_provider,
            chat_model_name_resolver=lambda *, images=False: (
                "vision-chat" if images else "text-chat"
            ),
        ),
        config=_config(),
    )

    assert result.response_text == "ok"
    assert resolver_calls == [True]
    [call] = run_conversation.calls
    assert call["provider"] is image_provider


@pytest.mark.asyncio
async def test_execute_turn_distills_images_for_text_model_and_reuses_cache(
    tmp_path: Path,
) -> None:
    class DistillingProvider(StubProvider):
        def __init__(self) -> None:
            super().__init__(
                provider_key="vision",
                model="vision-model",
                capabilities={ProviderCapability.TEXT, ProviderCapability.IMAGE_INPUT},
            )
            self.requests: list[ProviderRequest] = []

        async def run_turn(self, request: ProviderRequest) -> ProviderResponse:
            self.requests.append(request)
            return ProviderResponse(
                content=("Image 1: A red sign reading STOP [120, 80, 610, 740], confidence high."),
                model="vision-served",
                usage={"input_tokens": 40, "output_tokens": 18},
            )

    context = ConversationContext(key="guild:100:main", db_conversation_id=55)
    image_part = ContentPart.from_image_url(
        url="data:image/png;base64,YWJj",
        media_type="image/png",
    )
    text_provider = StubProvider(
        provider_key="text",
        model="text-model",
        capabilities={ProviderCapability.TEXT},
    )
    image_provider = DistillingProvider()
    cache = RecordingImageDistillationCache()

    def resolve_chat_provider(*, images: bool = False) -> StubProvider:
        return image_provider if images else text_provider

    dependencies = _dependencies(
        workspace_dir=tmp_path,
        run_conversation=RecordingRunConversation(ConversationRunResult(text="unused")),
        provider=text_provider,
        chat_provider_resolver=resolve_chat_provider,
        chat_model_name_resolver=lambda *, images=False: "vision-chat" if images else "text-chat",
        image_distillation_store=cache,
    )
    first_run = RecordingRunConversation(ConversationRunResult(text="first"))
    dependencies = replace(dependencies, run_conversation=first_run)

    first_result = await execute_turn(
        _turn_request(context, image_part=image_part),
        dependencies=dependencies,
        config=_config(),
    )

    assert first_result.response_text == "first"
    assert len(image_provider.requests) == 1
    distillation_request = image_provider.requests[0]
    assert distillation_request.requested_capabilities == {ProviderCapability.IMAGE_INPUT}
    assert "0-1000 grid" in distillation_request.system_prompt
    assert any(
        part.type is ContentPartType.IMAGE for part in distillation_request.current_user_parts
    )
    [first_call] = first_run.calls
    assert first_call["provider"] is text_provider
    assert all(
        part.type is not ContentPartType.IMAGE
        for message in first_call["context"].messages
        for part in message.content
    )
    assert all(part.type is not ContentPartType.IMAGE for part in first_call["input_parts"])
    assert is_image_caption(first_call["input_parts"][-1].text)
    assert "[120, 80, 610, 740]" in first_call["input_parts"][-1].text
    assert len(cache.writes) == 1
    context.messages.append(
        ConversationMessage(
            role="user",
            content=[ContentPart.from_text("Alice: inspect this"), image_part],
        )
    )

    second_run = RecordingRunConversation(ConversationRunResult(text="cached"))
    second_result = await execute_turn(
        _turn_request(context),
        dependencies=replace(dependencies, run_conversation=second_run),
        config=_config(),
    )

    assert second_result.response_text == "cached"
    assert len(image_provider.requests) == 1
    [second_call] = second_run.calls
    assert second_call["provider"] is text_provider
    assert is_image_caption(second_call["input_parts"][-1].text)
    assert "[120, 80, 610, 740]" in second_call["input_parts"][-1].text


@pytest.mark.asyncio
async def test_execute_turn_skips_images_already_carrying_a_stored_caption(
    tmp_path: Path,
) -> None:
    """A captioned history message is already described.

    Describing it again would spend a vision call and put the same picture in the
    request twice. With nothing left to describe the turn must still drop the image
    parts and run on the text model rather than falling back to the vision route.
    """

    class UnusedVisionProvider(StubProvider):
        def __init__(self) -> None:
            super().__init__(
                provider_key="vision",
                model="vision-model",
                capabilities={ProviderCapability.TEXT, ProviderCapability.IMAGE_INPUT},
            )
            self.requests: list[ProviderRequest] = []

        async def run_turn(self, request: ProviderRequest) -> ProviderResponse:
            self.requests.append(request)
            raise AssertionError("a captioned image must not be described again")

    caption = format_image_caption("Image 1: A red sign reading STOP.")
    image_part = ContentPart.from_image_url(
        url="data:image/png;base64,YWJj",
        media_type="image/png",
    )
    context = ConversationContext(key="guild:100:main", db_conversation_id=55)
    context.messages.append(
        ConversationMessage(
            role="user",
            content=[
                ContentPart.from_text("Alice: look at this"),
                image_part,
                ContentPart.from_text(caption),
            ],
        )
    )

    text_provider = StubProvider(
        provider_key="text",
        model="text-model",
        capabilities={ProviderCapability.TEXT},
    )
    image_provider = UnusedVisionProvider()
    cache = RecordingImageDistillationCache()
    run = RecordingRunConversation(ConversationRunResult(text="answered"))

    dependencies = _dependencies(
        workspace_dir=tmp_path,
        run_conversation=run,
        provider=text_provider,
        chat_provider_resolver=lambda *, images=False: image_provider if images else text_provider,
        chat_model_name_resolver=lambda *, images=False: "vision-chat" if images else "text-chat",
        image_distillation_store=cache,
    )

    result = await execute_turn(_turn_request(context), dependencies=dependencies, config=_config())

    assert result.response_text == "answered"
    assert image_provider.requests == []
    assert cache.writes == []
    [call] = run.calls
    assert call["provider"] is text_provider
    history_parts = [part for message in call["context"].messages for part in message.content]
    assert all(part.type is not ContentPartType.IMAGE for part in history_parts)
    assert ContentPart.from_text(caption) in history_parts


@pytest.mark.asyncio
async def test_execute_turn_preserves_vision_route_when_distillation_fails(
    tmp_path: Path,
) -> None:
    class FailingVisionProvider(StubProvider):
        def __init__(self) -> None:
            super().__init__(
                provider_key="vision",
                model="vision-model",
                capabilities={ProviderCapability.TEXT, ProviderCapability.IMAGE_INPUT},
            )
            self.calls = 0

        async def run_turn(self, request: ProviderRequest) -> ProviderResponse:
            _ = request
            self.calls += 1
            raise RuntimeError("distiller unavailable")

    context = ConversationContext(key="guild:100:main", db_conversation_id=55)
    image_part = ContentPart.from_image_url(
        url="data:image/png;base64,YWJj",
        media_type="image/png",
    )
    context.messages.append(ConversationMessage(role="user", content=[image_part]))
    text_provider = StubProvider(
        provider_key="text",
        model="text-model",
        capabilities={ProviderCapability.TEXT},
    )
    image_provider = FailingVisionProvider()
    resolver_calls: list[bool] = []

    def resolve_chat_provider(*, images: bool = False) -> StubProvider:
        resolver_calls.append(images)
        return image_provider if images else text_provider

    run_conversation = RecordingRunConversation(ConversationRunResult(text="fallback"))
    result = await execute_turn(
        _turn_request(context),
        dependencies=_dependencies(
            workspace_dir=tmp_path,
            run_conversation=run_conversation,
            provider=text_provider,
            chat_provider_resolver=resolve_chat_provider,
            image_distillation_store=RecordingImageDistillationCache(),
        ),
        config=_config(),
    )

    assert result.response_text == "fallback"
    assert image_provider.calls == 1
    assert resolver_calls == [False, True, True]
    [call] = run_conversation.calls
    assert call["provider"] is image_provider
    assert any(
        part.type is ContentPartType.IMAGE
        for message in call["context"].messages
        for part in message.content
    )


@pytest.mark.asyncio
async def test_execute_turn_passes_command_template_to_run_conversation(
    tmp_path: Path,
) -> None:
    context = ConversationContext(key="guild:100:main")
    run_conversation = RecordingRunConversation(ConversationRunResult(text="ok"))

    await execute_turn(
        _turn_request(context),
        dependencies=_dependencies(
            workspace_dir=tmp_path,
            run_conversation=run_conversation,
        ),
        config=TurnExecutionConfig(
            max_iterations=7,
            max_tokens=1234,
            command_template="translate",
        ),
    )

    [call] = run_conversation.calls
    assert call["command_template"] == "translate"


@pytest.mark.asyncio
async def test_execute_turn_passes_reply_context_to_run_conversation(
    tmp_path: Path,
) -> None:
    context = ConversationContext(key="guild:100:main")
    reply_context = ReplyContext(
        referenced_message_id="444",
        author_name="Bob",
        text="message being replied to",
    )
    run_conversation = RecordingRunConversation(ConversationRunResult(text="ok"))

    await execute_turn(
        _turn_request(context, reply_context=reply_context),
        dependencies=_dependencies(
            workspace_dir=tmp_path,
            run_conversation=run_conversation,
        ),
        config=_config(),
    )

    [call] = run_conversation.calls
    assert call["reply_context"] == reply_context


@pytest.mark.asyncio
async def test_execute_turn_passes_activity_reporter_to_run_conversation(
    tmp_path: Path,
) -> None:
    context = ConversationContext(key="guild:100:main")

    async def reporter(update: ActivityUpdate) -> None:
        _ = update

    run_conversation = RecordingRunConversation(ConversationRunResult(text="ok"))

    await execute_turn(
        _turn_request(context),
        dependencies=_dependencies(
            workspace_dir=tmp_path,
            run_conversation=run_conversation,
            activity_reporter=reporter,
        ),
        config=_config(),
    )

    [call] = run_conversation.calls
    assert call["activity_reporter"] is reporter


@pytest.mark.asyncio
async def test_execute_turn_suppresses_moderated_narration_before_activity_send(
    tmp_path: Path,
) -> None:
    context = ConversationContext(key="guild:100:main")
    recorded_steps: list[tuple[str, list[str]]] = []

    class Reporter:
        async def __call__(self, update: ActivityUpdate) -> None:
            _ = update

        async def commit_step(self, narration: str, tool_names: list[str]) -> None:
            recorded_steps.append((narration, tool_names))

    class NarratingRunConversation(RecordingRunConversation):
        async def __call__(
            self,
            request: ConversationRunRequest | None = None,
            **kwargs: Any,
        ) -> ConversationRunResult:
            reporter = cast(
                Any,
                request.activity_reporter if request is not None else kwargs["activity_reporter"],
            )
            await reporter.commit_step("unsafe narration", ["browse_tools"])
            return await super().__call__(request=request, **kwargs)

    moderation = RecordingModerationService(blocked_text="unsafe narration")
    run_conversation = NarratingRunConversation(ConversationRunResult(text="safe final"))

    result = await execute_turn(
        _turn_request(context),
        dependencies=_dependencies(
            workspace_dir=tmp_path,
            run_conversation=run_conversation,
            activity_reporter=Reporter(),
            moderation_service=moderation,
        ),
        config=_config(),
    )

    assert result.response_text == "safe final"
    assert recorded_steps == []
    assert [call["text"] for call in moderation.calls] == [
        "unsafe narration",
        "safe final",
    ]


@pytest.mark.asyncio
async def test_execute_turn_suppresses_moderated_plan_updates(tmp_path: Path) -> None:
    context = ConversationContext(key="guild:100:main")
    recorded_plans: list[list[dict[str, str]]] = []

    class Reporter:
        async def __call__(self, update: ActivityUpdate) -> None:
            _ = update

        async def commit_step(self, narration: str, tool_names: list[str]) -> None:
            _ = (narration, tool_names)

        async def update_plan(self, steps: list[dict[str, str]]) -> None:
            recorded_plans.append(steps)

    class PlanningRunConversation(RecordingRunConversation):
        async def __call__(
            self,
            request: ConversationRunRequest | None = None,
            **kwargs: Any,
        ) -> ConversationRunResult:
            reporter = cast(
                Any,
                request.activity_reporter if request is not None else kwargs["activity_reporter"],
            )
            await reporter.update_plan([{"content": "unsafe step", "status": "pending"}])
            await reporter.update_plan([{"content": "safe step", "status": "pending"}])
            return await super().__call__(request=request, **kwargs)

    moderation = RecordingModerationService(blocked_text="unsafe step")
    run_conversation = PlanningRunConversation(ConversationRunResult(text="ok"))

    await execute_turn(
        _turn_request(context),
        dependencies=_dependencies(
            workspace_dir=tmp_path,
            run_conversation=run_conversation,
            activity_reporter=Reporter(),
            moderation_service=moderation,
        ),
        config=_config(),
    )

    assert [[s["content"] for s in plan] for plan in recorded_plans] == [["safe step"]]


@pytest.mark.asyncio
async def test_execute_turn_moderates_plan_only_reporter(tmp_path: Path) -> None:
    # A reporter supporting only plan updates must still get the moderation
    # wrapper; narration steps aimed at it are dropped without a crash.
    context = ConversationContext(key="guild:100:main")
    recorded_plans: list[list[dict[str, str]]] = []

    class PlanOnlyReporter:
        async def __call__(self, update: ActivityUpdate) -> None:
            _ = update

        async def update_plan(self, steps: list[dict[str, str]]) -> None:
            recorded_plans.append(steps)

    class PlanningRunConversation(RecordingRunConversation):
        async def __call__(
            self,
            request: ConversationRunRequest | None = None,
            **kwargs: Any,
        ) -> ConversationRunResult:
            reporter = cast(
                Any,
                request.activity_reporter if request is not None else kwargs["activity_reporter"],
            )
            await reporter.commit_step("narration", ["t1"])
            await reporter.update_plan([{"content": "unsafe step", "status": "pending"}])
            return await super().__call__(request=request, **kwargs)

    moderation = RecordingModerationService(blocked_text="unsafe step")
    run_conversation = PlanningRunConversation(ConversationRunResult(text="ok"))

    await execute_turn(
        _turn_request(context),
        dependencies=_dependencies(
            workspace_dir=tmp_path,
            run_conversation=run_conversation,
            activity_reporter=PlanOnlyReporter(),
            moderation_service=moderation,
        ),
        config=_config(),
    )

    assert recorded_plans == []
    assert "unsafe step" in [call["text"] for call in moderation.calls]


@pytest.mark.asyncio
async def test_moderated_reporter_skips_plan_for_narration_only_delegate() -> None:
    from agent.turn import _ModeratedActivityReporter

    class NarrationOnly:
        async def __call__(self, update: ActivityUpdate) -> None:
            _ = update

        async def commit_step(self, narration: str, tool_names: list[str]) -> None:
            _ = (narration, tool_names)

    moderation = RecordingModerationService()
    wrapper = _ModeratedActivityReporter(
        NarrationOnly(),
        moderation_service=moderation,
        user_id="1",
        channel_id="2",
        thread_id=None,
        trust_tier=TrustTier.MEMBER,
    )

    await wrapper.update_plan([{"content": "a", "status": "pending"}])

    # Delegate can't render plans, so the wrapper bails before spending a
    # moderation call.
    assert moderation.calls == []


@pytest.mark.asyncio
async def test_moderated_reporter_suppresses_plan_when_moderation_fails() -> None:
    from agent.turn import _ModeratedActivityReporter

    class FailingModeration(RecordingModerationService):
        async def check(self, **kwargs: Any) -> ModerationDecision:
            raise RuntimeError("moderation down")

    recorded: list[list[dict[str, str]]] = []

    class Reporter:
        async def __call__(self, update: ActivityUpdate) -> None:
            _ = update

        async def commit_step(self, narration: str, tool_names: list[str]) -> None:
            _ = (narration, tool_names)

        async def update_plan(self, steps: list[dict[str, str]]) -> None:
            recorded.append(steps)

    wrapper = _ModeratedActivityReporter(
        Reporter(),
        moderation_service=FailingModeration(),
        user_id="1",
        channel_id="2",
        thread_id=None,
        trust_tier=TrustTier.MEMBER,
    )

    await wrapper.update_plan([{"content": "a", "status": "pending"}])

    assert recorded == []


@pytest.mark.asyncio
async def test_execute_turn_writes_generated_assets_and_clears_pending_outputs(
    tmp_path: Path,
) -> None:
    manager = WorkspaceManager(tmp_path)
    queued = manager.user_files_dir(WorkspaceKey("123__999")) / "tool-output.txt"
    queued.write_text("tool output", encoding="utf-8")
    context = ConversationContext(key="guild:100:main")
    context.pending_output_files.append(str(queued))
    context.pending_allowed_file_roots.append(str(queued.parent))
    asset = GeneratedAsset(
        kind="image",
        media_type="image/png",
        data_base64="YWJj",
        suggested_filename="image.png",
    )
    run_conversation = RecordingRunConversation(
        ConversationRunResult(text="made an image", generated_assets=[asset])
    )
    asset_writer = RecordingAssetWriter([Path("image.png")])

    result = await execute_turn(
        _turn_request(context),
        dependencies=_dependencies(
            workspace_dir=tmp_path,
            run_conversation=run_conversation,
            asset_writer=asset_writer,
            workspace_manager=manager,
            workspace_locks=UserLocks(),
        ),
        config=_config(),
    )

    # The manager owns the layout: one fresh per-job dir under the conversation's
    # generated root, so the assets of two turns can never collide.
    (call,) = asset_writer.calls
    assert call["generated_assets"] == [asset]
    generated_root = call["output_dir"]
    assert generated_root.parent == tmp_path / "generated" / "guild_100_main"
    assert generated_root.name.startswith("native-")
    assert str(generated_root / "image.png") in result.output_files
    # The queued tool output survives alongside the generated asset.
    assert len(result.output_files) == 2
    assert str(generated_root.resolve()) in result.allowed_file_roots
    assert context.pending_output_files == []
    assert context.pending_allowed_file_roots == []


@pytest.mark.asyncio
async def test_execute_turn_offloads_asset_writes_off_event_loop(tmp_path: Path) -> None:
    import threading

    loop_thread_id = threading.get_ident()
    captured: dict[str, int] = {}

    class _ThreadCapturingWriter:
        calls: list[dict[str, Any]] = []

        def __call__(
            self, generated_assets: list[GeneratedAsset], *, output_dir: Path
        ) -> list[Path]:
            captured["thread_id"] = threading.get_ident()
            return [output_dir / "image.png"]

    context = ConversationContext(key="guild:100:main")
    asset = GeneratedAsset(
        kind="image",
        media_type="image/png",
        data_base64="YWJj",
        suggested_filename="image.png",
    )
    run_conversation = RecordingRunConversation(
        ConversationRunResult(text="img", generated_assets=[asset])
    )

    await execute_turn(
        _turn_request(context),
        dependencies=_dependencies(
            workspace_dir=tmp_path,
            run_conversation=run_conversation,
            asset_writer=cast(Any, _ThreadCapturingWriter()),
        ),
        config=_config(),
    )

    assert captured["thread_id"] != loop_thread_id


@pytest.mark.asyncio
async def test_timed_out_generated_asset_worker_cleans_only_its_new_files(
    tmp_path: Path,
) -> None:
    import asyncio
    import threading

    from agent.core import ConversationTurnTimeoutError

    worker_started = threading.Event()
    allow_worker_finish = threading.Event()
    worker_finished = threading.Event()
    context = ConversationContext(key="guild:100:main")
    generated_root = tmp_path / "generated" / "guild_100_main"
    generated_root.mkdir(parents=True)
    preexisting_generated_asset = generated_root / "late.png"
    preexisting_generated_asset.write_bytes(b"original")
    existing_workspace_file = tmp_path / "files" / "keep.txt"
    existing_workspace_file.parent.mkdir(parents=True)
    existing_workspace_file.write_text("keep", encoding="utf-8")
    context.pending_output_files.append(str(existing_workspace_file))
    context.pending_allowed_file_roots.append(str(existing_workspace_file.parent))
    asset = GeneratedAsset(
        kind="image",
        media_type="image/png",
        data_base64="YWJj",
        suggested_filename="late.png",
    )

    class LateWriter:
        def __call__(
            self,
            generated_assets: list[GeneratedAsset],
            *,
            output_dir: Path,
        ) -> list[Path]:
            assert generated_assets == [asset]
            worker_started.set()
            allow_worker_finish.wait(timeout=1.0)
            paths = write_generated_assets(generated_assets, output_dir=output_dir)
            worker_finished.set()
            return paths

    run_conversation = RecordingRunConversation(
        ConversationRunResult(text="made an image", generated_assets=[asset])
    )
    task = asyncio.create_task(
        execute_turn(
            _turn_request(context),
            dependencies=_dependencies(
                workspace_dir=tmp_path,
                run_conversation=run_conversation,
                asset_writer=cast(Any, LateWriter()),
            ),
            config=TurnExecutionConfig(
                max_iterations=7,
                max_tokens=1234,
                timeout_seconds=0.01,
            ),
        )
    )
    assert await asyncio.to_thread(worker_started.wait, 1.0)
    with pytest.raises(ConversationTurnTimeoutError):
        await asyncio.wait_for(task, timeout=1.0)

    # Root-visible execution has timed out, but the shielded worker drains under
    # its child activity lifetime. Its eventual result is cleanup-only.
    allow_worker_finish.set()
    assert await asyncio.to_thread(worker_finished.wait, 1.0)
    for _ in range(20):
        if not (generated_root / "late-2.png").exists():
            break
        await asyncio.sleep(0)

    assert existing_workspace_file.read_text(encoding="utf-8") == "keep"
    assert preexisting_generated_asset.read_bytes() == b"original"
    assert not (generated_root / "late-2.png").exists()


@pytest.mark.asyncio
async def test_execute_turn_surfaces_pending_embed(tmp_path: Path) -> None:
    context = ConversationContext(key="guild:100:main")
    spec = EmbedSpec(title="Hi")
    context.pending_embed = spec
    run_conversation = RecordingRunConversation(ConversationRunResult(text=""))

    result = await execute_turn(
        _turn_request(context),
        dependencies=_dependencies(
            workspace_dir=tmp_path,
            run_conversation=run_conversation,
        ),
        config=_config(),
    )

    assert result.embed is spec
    assert result.response_text == ""
    assert context.pending_embed is None


@pytest.mark.asyncio
async def test_execute_turn_materializes_embed_attachment_onto_output_files(
    tmp_path: Path,
) -> None:
    manager = WorkspaceManager(tmp_path)
    files_dir = manager.user_files_dir(WorkspaceKey("123__999"))
    attachment = files_dir / "c.png"
    attachment.write_bytes(b"png bytes")
    context = ConversationContext(key="guild:100:main")
    context.pending_embed = EmbedSpec(title="Hi", image="attachment://c.png")
    context.pending_embed_attachment = EmbedAttachment(
        path=str(attachment), root=str(files_dir), filename="c.png"
    )
    run_conversation = RecordingRunConversation(ConversationRunResult(text=""))

    result = await execute_turn(
        _turn_request(context),
        dependencies=_dependencies(
            workspace_dir=tmp_path,
            run_conversation=run_conversation,
            workspace_manager=manager,
            workspace_locks=UserLocks(),
        ),
        config=_config(),
    )

    # Staging copies the attachment into a delivery dir; the embed keeps pointing
    # at it by filename, and the queued original is released.
    (staged,) = result.output_files
    assert Path(staged).name == "c.png"
    assert Path(staged).read_bytes() == b"png bytes"
    # The staging root is what the delivery boundary checks attachments against.
    assert result.allowed_file_roots == (str(Path(staged).parent),)
    assert context.pending_embed_attachment is None


@pytest.mark.asyncio
async def test_execute_turn_persists_only_new_activated_tools(tmp_path: Path) -> None:
    context = ConversationContext(
        key="guild:100:main",
        db_conversation_id=55,
        activated_tools={"openalex_lookup"},
    )
    context_manager = StubContextManager()

    class ActivatingRunConversation(RecordingRunConversation):
        async def __call__(
            self,
            request: ConversationRunRequest | None = None,
            **kwargs: Any,
        ) -> ConversationRunResult:
            result = await super().__call__(request=request, **kwargs)
            context = request.context if request is not None else kwargs["context"]
            context.activated_tools.update({"openalex_lookup", "wolfram_alpha"})
            return result

    run_conversation = ActivatingRunConversation(ConversationRunResult(text="ok"))

    result = await execute_turn(
        _turn_request(context),
        dependencies=_dependencies(
            workspace_dir=tmp_path,
            run_conversation=run_conversation,
            context_manager=context_manager,
        ),
        config=_config(),
    )

    assert result.response_text == "ok"
    assert context_manager.added_activated_tools == [(55, {"wolfram_alpha"})]


@pytest.mark.asyncio
async def test_execute_turn_skips_activation_persist_when_unchanged(tmp_path: Path) -> None:
    context = ConversationContext(
        key="guild:100:main",
        db_conversation_id=55,
        activated_tools={"openalex_lookup"},
    )
    context_manager = StubContextManager()
    run_conversation = RecordingRunConversation(ConversationRunResult(text="ok"))

    await execute_turn(
        _turn_request(context),
        dependencies=_dependencies(
            workspace_dir=tmp_path,
            run_conversation=run_conversation,
            context_manager=context_manager,
        ),
        config=_config(),
    )

    assert context_manager.added_activated_tools == []


@pytest.mark.asyncio
async def test_execute_turn_blocks_output_before_asset_writes_and_clears_pending_outputs(
    tmp_path: Path,
) -> None:
    manager = WorkspaceManager(tmp_path)
    files_dir = manager.user_files_dir(WorkspaceKey("123__999"))
    context = ConversationContext(key="guild:100:main")
    queued_image = files_dir / "tool-output.png"
    queued_image.write_bytes(b"\x89PNG\r\n\x1a\nqueued")
    context.pending_output_files.append(str(queued_image))
    context.pending_allowed_file_roots.append(str(files_dir))
    unsafe = files_dir / "unsafe.png"
    unsafe.write_bytes(b"\x89PNG\r\n\x1a\nunsafe")
    context.pending_embed = EmbedSpec(title="Unsafe", image="attachment://unsafe.png")
    context.pending_embed_attachment = EmbedAttachment(
        path=str(unsafe),
        root=str(files_dir),
        filename="unsafe.png",
    )
    asset = GeneratedAsset(
        kind="image",
        media_type="image/png",
        data_base64="YWJj",
        suggested_filename="image.png",
    )
    run_conversation = RecordingRunConversation(
        ConversationRunResult(text="unsafe output", generated_assets=[asset])
    )
    asset_writer = RecordingAssetWriter([Path("image.png")])
    moderation = RecordingModerationService(blocked=True)

    result = await execute_turn(
        _turn_request(context),
        dependencies=_dependencies(
            workspace_dir=tmp_path,
            run_conversation=run_conversation,
            asset_writer=asset_writer,
            moderation_service=moderation,
            workspace_manager=manager,
            workspace_locks=UserLocks(),
        ),
        config=_config(),
    )

    assert result.response_text == "output blocked"
    assert result.blocked_by_moderation is True
    assert result.output_files == ()
    assert result.allowed_file_roots == ()
    assert result.embed is None
    assert asset_writer.calls == []
    assert context.pending_output_files == []
    assert context.pending_allowed_file_roots == []
    assert context.pending_embed is None
    assert context.pending_embed_attachment is None
    # Generic queued images are delivery artifacts. The explicitly owned embed image
    # and native generated assets remain first-class moderation inputs.
    call = moderation.calls[0]
    assert call["text"] == "unsafe output"
    assert call["direction"] is Direction.OUTPUT
    assert call["generated_assets"] == [asset]
    assert call["embed"].title == "Unsafe"
    assert call["embed_attachment"].filename == "unsafe.png"
    assert "images" not in call


@pytest.mark.asyncio
async def test_execute_turn_skips_output_moderation_for_exempt_regular(
    tmp_path: Path,
) -> None:
    context = ConversationContext(key="guild:100:main")
    run_conversation = RecordingRunConversation(
        ConversationRunResult(text="unsafe but regular-exempt")
    )
    moderation = RecordingModerationService(
        blocked=True,
        output_exempt_tier=TrustTier.REGULAR,
    )

    result = await execute_turn(
        _turn_request(context, trust_tier=TrustTier.REGULAR),
        dependencies=_dependencies(
            workspace_dir=tmp_path,
            run_conversation=run_conversation,
            moderation_service=moderation,
        ),
        config=_config(),
    )

    assert result.response_text == "unsafe but regular-exempt"
    assert result.blocked_by_moderation is False
    assert moderation.calls == []


@pytest.mark.asyncio
async def test_execute_turn_keeps_output_moderation_for_member_below_exempt_tier(
    tmp_path: Path,
) -> None:
    context = ConversationContext(key="guild:100:main")
    run_conversation = RecordingRunConversation(ConversationRunResult(text="unsafe output"))
    moderation = RecordingModerationService(
        blocked=True,
        output_exempt_tier=TrustTier.REGULAR,
    )

    result = await execute_turn(
        _turn_request(context, trust_tier=TrustTier.MEMBER),
        dependencies=_dependencies(
            workspace_dir=tmp_path,
            run_conversation=run_conversation,
            moderation_service=moderation,
        ),
        config=_config(),
    )

    assert result.response_text == "output blocked"
    assert result.blocked_by_moderation is True
    assert moderation.calls[0]["trust_tier"] == "member"


@pytest.mark.asyncio
async def test_execute_turn_excludes_queued_utf8_workspace_files_from_moderation(
    tmp_path: Path,
) -> None:
    context = ConversationContext(key="guild:100:main")
    output_root = tmp_path / "workspaces" / "u"
    output_root.mkdir(parents=True)
    output_file = output_root / "tool-output.txt"
    output_file.write_text("unsafe attachment body", encoding="utf-8")
    context.pending_output_files.append(str(output_file))
    context.pending_allowed_file_roots.append(str(output_root))
    run_conversation = RecordingRunConversation(ConversationRunResult(text="see attached"))
    moderation = RecordingModerationService(blocked_text="unsafe attachment body")

    result = await execute_turn(
        _turn_request(context),
        dependencies=_dependencies(
            workspace_dir=tmp_path,
            run_conversation=run_conversation,
            moderation_service=moderation,
        ),
        config=_config(),
    )

    assert result.response_text == "see attached"
    assert result.blocked_by_moderation is False
    assert len(result.output_files) == 1
    assert Path(result.output_files[0]).name == "tool-output.txt"
    assert Path(result.output_files[0]).read_text(encoding="utf-8") == "unsafe attachment body"
    assert len(result.allowed_file_roots) == 1
    assert context.pending_output_files == []
    assert context.pending_allowed_file_roots == []
    assert moderation.calls[0]["text"] == "see attached"
    assert "images" not in moderation.calls[0]


@pytest.mark.asyncio
async def test_execute_turn_moderates_attachment_descriptions(tmp_path: Path) -> None:
    context = ConversationContext(key="guild:100:main")
    output_root = tmp_path / "workspaces" / "u"
    output_root.mkdir(parents=True)
    output_file = output_root / "visual.png"
    output_file.write_bytes(b"image")
    output_path = str(output_file)
    context.pending_output_files.append(output_path)
    context.pending_output_file_descriptions[output_path] = "unsafe attachment description"
    context.pending_allowed_file_roots.append(str(output_root))
    run_conversation = RecordingRunConversation(ConversationRunResult(text="see attached"))
    moderation = RecordingModerationService(blocked_text="unsafe attachment description")

    result = await execute_turn(
        _turn_request(context),
        dependencies=_dependencies(
            workspace_dir=tmp_path,
            run_conversation=run_conversation,
            moderation_service=moderation,
        ),
        config=_config(),
    )

    assert result.response_text == "output blocked"
    assert result.blocked_by_moderation is True
    assert result.output_files == ()
    assert result.output_file_descriptions == ()
    assert context.pending_output_file_descriptions == {}
    assert "Attachment descriptions:" in moderation.calls[0]["text"]


@pytest.mark.asyncio
async def test_execute_turn_delivers_opaque_binary_workspace_file(
    tmp_path: Path,
) -> None:
    context = ConversationContext(key="guild:100:main")
    output_root = tmp_path / "workspaces" / "u"
    output_root.mkdir(parents=True)
    output_file = output_root / "archive.zip"
    output_file.write_bytes(b"PK\x03\x04\x00binary\x00payload")
    context.pending_output_files.append(str(output_file))
    context.pending_allowed_file_roots.append(str(output_root))
    run_conversation = RecordingRunConversation(ConversationRunResult(text="see attached"))
    moderation = RecordingModerationService(blocked_text="binary")

    result = await execute_turn(
        _turn_request(context),
        dependencies=_dependencies(
            workspace_dir=tmp_path,
            run_conversation=run_conversation,
            moderation_service=moderation,
        ),
        config=_config(),
    )

    assert result.response_text == "see attached"
    assert result.blocked_by_moderation is False
    assert len(result.output_files) == 1
    assert Path(result.output_files[0]).name == "archive.zip"
    assert Path(result.output_files[0]).read_bytes() == b"PK\x03\x04\x00binary\x00payload"
    assert context.pending_output_files == []
    assert moderation.calls[0]["text"] == "see attached"
    assert "images" not in moderation.calls[0]


def test_execute_turn_forwards_compactor():
    captured = {}

    async def fake_run_conversation(**kwargs):
        request = kwargs.get("request")
        captured.update(request.__dict__ if isinstance(request, ConversationRunRequest) else kwargs)
        return ConversationRunResult(text="ok")

    sentinel = object()
    deps = make_turn_dependencies(
        run_conversation=fake_run_conversation,
        compactor=sentinel,
    )
    turn = TurnRequest(
        content="hi",
        context=ConversationContext(key="t"),
        trust_tier=TrustTier.MEMBER,
        user_id="1",
        user_name="C",
        guild_id=None,
        channel_id="c",
        thread_id=None,
        channel_name="general",
    )
    asyncio.run(
        execute_turn(
            turn,
            dependencies=deps,
            config=TurnExecutionConfig(max_iterations=3, max_tokens=256),
        )
    )
    assert captured["compactor"] is sentinel
