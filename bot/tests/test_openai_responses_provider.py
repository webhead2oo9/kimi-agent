from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast

import pytest

from providers.errors import (
    ProviderAvailabilityError,
    ProviderError,
    ProviderPolicyError,
    provider_failure_disposition,
)
from providers.failure_policy import CooldownPolicy, FailureCategory, generic_failure_policy
from providers.openai_responses import OpenAIResponsesProvider
from providers.types import (
    ContentPart,
    ConversationMessage,
    ProviderCapability,
    ProviderRequest,
)
from tests.helpers import FakeResponses


def _request(
    *,
    messages: list[ConversationMessage] | None = None,
    current: list[ContentPart] | None = None,
    tools: list[dict[str, Any]] | None = None,
) -> ProviderRequest:
    return ProviderRequest(
        conversation_id=1,
        system_prompt="Be concise.",
        messages=[] if messages is None else messages,
        current_user_parts=([ContentPart.from_text("hello")] if current is None else current),
        tools=[] if tools is None else tools,
        max_tokens=200,
    )


def _reasoning_request(
    *,
    reasoning_enabled: bool = True,
    reasoning_effort: str | None = None,
) -> ProviderRequest:
    base = _request()
    return replace(
        base,
        reasoning_enabled=reasoning_enabled,
        reasoning_effort=reasoning_effort,
    )


def _native_response(**overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "output_text": "done",
        "output": [],
        "status": "completed",
        "incomplete_details": None,
        "usage": None,
        "model": "gpt-5.6-luna",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_responses_provider_is_stateless_and_disables_sdk_retries() -> None:
    provider = OpenAIResponsesProvider(
        api_key="test",
        base_url="https://example.test/v1",
        model="gpt-5.6-luna",
    )

    assert provider._client.max_retries == 0
    assert provider.base_url == "https://example.test/v1"
    assert provider.capabilities == {
        ProviderCapability.TEXT,
        ProviderCapability.IMAGE_INPUT,
        ProviderCapability.TOOL_CALLING,
    }


def test_responses_provider_constructs_for_a_keyless_gateway() -> None:
    # keyless: true profiles resolve api_key to ""; the SDK refuses to
    # construct with an empty key, so the provider substitutes a placeholder
    # (same hardening as the Chat Completions providers).
    provider = OpenAIResponsesProvider(
        api_key="",
        base_url="http://localhost:8080/v1",
        model="gpt-5.6-luna",
    )

    assert provider.base_url == "http://localhost:8080/v1"


def test_responses_provider_sends_a_neutral_user_agent() -> None:
    # Some OpenAI-compatible proxies WAF-block the SDK's default "OpenAI/Python"
    # UA; the provider sends the same neutral UA as the chat providers.
    provider = OpenAIResponsesProvider(
        api_key="test",
        base_url="https://example.test/v1",
        model="gpt-5.6-luna",
    )

    assert provider._client._custom_headers["User-Agent"] == "Kimi"


def test_responses_provider_accepts_the_runtime_bot_name_as_user_agent() -> None:
    provider = OpenAIResponsesProvider(
        api_key="test",
        base_url="https://example.test/v1",
        model="gpt-5.6-luna",
        user_agent="Commúnity Helper 🤖\r\nInjected: value",
    )

    assert provider._client._custom_headers["User-Agent"] == "Community Helper Injected- value"


def test_responses_provider_sends_images_tools_and_store_false() -> None:
    provider = OpenAIResponsesProvider(api_key="test", model="gpt-5.6-luna")
    fake = FakeResponses(_native_response())
    cast(Any, provider)._client.responses = fake
    image = ContentPart.from_image_url(
        url="data:image/png;base64,YWJj",
        media_type="image/png",
    )
    tool = {
        "name": "lookup",
        "description": "Look something up",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    }

    response = asyncio.run(
        provider.run_turn(_request(current=[ContentPart.from_text("inspect"), image], tools=[tool]))
    )

    assert response.content == "done"
    [call] = fake.calls
    assert call["store"] is False
    assert "previous_response_id" not in call
    assert call["input"] == [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": "inspect"},
                {
                    "type": "input_image",
                    "image_url": "data:image/png;base64,YWJj",
                    "detail": "auto",
                },
            ],
        }
    ]
    assert call["tools"] == [
        {
            "type": "function",
            "name": "lookup",
            "description": "Look something up",
            "parameters": tool["parameters"],
        }
    ]


def test_responses_provider_parses_and_replays_function_calls() -> None:
    function_call = SimpleNamespace(
        type="function_call",
        id="item-1",
        call_id="call-1",
        name="lookup",
        arguments='{"query":"kimi"}',
    )
    provider = OpenAIResponsesProvider(api_key="test", model="gpt-5.6-luna")
    fake = FakeResponses(
        _native_response(output_text="", output=[function_call], status="completed")
    )
    cast(Any, provider)._client.responses = fake

    response = asyncio.run(provider.run_turn(_request()))

    assert response.tool_calls[0].id == "call-1"
    assert response.tool_calls[0].name == "lookup"
    assert response.tool_calls[0].arguments == {"query": "kimi"}
    assert response.raw_message == {
        "type": "response_output",
        "output": [
            {
                "type": "function_call",
                "id": "item-1",
                "call_id": "call-1",
                "name": "lookup",
                "arguments": '{"query":"kimi"}',
            }
        ],
    }

    replay = provider._build_input(
        _request(
            messages=[
                ConversationMessage(
                    role="assistant",
                    raw_provider_data=response.raw_message,
                ),
                ConversationMessage(
                    role="tool",
                    tool_call_id="call-1",
                    content=[ContentPart.from_text("found")],
                ),
            ],
            current=[],
        )
    )
    assert replay[-1] == {
        "type": "function_call_output",
        "call_id": "call-1",
        "output": "found",
    }


def test_responses_provider_normalizes_usage_and_length_finish() -> None:
    truncated_call = SimpleNamespace(
        type="function_call",
        id="item-1",
        call_id="call-1",
        name="delete_file",
        arguments='{"path":"important.txt"}',
    )
    provider = OpenAIResponsesProvider(api_key="test", model="gpt-5.6-luna")
    fake = FakeResponses(
        _native_response(
            status="incomplete",
            incomplete_details=SimpleNamespace(reason="max_output_tokens"),
            output_text="partial answer",
            output=[truncated_call],
            usage=SimpleNamespace(
                input_tokens=20,
                output_tokens=5,
                total_tokens=25,
                input_tokens_details=SimpleNamespace(cached_tokens=7),
            ),
        )
    )
    cast(Any, provider)._client.responses = fake

    response = asyncio.run(provider.run_turn(_request()))

    assert response.finish_reason == "length"
    assert response.content == "partial answer"
    assert response.has_tool_calls is False
    assert response.raw_message["output"] == []
    assert response.usage == {
        "input_tokens": 20,
        "output_tokens": 5,
        "total_tokens": 25,
        "input_tokens_details": {"cached_tokens": 7},
    }
    assert response.model == "gpt-5.6-luna"


def test_responses_provider_failed_status_raises_classifiable_error() -> None:
    provider = OpenAIResponsesProvider(api_key="test", model="gpt-5.6-luna")
    fake = FakeResponses(
        _native_response(
            status="failed",
            output_text="partial output must not escape",
            error=SimpleNamespace(code="invalid_request"),
        )
    )
    cast(Any, provider)._client.responses = fake

    with pytest.raises(ProviderError, match="failed response") as excinfo:
        asyncio.run(provider.run_turn(_request()))

    assert provider_failure_disposition(excinfo.value) == "stop"


@pytest.mark.parametrize(
    ("retry_after", "disposition", "retry_at"),
    ((None, "retry", 1045), (2.5, "failover", 1002.5)),
)
def test_responses_provider_rate_limit_failure_uses_rate_limit_policy(
    retry_after: float | None,
    disposition: str,
    retry_at: float,
) -> None:
    provider = OpenAIResponsesProvider(api_key="test", model="gpt-5.6-luna")
    fake = FakeResponses(
        _native_response(
            status="failed",
            error=SimpleNamespace(
                code="rate_limit_exceeded",
                retry_after_seconds=retry_after,
            ),
        )
    )
    cast(Any, provider)._client.responses = fake

    with pytest.raises(RuntimeError, match="failed response") as excinfo:
        asyncio.run(provider.run_turn(_request()))

    failure = generic_failure_policy(
        excinfo.value,
        CooldownPolicy(rate_limit_seconds=45),
        1000,
    )
    assert failure.disposition == disposition
    assert failure.category is FailureCategory.RATE_LIMIT
    assert failure.status_code == 429
    assert failure.provider_code == "rate_limit_exceeded"
    assert failure.retry_at == retry_at


def test_responses_provider_server_failure_is_retryable() -> None:
    provider = OpenAIResponsesProvider(api_key="test", model="gpt-5.6-luna")
    fake = FakeResponses(
        _native_response(
            status="failed",
            error=SimpleNamespace(code="server_error"),
        )
    )
    cast(Any, provider)._client.responses = fake

    with pytest.raises(ProviderAvailabilityError) as excinfo:
        asyncio.run(provider.run_turn(_request()))

    assert provider_failure_disposition(excinfo.value) == "retry"


def test_responses_provider_content_filter_discards_partial_output() -> None:
    provider = OpenAIResponsesProvider(api_key="test", model="gpt-5.6-luna")
    fake = FakeResponses(
        _native_response(
            status="incomplete",
            output_text="partial output must not escape",
            incomplete_details=SimpleNamespace(reason="content_filter"),
        )
    )
    cast(Any, provider)._client.responses = fake

    with pytest.raises(ProviderPolicyError):
        asyncio.run(provider.run_turn(_request()))


def test_responses_provider_close_closes_client() -> None:
    provider = OpenAIResponsesProvider(api_key="test", model="gpt-5.6-luna")
    closed = 0

    async def close() -> None:
        nonlocal closed
        closed += 1

    cast(Any, provider)._client.close = close
    asyncio.run(provider.close())

    assert closed == 1


def _run_and_capture(provider: OpenAIResponsesProvider, request: ProviderRequest) -> dict[str, Any]:
    fake = FakeResponses(_native_response())
    cast(Any, provider)._client.responses = fake
    asyncio.run(provider.run_turn(request))
    [call] = fake.calls
    return call


def test_responses_provider_omits_reasoning_when_no_effort_is_configured() -> None:
    # Omit reasoning when neither the profile nor the turn selects an effort;
    # compatibility gateways may reject the unknown field.
    provider = OpenAIResponsesProvider(api_key="test", model="gpt-5.6-luna")

    call = _run_and_capture(provider, _reasoning_request())

    assert "reasoning" not in call
    assert "include" not in call


def test_responses_provider_sends_profile_effort_and_asks_for_encrypted_reasoning() -> None:
    # store=false keeps no server-side reasoning state, so continuity across
    # tool-call rounds needs the encrypted items back for replay.
    provider = OpenAIResponsesProvider(
        api_key="test",
        model="gpt-5.6-luna",
        reasoning_effort="medium",
    )

    call = _run_and_capture(provider, _reasoning_request())

    assert call["reasoning"] == {"effort": "medium"}
    assert call["include"] == ["reasoning.encrypted_content"]


def test_responses_provider_lets_tool_escalation_beat_the_profile_baseline() -> None:
    provider = OpenAIResponsesProvider(
        api_key="test",
        model="gpt-5.6-luna",
        reasoning_effort="low",
    )

    call = _run_and_capture(provider, _reasoning_request(reasoning_effort="high"))

    assert call["reasoning"] == {"effort": "high"}


def test_responses_provider_escalates_even_without_a_profile_baseline() -> None:
    provider = OpenAIResponsesProvider(api_key="test", model="gpt-5.6-luna")

    call = _run_and_capture(provider, _reasoning_request(reasoning_effort="high"))

    assert call["reasoning"] == {"effort": "high"}


def test_responses_provider_pins_cheapest_effort_on_reasoning_disabled_turns() -> None:
    # Compaction and finalizer turns disable reasoning; they take the floor
    # rather than the profile baseline (same rule as codex.py).
    provider = OpenAIResponsesProvider(
        api_key="test",
        model="gpt-5.6-luna",
        reasoning_effort="high",
    )

    call = _run_and_capture(provider, _reasoning_request(reasoning_enabled=False))

    assert call["reasoning"] == {"effort": "low"}


def test_responses_provider_stays_silent_on_reasoning_disabled_unconfigured_turns() -> None:
    provider = OpenAIResponsesProvider(api_key="test", model="gpt-5.6-luna")

    call = _run_and_capture(provider, _reasoning_request(reasoning_enabled=False))

    assert "reasoning" not in call


def test_responses_provider_replays_encrypted_reasoning_items() -> None:
    # The reasoning items returned under `include` must survive into the next
    # request's input, or continuity is lost the moment a tool call happens.
    provider = OpenAIResponsesProvider(api_key="test", model="gpt-5.6-luna")
    reasoning_item = SimpleNamespace(
        type="reasoning",
        id="rs_1",
        summary=[],
        encrypted_content="ENCRYPTED",
    )

    replayed = cast(Any, provider)._replayable_output([reasoning_item])

    assert replayed == [
        {
            "type": "reasoning",
            "id": "rs_1",
            "summary": [],
            "encrypted_content": "ENCRYPTED",
        }
    ]
