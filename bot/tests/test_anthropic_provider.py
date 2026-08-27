import asyncio
from types import SimpleNamespace
from typing import Any, cast

from providers.anthropic import AnthropicProvider
from providers.types import ContentPart, ConversationMessage, ProviderRequest, ToolCall


class FakeMessages:
    def __init__(self, response: SimpleNamespace) -> None:
        self.calls: list[dict[str, Any]] = []
        self._response = response

    async def create(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        return self._response


def test_anthropic_provider_disables_sdk_retries() -> None:
    provider = AnthropicProvider(api_key="test", model="claude-sonnet-4-20250514")

    assert provider._client.max_retries == 0


def test_anthropic_provider_sends_system_separately_and_images_as_blocks() -> None:
    native = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="done")],
        stop_reason="end_turn",
        usage=SimpleNamespace(input_tokens=2, output_tokens=3),
        model="claude-sonnet-4-20250514-v2",
    )
    provider = AnthropicProvider(api_key="test", model="claude-sonnet-4-20250514")
    fake = FakeMessages(native)
    provider._client = cast(Any, SimpleNamespace(messages=fake))

    response = asyncio.run(
        provider.run_turn(
            ProviderRequest(
                conversation_id=1,
                system_prompt="You are helpful.",
                messages=[
                    ConversationMessage(
                        role="assistant",
                        content=[ContentPart.from_text("hello")],
                    )
                ],
                current_user_parts=[
                    ContentPart.from_image_url(
                        url="data:image/png;base64,abc",
                        media_type="image/png",
                    ),
                    ContentPart.from_text("describe"),
                ],
                tools=[],
                max_tokens=128,
            )
        )
    )

    request = fake.calls[0]
    assert request["system"] == "You are helpful."
    assert request["messages"][1]["content"][0]["type"] == "image"
    assert response.content == "done"
    assert response.usage == {"input_tokens": 2, "output_tokens": 3}
    assert response.has_reported_usage is True
    assert response.model == "claude-sonnet-4-20250514-v2"


def test_anthropic_provider_extracts_tool_use_blocks() -> None:
    native = SimpleNamespace(
        content=[
            SimpleNamespace(
                type="tool_use",
                id="toolu_1",
                name="lookup",
                input={"q": "vr"},
            )
        ],
        stop_reason="tool_use",
        usage=None,
    )
    provider = AnthropicProvider(api_key="test", model="claude-sonnet-4-20250514")
    fake = FakeMessages(native)
    provider._client = cast(Any, SimpleNamespace(messages=fake))

    response = asyncio.run(
        provider.run_turn(
            ProviderRequest(
                conversation_id=1,
                system_prompt="",
                messages=[],
                current_user_parts=[ContentPart.from_text("search")],
                tools=[{"name": "lookup", "description": "Search", "parameters": {}}],
                max_tokens=128,
            )
        )
    )

    assert fake.calls[0]["tools"][0]["input_schema"] == {}
    assert response.tool_calls[0].id == "toolu_1"
    assert response.tool_calls[0].arguments == {"q": "vr"}
    assert response.has_reported_usage is False


def test_anthropic_provider_preserves_cache_usage_fields() -> None:
    native = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="done")],
        stop_reason="end_turn",
        usage=SimpleNamespace(
            input_tokens=12,
            output_tokens=3,
            cache_read_input_tokens=7,
            cache_creation_input_tokens=2,
        ),
    )
    provider = AnthropicProvider(api_key="test", model="claude-sonnet-4-20250514")
    fake = FakeMessages(native)
    provider._client = cast(Any, SimpleNamespace(messages=fake))

    response = asyncio.run(
        provider.run_turn(
            ProviderRequest(
                conversation_id=1,
                system_prompt="",
                messages=[],
                current_user_parts=[ContentPart.from_text("hi")],
                tools=[],
                max_tokens=128,
            )
        )
    )

    assert response.usage == {
        "input_tokens": 12,
        "output_tokens": 3,
        "cache_read_input_tokens": 7,
        "cache_creation_input_tokens": 2,
    }


def test_anthropic_provider_sends_tool_results_as_user_blocks() -> None:
    native = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="done")],
        stop_reason="end_turn",
        usage=None,
    )
    provider = AnthropicProvider(api_key="test", model="claude-sonnet-4-20250514")
    fake = FakeMessages(native)
    provider._client = cast(Any, SimpleNamespace(messages=fake))

    asyncio.run(
        provider.run_turn(
            ProviderRequest(
                conversation_id=1,
                system_prompt="",
                messages=[
                    ConversationMessage(
                        role="tool",
                        content=[ContentPart.from_text('{"value": "vr"}')],
                        tool_call_id="toolu_1",
                        tool_name="lookup",
                    )
                ],
                current_user_parts=[],
                tools=[],
                max_tokens=128,
            )
        )
    )

    assert fake.calls[0]["messages"][0] == {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "toolu_1",
                "content": '{"value": "vr"}',
            }
        ],
    }


def test_anthropic_provider_replays_native_thinking_blocks_verbatim() -> None:
    native = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="done")],
        stop_reason="end_turn",
        usage=None,
    )
    provider = AnthropicProvider(api_key="test", model="claude-sonnet-4-20250514")
    fake = FakeMessages(native)
    provider._client = cast(Any, SimpleNamespace(messages=fake))
    raw = {
        "role": "assistant",
        "content": [
            {
                "type": "thinking",
                "thinking": "checking the source",
                "signature": "signed-thinking",
            },
            {"type": "text", "text": "I’ll check."},
            {
                "type": "tool_use",
                "id": "toolu_1",
                "name": "lookup",
                "input": {"q": "vr"},
            },
        ],
    }

    asyncio.run(
        provider.run_turn(
            ProviderRequest(
                conversation_id=1,
                system_prompt="",
                messages=[
                    ConversationMessage(
                        role="assistant",
                        content=[ContentPart.from_text("I’ll check.")],
                        tool_calls=[
                            ToolCall(
                                id="toolu_1",
                                name="lookup",
                                arguments={"q": "vr"},
                            )
                        ],
                        raw_provider_data=raw,
                    ),
                    ConversationMessage(
                        role="tool",
                        content=[ContentPart.from_text("found")],
                        tool_call_id="toolu_1",
                        tool_name="lookup",
                    ),
                ],
                current_user_parts=[],
                tools=[],
                max_tokens=128,
            )
        )
    )

    assert fake.calls[0]["messages"][0] == raw


def test_anthropic_provider_close_closes_client() -> None:
    provider = AnthropicProvider(api_key="test", model="claude-sonnet-4-20250514")
    closed = {"n": 0}

    async def fake_close() -> None:
        closed["n"] += 1

    provider._client = cast(Any, SimpleNamespace(close=fake_close))
    asyncio.run(provider.close())
    assert closed["n"] == 1
