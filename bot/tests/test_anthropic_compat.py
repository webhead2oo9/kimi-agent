from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from providers.anthropic_compat import AnthropicCompatProvider
from providers.types import ContentPart, ConversationMessage, ProviderRequest, ToolCall

CANNED = {
    "type": "message",
    "role": "assistant",
    "model": "minimax-m3",
    "stop_reason": "tool_use",
    "content": [
        {"type": "text", "text": "Let me check."},
        {
            "type": "tool_use",
            "id": "toolu_1",
            "name": "get_weather",
            "input": {"city": "Paris"},
        },
    ],
    "usage": {"input_tokens": 42, "output_tokens": 7},
}


def _provider(handler, *, prompt_caching: bool = False) -> AnthropicCompatProvider:
    return AnthropicCompatProvider(
        base_url="https://opencode.ai/zen/go/v1",
        model="minimax-m3",
        api_key="sk-zen",
        prompt_caching=prompt_caching,
        transport=httpx.MockTransport(handler),
    )


def _request() -> ProviderRequest:
    return ProviderRequest(
        conversation_id=1,
        system_prompt="You are Kimi.",
        messages=[
            ConversationMessage(role="user", content=[ContentPart.from_text("hi")]),
        ],
        current_user_parts=[ContentPart.from_text("weather in Paris?")],
        tools=[{"name": "get_weather", "description": "w", "parameters": {"type": "object"}}],
        max_tokens=256,
    )


@pytest.mark.asyncio
async def test_parses_text_and_tool_use() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=CANNED)

    resp = await _provider(handler).run_turn(_request())
    assert resp.model == "minimax-m3"

    assert resp.content == "Let me check."
    assert resp.finish_reason == "tool_use"
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].id == "toolu_1"
    assert resp.tool_calls[0].name == "get_weather"
    assert resp.tool_calls[0].arguments == {"city": "Paris"}
    assert resp.usage == {"input_tokens": 42, "output_tokens": 7}
    assert resp.raw_message["role"] == "assistant"


@pytest.mark.asyncio
async def test_max_tokens_stop_reason_normalizes_to_length() -> None:
    truncated = dict(CANNED, stop_reason="max_tokens", content=[{"type": "text", "text": "par"}])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=truncated)

    resp = await _provider(handler).run_turn(_request())

    # agent/core.py and research/engine.py detect truncation via "length".
    assert resp.finish_reason == "length"


@pytest.mark.asyncio
async def test_request_targets_messages_path_with_api_key_auth() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = request.headers
        captured["json"] = json.loads(request.content)
        return httpx.Response(200, json=CANNED)

    await _provider(handler).run_turn(_request())

    assert captured["url"] == "https://opencode.ai/zen/go/v1/messages"
    # Only the minimal Anthropic headers; no SDK/beta headers leak through.
    assert captured["headers"]["x-api-key"] == "sk-zen"
    assert captured["headers"]["anthropic-version"] == "2023-06-01"
    assert "anthropic-beta" not in captured["headers"]

    body = captured["json"]
    assert body["model"] == "minimax-m3"
    assert body["system"] == "You are Kimi."
    assert body["max_tokens"] == 256
    assert body["tools"][0]["name"] == "get_weather"
    assert body["messages"][-1]["role"] == "user"
    # No temperature is sent in v1 (mirrors the other Anthropic-style providers).
    assert "temperature" not in body


@pytest.mark.asyncio
async def test_codex_tool_call_is_rebuilt_before_anthropic_tool_result() -> None:
    request = ProviderRequest(
        conversation_id=1,
        system_prompt="You are Kimi.",
        messages=[
            ConversationMessage(
                role="assistant",
                content=[ContentPart.from_text("I’ll look that up.")],
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        name="lookup",
                        arguments={"query": "Quest 3"},
                    )
                ],
                raw_provider_data={
                    "type": "response_output",
                    "output": [
                        {
                            "type": "function_call",
                            "call_id": "call_1",
                            "name": "lookup",
                            "arguments": '{"query":"Quest 3"}',
                        }
                    ],
                },
            ),
            ConversationMessage(
                role="tool",
                content=[ContentPart.from_text('{"answer":"Quest 3"}')],
                tool_call_id="call_1",
                tool_name="lookup",
            ),
        ],
        current_user_parts=[],
        tools=[],
        max_tokens=256,
    )
    captured: dict[str, Any] = {}

    await _provider(_capture_body(captured)).run_turn(request)

    assert captured["json"]["messages"] == [
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "I’ll look that up."},
                {
                    "type": "tool_use",
                    "id": "call_1",
                    "name": "lookup",
                    "input": {"query": "Quest 3"},
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "call_1",
                    "content": '{"answer":"Quest 3"}',
                }
            ],
        },
    ]


@pytest.mark.asyncio
async def test_non_200_raises_with_status_and_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"type": "error", "error": {"message": "bad model id"}})

    with pytest.raises(httpx.HTTPStatusError) as exc:
        await _provider(handler).run_turn(_request())

    assert "400" in str(exc.value)
    assert "bad model id" in str(exc.value)


def _capture_body(captured: dict[str, Any]):
    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content)
        return httpx.Response(200, json=CANNED)

    return handler


def _cache_marked_blocks(body: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        block
        for message in body["messages"]
        for block in message["content"]
        if isinstance(block, dict) and "cache_control" in block
    ]


@pytest.mark.asyncio
async def test_prompt_caching_marks_only_the_final_block() -> None:
    captured: dict[str, Any] = {}

    await _provider(_capture_body(captured), prompt_caching=True).run_turn(_request())

    body = captured["json"]
    # One rolling breakpoint: the cached prefix is everything before it.
    assert len(_cache_marked_blocks(body)) == 1
    assert body["messages"][-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    # The breakpoint rides the message list: a `system` breakpoint is ignored by
    # ccflare's claude-code route (verified live), so system stays a plain string.
    assert body["system"] == "You are Kimi."


@pytest.mark.asyncio
async def test_prompt_caching_disabled_sends_no_cache_control() -> None:
    captured: dict[str, Any] = {}

    await _provider(_capture_body(captured), prompt_caching=False).run_turn(_request())

    assert _cache_marked_blocks(captured["json"]) == []


@pytest.mark.asyncio
async def test_prompt_caching_does_not_mutate_stored_raw_provider_data() -> None:
    raw = {
        "role": "assistant",
        "content": [{"type": "text", "text": "earlier reply"}],
    }
    message = ConversationMessage(role="assistant", content=[], raw_provider_data=raw)
    request = ProviderRequest(
        conversation_id=1,
        system_prompt="You are Kimi.",
        messages=[message],
        current_user_parts=[],
        tools=[],
        max_tokens=256,
    )
    captured: dict[str, Any] = {}

    await _provider(_capture_body(captured), prompt_caching=True).run_turn(request)

    assert len(_cache_marked_blocks(captured["json"])) == 1
    # A breakpoint written back into the stored message would ride along in every
    # later turn, eventually blowing Anthropic's 4-breakpoint limit.
    assert raw["content"] == [{"type": "text", "text": "earlier reply"}]


@pytest.mark.asyncio
async def test_prompt_caching_skips_messages_with_no_markable_block() -> None:
    request = ProviderRequest(
        conversation_id=1,
        system_prompt="You are Kimi.",
        messages=[
            ConversationMessage(role="user", content=[ContentPart.from_text("hi")]),
            # Contributes an empty content list, nothing to hang a breakpoint on.
            ConversationMessage(role="assistant", content=[]),
        ],
        current_user_parts=[],
        tools=[],
        max_tokens=256,
    )
    captured: dict[str, Any] = {}

    await _provider(_capture_body(captured), prompt_caching=True).run_turn(request)

    body = captured["json"]
    assert len(_cache_marked_blocks(body)) == 1
    assert body["messages"][0]["content"][-1]["cache_control"] == {"type": "ephemeral"}


@pytest.mark.asyncio
async def test_thinking_blocks_survive_into_raw_message() -> None:
    payload = dict(
        CANNED,
        content=[
            {"type": "thinking", "thinking": "weighing options", "signature": "sig-1"},
            {"type": "redacted_thinking", "data": "enc"},
            {"type": "text", "text": "Let me check."},
        ],
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    resp = await _provider(handler).run_turn(_request())

    # ccflare's claude-code route enables extended thinking; a tool-use
    # continuation that drops these blocks can be rejected upstream.
    assert resp.raw_message["content"] == [
        {"type": "thinking", "thinking": "weighing options", "signature": "sig-1"},
        {"type": "redacted_thinking", "data": "enc"},
        {"type": "text", "text": "Let me check."},
    ]
    assert resp.content == "Let me check."


@pytest.mark.asyncio
async def test_cache_breakpoint_skips_trailing_thinking_block() -> None:
    raw = {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "partial answer"},
            {"type": "thinking", "thinking": "still weighing", "signature": "sig-2"},
        ],
    }
    request = ProviderRequest(
        conversation_id=1,
        system_prompt="You are Kimi.",
        messages=[ConversationMessage(role="assistant", content=[], raw_provider_data=raw)],
        current_user_parts=[],
        tools=[],
        max_tokens=256,
    )
    captured: dict[str, Any] = {}

    await _provider(_capture_body(captured), prompt_caching=True).run_turn(request)

    blocks = captured["json"]["messages"][-1]["content"]
    # A thinking block has to go back verbatim, so the breakpoint moves to the
    # last block that is not one.
    assert "cache_control" not in blocks[-1]
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}


@pytest.mark.asyncio
async def test_usage_preserves_cache_fields() -> None:
    payload = dict(CANNED)
    payload["usage"] = {
        "input_tokens": 67,
        "output_tokens": 26,
        "cache_read_input_tokens": 114,
        "cache_creation_input_tokens": 5,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    resp = await _provider(handler).run_turn(_request())

    assert resp.usage["input_tokens"] == 67
    assert resp.usage["output_tokens"] == 26
    assert resp.usage["cache_read_input_tokens"] == 114
    assert resp.usage["cache_creation_input_tokens"] == 5


@pytest.mark.asyncio
async def test_usage_keeps_thinking_token_details() -> None:
    """Preserve output_tokens_details consistently across compat and SDK paths."""
    payload = dict(CANNED)
    payload["usage"] = {
        "input_tokens": 10,
        "output_tokens": 20,
        "output_tokens_details": {"thinking_tokens": 8},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    resp = await _provider(handler).run_turn(_request())

    assert resp.usage["output_tokens_details"] == {"thinking_tokens": 8}


@pytest.mark.asyncio
async def test_absent_token_counts_report_zero_not_none() -> None:
    """Both Anthropic providers must agree; None here billed differently."""
    payload = dict(CANNED)
    payload["usage"] = {"cache_read_input_tokens": 3}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    resp = await _provider(handler).run_turn(_request())

    assert resp.usage["input_tokens"] == 0
    assert resp.usage["output_tokens"] == 0


def _effort_provider(handler, effort: str) -> AnthropicCompatProvider:
    return AnthropicCompatProvider(
        base_url="http://localhost:8080/v1/ccflare/anthropic",
        model="anthropic/claude-opus-5",
        api_key="",
        effort=effort,
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.asyncio
async def test_no_effort_configured_sends_no_output_config() -> None:
    captured: dict[str, Any] = {}

    await _provider(_capture_body(captured)).run_turn(_request())

    # The OpenCode Zen profile sets no effort; its body must stay unchanged.
    assert "output_config" not in captured["json"]


@pytest.mark.asyncio
async def test_profile_effort_is_sent_as_output_config() -> None:
    captured: dict[str, Any] = {}

    await _effort_provider(_capture_body(captured), "low").run_turn(_request())

    assert captured["json"]["output_config"] == {"effort": "low"}


@pytest.mark.asyncio
async def test_turn_escalation_overrides_profile_effort() -> None:
    captured: dict[str, Any] = {}
    request = ProviderRequest(
        conversation_id=1,
        system_prompt="You are Kimi.",
        messages=[],
        current_user_parts=[ContentPart.from_text("read the file")],
        tools=[],
        max_tokens=256,
        reasoning_effort="high",
    )

    await _effort_provider(_capture_body(captured), "low").run_turn(request)

    assert captured["json"]["output_config"] == {"effort": "high"}


@pytest.mark.asyncio
async def test_escalation_outside_anthropic_ladder_falls_back_to_profile() -> None:
    captured: dict[str, Any] = {}
    request = ProviderRequest(
        conversation_id=1,
        system_prompt="You are Kimi.",
        messages=[],
        current_user_parts=[ContentPart.from_text("hi")],
        tools=[],
        max_tokens=256,
        # "ultra" is in the agent's internal ladder but not Anthropic's; sending
        # it would be a deterministic 400 mid-turn that never fails over.
        reasoning_effort="ultra",
    )

    await _effort_provider(_capture_body(captured), "low").run_turn(request)

    assert captured["json"]["output_config"] == {"effort": "low"}
