from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import cast

import pytest

from agent.compaction import CompactionConfig, Compactor
from agent.context import ConversationContext
from agent.core import (
    THREAD_HANDOFF_ADVISORY_TAG,
    ConversationRunRequest,
    run_conversation,
)
from providers.base import LLMProvider
from providers.errors import ProviderContextOverflowError
from providers.types import (
    ContentPartType,
    ConversationMessage,
    ProviderCapability,
    ProviderRequest,
    ProviderResponse,
    ToolCall,
)
from tools.registry import MessageContext, ToolRegistry
from trust.tiers import TrustTier


class RecordingProvider(LLMProvider):
    provider_key = "recording"
    model = "test-model"
    capabilities = {ProviderCapability.TEXT, ProviderCapability.TOOL_CALLING}

    def __init__(self, responses: list[ProviderResponse]) -> None:
        self.responses = list(responses)
        self.requests: list[ProviderRequest] = []

    async def run_turn(self, request: ProviderRequest) -> ProviderResponse:
        self.requests.append(request)
        return self.responses.pop(0)


class _RecordingCompactor:
    def __init__(self, *, compact_normally: bool) -> None:
        self.config = CompactionConfig(keep_recent_iterations=1)
        self.compact_normally = compact_normally
        self.normal_inputs: list[list[ConversationMessage]] = []
        self.emergency_inputs: list[list[ConversationMessage]] = []

    def clamp_tool_output(
        self, running_chars: int, result: str, _tool_name: str
    ) -> tuple[str, int]:
        return result, running_chars + len(result)

    async def maybe_compact(self, **kwargs):
        messages = kwargs["turn_messages"]
        self.normal_inputs.append(messages)
        return list(messages) if self.compact_normally else messages

    async def emergency_compact(self, **kwargs):
        messages = kwargs["turn_messages"]
        self.emergency_inputs.append(messages)
        return list(messages)


class _OverflowAfterAdvisoryProvider(RecordingProvider):
    def __init__(self) -> None:
        super().__init__([])
        self.calls = 0

    async def run_turn(self, request: ProviderRequest) -> ProviderResponse:
        self.requests.append(request)
        self.calls += 1
        if self.calls == 1:
            return ProviderResponse(tool_calls=_calls("lookup", 5), finish_reason="tool_calls")
        if self.calls == 2:
            raise ProviderContextOverflowError("maximum context length exceeded")
        return ProviderResponse(content="done")


async def _ok(_args: dict, _ctx: MessageContext) -> str:
    return "ok"


def _registry(*, include_move: bool = True) -> ToolRegistry:
    registry = ToolRegistry()
    for name in ("lookup", "plan"):
        registry.register(
            name=name,
            description=name,
            parameters={"type": "object", "properties": {}},
            handler=_ok,
        )
    if include_move:
        registry.register(
            name="move_to_thread",
            description="Move this conversation to a thread",
            parameters={"type": "object", "properties": {}},
            handler=_ok,
        )
    return registry


def _calls(name: str, count: int, *, start: int = 0) -> list[ToolCall]:
    return [
        ToolCall(id=f"call-{index}", name=name, arguments={})
        for index in range(start, start + count)
    ]


def _message_advisory_count(messages: list[ConversationMessage]) -> int:
    return sum(
        1
        for message in messages
        for part in message.content
        if part.type is ContentPartType.TEXT
        and (part.text or "").startswith(THREAD_HANDOFF_ADVISORY_TAG)
    )


def _advisory_count(request: ProviderRequest) -> int:
    return _message_advisory_count(request.messages)


def _request(
    provider: RecordingProvider,
    registry: ToolRegistry,
    *,
    threshold: int,
    context: ConversationContext | None = None,
    thread_id: str | None = None,
    compactor: Compactor | None = None,
    checkpoint_sink: (
        Callable[
            [list[ConversationMessage], dict, list[dict[str, str]]],
            Awaitable[None],
        ]
        | None
    ) = None,
) -> ConversationRunRequest:
    return ConversationRunRequest(
        user_message="Investigate it",
        context=context or ConversationContext(key="guild:channel"),
        trust_tier=TrustTier.MEMBER,
        user_name="Alice",
        user_id="user-1",
        provider=cast(LLMProvider, provider),
        registry=registry,
        guild_id="guild-1",
        channel_id="channel-1",
        thread_id=thread_id,
        thread_handoff_suggest_after_tool_calls=threshold,
        compactor=compactor,
        checkpoint_sink=checkpoint_sink,
    )


@pytest.mark.asyncio
async def test_advisory_is_append_only_once_and_not_retained() -> None:
    provider = RecordingProvider(
        [
            ProviderResponse(tool_calls=_calls("lookup", 5)),
            ProviderResponse(tool_calls=_calls("lookup", 1, start=5)),
            ProviderResponse(content="done"),
        ]
    )
    context = ConversationContext(key="guild:channel")

    result = await run_conversation(_request(provider, _registry(), threshold=5, context=context))

    assert result.text == "done"
    assert len(provider.requests) == 3
    assert _advisory_count(provider.requests[0]) == 0
    assert _advisory_count(provider.requests[1]) == 1
    assert _advisory_count(provider.requests[2]) == 1
    # The advisory changes only the growing message suffix, never the tool schema.
    assert provider.requests[0].tools == provider.requests[1].tools
    assert provider.requests[1].tools == provider.requests[2].tools
    assert _message_advisory_count(context.messages) == 0


@pytest.mark.asyncio
async def test_advisory_is_not_written_to_durable_checkpoints() -> None:
    provider = RecordingProvider(
        [
            ProviderResponse(tool_calls=_calls("lookup", 5)),
            ProviderResponse(content="done"),
        ]
    )
    checkpoints: list[list[ConversationMessage]] = []

    async def checkpoint(
        messages: list[ConversationMessage],
        _provider_state: dict,
        _plan: list[dict[str, str]],
    ) -> None:
        checkpoints.append(messages)

    result = await run_conversation(
        _request(provider, _registry(), threshold=5, checkpoint_sink=checkpoint)
    )

    assert result.text == "done"
    assert len(checkpoints) == 1
    assert _message_advisory_count(checkpoints[0]) == 0
    assert _advisory_count(provider.requests[1]) == 1


@pytest.mark.asyncio
async def test_compaction_never_receives_or_retains_the_advisory() -> None:
    provider = RecordingProvider(
        [
            ProviderResponse(tool_calls=_calls("lookup", 5)),
            ProviderResponse(content="done"),
        ]
    )
    compactor = _RecordingCompactor(compact_normally=True)
    context = ConversationContext(key="guild:channel")

    result = await run_conversation(
        _request(
            provider,
            _registry(),
            threshold=5,
            context=context,
            compactor=cast(Compactor, compactor),
        )
    )

    assert result.text == "done"
    assert compactor.normal_inputs
    assert all(_message_advisory_count(messages) == 0 for messages in compactor.normal_inputs)
    assert _advisory_count(provider.requests[1]) == 1
    assert _message_advisory_count(context.messages) == 0


@pytest.mark.asyncio
async def test_emergency_compaction_drops_the_advisory_before_summarizing() -> None:
    provider = _OverflowAfterAdvisoryProvider()
    compactor = _RecordingCompactor(compact_normally=False)
    context = ConversationContext(key="guild:channel")

    result = await run_conversation(
        _request(
            provider,
            _registry(),
            threshold=5,
            context=context,
            compactor=cast(Compactor, compactor),
        )
    )

    assert result.text == "done"
    assert _advisory_count(provider.requests[1]) == 1
    assert compactor.emergency_inputs
    assert all(_message_advisory_count(messages) == 0 for messages in compactor.emergency_inputs)
    assert _advisory_count(provider.requests[2]) == 0
    assert _message_advisory_count(context.messages) == 0


@pytest.mark.asyncio
async def test_planning_calls_do_not_reach_the_threshold() -> None:
    provider = RecordingProvider(
        [
            ProviderResponse(tool_calls=_calls("plan", 5)),
            ProviderResponse(content="done"),
        ]
    )

    await run_conversation(_request(provider, _registry(), threshold=1))

    assert _advisory_count(provider.requests[1]) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("threshold", "include_move", "thread_id"),
    [
        (0, True, None),
        (1, False, None),
        (1, True, "thread-1"),
    ],
)
async def test_advisory_requires_configuration_and_an_eligible_visible_tool(
    threshold: int,
    include_move: bool,
    thread_id: str | None,
) -> None:
    provider = RecordingProvider(
        [
            ProviderResponse(tool_calls=_calls("lookup", 1)),
            ProviderResponse(content="done"),
        ]
    )

    await run_conversation(
        _request(
            provider,
            _registry(include_move=include_move),
            threshold=threshold,
            thread_id=thread_id,
        )
    )

    assert _advisory_count(provider.requests[1]) == 0
