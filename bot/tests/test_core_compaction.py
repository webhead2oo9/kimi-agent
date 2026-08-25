from __future__ import annotations

import asyncio
import json

from agent.compaction import NOTE_PREFIX, CompactionConfig, Compactor
from agent.context import ConversationContext
from agent.core import ConversationRunRequest, run_conversation
from observability import events as ev
from providers.base import LLMProvider
from providers.types import (
    ProviderCapability,
    ProviderRequest,
    ProviderResponse,
    ToolCall,
)
from providers.errors import ProviderContextOverflowError
from tools.registry import MessageContext, ToolRegistry
from trust.tiers import TrustTier


class _Summarizer(LLMProvider):
    async def run_turn(self, request: ProviderRequest) -> ProviderResponse:
        return ProviderResponse(content="COMPACTED SUMMARY")


class _FailingSummarizer(LLMProvider):
    async def run_turn(self, request: ProviderRequest) -> ProviderResponse:
        raise RuntimeError("summarizer down")


class _ScriptedProvider(LLMProvider):
    provider_key = "scripted"
    model = "scripted"
    capabilities = {ProviderCapability.TEXT, ProviderCapability.TOOL_CALLING}

    def __init__(self, responses: list[ProviderResponse]) -> None:
        self.responses = responses
        self.requests: list[ProviderRequest] = []

    async def run_turn(self, request: ProviderRequest) -> ProviderResponse:
        self.requests.append(request)
        return self.responses.pop(0)


class _ServerSideScriptedProvider(_ScriptedProvider):
    capabilities = {
        ProviderCapability.TEXT,
        ProviderCapability.TOOL_CALLING,
        ProviderCapability.PREVIOUS_RESPONSE_ID,
        ProviderCapability.SERVER_SIDE_CONTEXT,
    }


class _OverflowProvider(LLMProvider):
    provider_key = "overflow"
    model = "overflow"
    capabilities = {ProviderCapability.TEXT, ProviderCapability.TOOL_CALLING}

    def __init__(self) -> None:
        self.requests: list[ProviderRequest] = []

    async def run_turn(self, request: ProviderRequest) -> ProviderResponse:
        self.requests.append(request)
        raise ProviderContextOverflowError("maximum context length exceeded")


def _registry() -> ToolRegistry:
    async def big(args: dict, ctx: MessageContext) -> str:
        return "Z" * 8000

    registry = ToolRegistry()
    registry.register(
        name="big",
        description="returns a big blob",
        parameters={"type": "object", "properties": {}},
        handler=big,
    )
    return registry


def test_compaction_shrinks_second_request():
    responses = [
        ProviderResponse(
            content="",
            tool_calls=[ToolCall(id="c1", name="big", arguments={})],
            finish_reason="tool_calls",
            usage={"prompt_tokens": 200000},
        ),
        ProviderResponse(
            content="",
            tool_calls=[ToolCall(id="c2", name="big", arguments={})],
            finish_reason="tool_calls",
            usage={"prompt_tokens": 5000},
        ),
        ProviderResponse(content="Done."),
    ]
    provider = _ScriptedProvider(responses)
    compactor = Compactor(
        CompactionConfig(
            trigger_tokens=1000,
            keep_recent_iterations=1,
            keep_recent_tokens=0,
            max_tokens=64,
        ),
        _Summarizer(),
    )
    result = asyncio.run(
        run_conversation(
            request=ConversationRunRequest(
                user_message="go",
                context=ConversationContext(key="t", user_name="C"),
                trust_tier=TrustTier.MEMBER,
                user_name="C",
                user_id="1",
                provider=provider,
                registry=_registry(),
                max_iterations=5,
                compactor=compactor,
            )
        )
    )
    assert result.text == "Done."
    last_msgs = provider.requests[-1].messages
    assert any(
        m.role == "user" and m.content and m.content[0].text.startswith(NOTE_PREFIX)
        for m in last_msgs
    )


def test_compaction_note_carries_live_plan_checklist():
    from tools.plan import init_plan_tool

    responses = [
        ProviderResponse(
            content="",
            tool_calls=[
                ToolCall(
                    id="c0",
                    name="plan",
                    arguments={"steps": ["find sources", "write summary"]},
                )
            ],
            finish_reason="tool_calls",
            usage={"prompt_tokens": 10},
        ),
        ProviderResponse(
            content="",
            tool_calls=[ToolCall(id="c1", name="big", arguments={})],
            finish_reason="tool_calls",
            usage={"prompt_tokens": 200000},
        ),
        ProviderResponse(content="Done."),
    ]
    provider = _ScriptedProvider(responses)
    registry = _registry()
    init_plan_tool(registry)
    compactor = Compactor(
        CompactionConfig(
            trigger_tokens=1000,
            keep_recent_iterations=1,
            keep_recent_tokens=0,
            max_tokens=64,
        ),
        _Summarizer(),
    )
    result = asyncio.run(
        run_conversation(
            request=ConversationRunRequest(
                user_message="go",
                context=ConversationContext(key="t", user_name="C"),
                trust_tier=TrustTier.MEMBER,
                user_name="C",
                user_id="1",
                provider=provider,
                registry=registry,
                max_iterations=5,
                compactor=compactor,
            )
        )
    )
    assert result.text == "Done."
    note = next(
        m
        for m in provider.requests[-1].messages
        if m.role == "user" and m.content and m.content[0].text.startswith(NOTE_PREFIX)
    )
    assert "Current checklist" in note.content[0].text
    assert "find sources" in note.content[0].text


def test_compaction_emits_tool_stream_event(tmp_path):
    log_path = tmp_path / "events.jsonl"

    responses = [
        ProviderResponse(
            content="",
            tool_calls=[ToolCall(id="c1", name="big", arguments={})],
            finish_reason="tool_calls",
            usage={"prompt_tokens": 200000},
        ),
        ProviderResponse(content="Done."),
    ]
    provider = _ScriptedProvider(responses)
    compactor = Compactor(
        CompactionConfig(
            trigger_tokens=1000,
            keep_recent_iterations=0,
            keep_recent_tokens=0,
            max_tokens=64,
        ),
        _Summarizer(),
    )

    async def run() -> None:
        ev.start_event_writer(str(log_path), max_field_bytes=8192, content_mode="full")
        try:
            await run_conversation(
                request=ConversationRunRequest(
                    user_message="go",
                    context=ConversationContext(key="t", user_name="C"),
                    trust_tier=TrustTier.MEMBER,
                    user_name="C",
                    user_id="1",
                    provider=provider,
                    registry=_registry(),
                    max_iterations=5,
                    compactor=compactor,
                )
            )
        finally:
            await ev.stop_event_writer()

    asyncio.run(run())

    lines = [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [line["type"] for line in lines] == ["tool_call", "compaction", "turn"]
    event = lines[1]
    assert event["reason"] == "threshold"
    assert event["iteration"] == 0
    # [user, assistant, tool] summarizes into [user, note] plus the request
    # re-anchor at the tail: message count breaks even while bytes shrink.
    assert event["before_messages"] == 3
    assert event["after_messages"] == 3
    assert event["note_chars"] == len("COMPACTED SUMMARY")


def test_compaction_event_counts_grow_on_elision_fallback(tmp_path):
    # Summarizer failure elides tool bodies in place (same message count) and the
    # request re-anchor is still appended, so after_messages exceeds
    # before_messages: the event reports message counts, not bytes.
    log_path = tmp_path / "events.jsonl"

    responses = [
        ProviderResponse(
            content="",
            tool_calls=[ToolCall(id="c1", name="big", arguments={})],
            finish_reason="tool_calls",
            usage={"prompt_tokens": 200000},
        ),
        ProviderResponse(content="Done."),
    ]
    provider = _ScriptedProvider(responses)
    compactor = Compactor(
        CompactionConfig(
            trigger_tokens=1000,
            keep_recent_iterations=0,
            keep_recent_tokens=0,
            max_tokens=64,
        ),
        _FailingSummarizer(),
    )

    async def run() -> None:
        ev.start_event_writer(str(log_path), max_field_bytes=8192, content_mode="full")
        try:
            await run_conversation(
                request=ConversationRunRequest(
                    user_message="go",
                    context=ConversationContext(key="t", user_name="C"),
                    trust_tier=TrustTier.MEMBER,
                    user_name="C",
                    user_id="1",
                    provider=provider,
                    registry=_registry(),
                    max_iterations=5,
                    compactor=compactor,
                )
            )
        finally:
            await ev.stop_event_writer()

    asyncio.run(run())

    lines = [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [line["type"] for line in lines] == ["tool_call", "compaction", "turn"]
    event = lines[1]
    assert event["reason"] == "threshold"
    assert event["after_messages"] == event["before_messages"] + 1
    assert event["note_chars"] == 0
    assert event["elided_tool_results"] + event["hard_truncated_tool_results"] >= 1


def test_compaction_resets_server_side_provider_state_after_rewrite():
    provider = _ServerSideScriptedProvider(
        [
            ProviderResponse(
                content="",
                tool_calls=[ToolCall(id="c1", name="big", arguments={})],
                finish_reason="tool_calls",
                usage={"prompt_tokens": 200000},
                provider_state={"latest_response_id": "resp-1"},
            ),
            ProviderResponse(content="Done."),
        ]
    )
    compactor = Compactor(
        CompactionConfig(
            trigger_tokens=1000,
            keep_recent_iterations=0,
            keep_recent_tokens=0,
            max_tokens=64,
        ),
        _Summarizer(),
    )

    result = asyncio.run(
        run_conversation(
            request=ConversationRunRequest(
                user_message="go",
                context=ConversationContext(key="t", user_name="C"),
                trust_tier=TrustTier.MEMBER,
                user_name="C",
                user_id="1",
                provider=provider,
                registry=_registry(),
                max_iterations=3,
                compactor=compactor,
            )
        )
    )

    assert result.text == "Done."
    assert provider.requests[1].provider_state == {}
    assert any(
        m.role == "user" and m.content and m.content[0].text.startswith(NOTE_PREFIX)
        for m in provider.requests[1].messages
    )


def test_no_compactor_is_inert():
    responses = [ProviderResponse(content="hi")]
    provider = _ScriptedProvider(responses)
    result = asyncio.run(
        run_conversation(
            request=ConversationRunRequest(
                user_message="hi",
                context=ConversationContext(key="t", user_name="C"),
                trust_tier=TrustTier.MEMBER,
                user_name="C",
                user_id="1",
                provider=provider,
                registry=ToolRegistry(),
            )
        )
    )
    assert result.text == "hi"


def test_initial_overflow_does_not_retry_without_compactable_turn_history():
    provider = _OverflowProvider()
    compactor = Compactor(
        CompactionConfig(
            trigger_tokens=1000,
            keep_recent_iterations=1,
            keep_recent_tokens=0,
            max_tokens=64,
        ),
        _Summarizer(),
    )

    result = asyncio.run(
        run_conversation(
            request=ConversationRunRequest(
                user_message="go",
                context=ConversationContext(key="t", user_name="C"),
                trust_tier=TrustTier.MEMBER,
                user_name="C",
                user_id="1",
                provider=provider,
                registry=ToolRegistry(),
                max_iterations=5,
                compactor=compactor,
            )
        )
    )

    assert len(provider.requests) == 1
    assert result.text == "maximum context length exceeded"


def test_emergency_compaction_retry_uses_safe_partial_completion_error():
    class StatusError(Exception):
        status_code = 403

    class OverflowThenRejectedProvider(LLMProvider):
        provider_key = "overflow-then-rejected"
        model = "overflow-then-rejected"
        capabilities = {ProviderCapability.TEXT, ProviderCapability.TOOL_CALLING}

        def __init__(self) -> None:
            self.calls = 0

        async def run_turn(self, request: ProviderRequest) -> ProviderResponse:
            self.calls += 1
            if self.calls == 1:
                return ProviderResponse(
                    tool_calls=[ToolCall(id="c1", name="big", arguments={})],
                    finish_reason="tool_calls",
                    usage={"prompt_tokens": 1},
                )
            if self.calls == 2:
                raise ProviderContextOverflowError("maximum context length exceeded")
            raise StatusError("private provider detail at /run/secrets/token")

    provider = OverflowThenRejectedProvider()
    compactor = Compactor(
        CompactionConfig(
            trigger_tokens=10_000_000,
            keep_recent_iterations=0,
            keep_recent_tokens=0,
            max_tokens=64,
        ),
        _Summarizer(),
    )

    result = asyncio.run(
        run_conversation(
            request=ConversationRunRequest(
                user_message="go",
                context=ConversationContext(key="t", user_name="C"),
                trust_tier=TrustTier.MEMBER,
                user_name="C",
                user_id="1",
                provider=provider,
                registry=_registry(),
                max_iterations=5,
                compactor=compactor,
            )
        )
    )

    assert provider.calls == 3
    assert "selected model is unavailable" in result.text
    assert "Earlier tool actions may already have completed" in result.text
    assert "contact the bot operator" in result.text
    assert "private provider detail" not in result.text
    assert "/run/secrets/token" not in result.text


def test_per_iteration_clamp_preserves_raw_tool_event(tmp_path):
    log_path = tmp_path / "events.jsonl"

    async def big_error(args: dict, ctx: MessageContext) -> str:
        return json.dumps({"error": "boom " + "Z" * 8000})

    registry = ToolRegistry()
    registry.register(
        name="big_error",
        description="returns a large error payload",
        parameters={"type": "object", "properties": {}},
        handler=big_error,
    )
    provider = _ScriptedProvider(
        [
            ProviderResponse(
                content="",
                tool_calls=[ToolCall(id="c1", name="big_error", arguments={})],
                finish_reason="tool_calls",
                usage={"prompt_tokens": 1},
            ),
            ProviderResponse(content="Done."),
        ]
    )
    compactor = Compactor(
        CompactionConfig(trigger_tokens=10_000_000, max_iteration_tool_output_tokens=10),
        _Summarizer(),
    )

    async def run() -> None:
        ev.start_event_writer(str(log_path), max_field_bytes=10000, content_mode="full")
        try:
            await run_conversation(
                request=ConversationRunRequest(
                    user_message="go",
                    context=ConversationContext(key="t", user_name="C"),
                    trust_tier=TrustTier.MEMBER,
                    user_name="C",
                    user_id="1",
                    provider=provider,
                    registry=registry,
                    max_iterations=3,
                    compactor=compactor,
                )
            )
        finally:
            await ev.stop_event_writer()

    asyncio.run(run())

    tool_events = [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if json.loads(line)["type"] == "tool_call"
    ]
    assert len(tool_events) == 1
    assert tool_events[0]["ok"] is False
    assert tool_events[0]["error"].startswith("boom")
    model_result = provider.requests[1].messages[-1].content[0].text
    assert "truncated (per-iteration budget)" in model_result
