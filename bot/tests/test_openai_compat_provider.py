import asyncio
from types import SimpleNamespace
from typing import Any, cast

from providers.openai_compat import OpenAICompatProvider
from providers.types import ContentPart, ProviderRequest, ToolCall


class FakeStream:
    """Serves one message as a chunked stream (OpenAICompatProvider streams)."""

    def __init__(self, message: SimpleNamespace, finish_reason: str) -> None:
        delta = SimpleNamespace(
            content=message.content,
            reasoning_content=getattr(message, "reasoning_content", None),
            tool_calls=[
                SimpleNamespace(index=i, id=tc.id, function=tc.function)
                for i, tc in enumerate(message.tool_calls or [])
            ]
            or None,
        )
        self._chunks = [
            SimpleNamespace(
                usage=None,
                choices=[SimpleNamespace(finish_reason=None, delta=delta)],
            ),
            SimpleNamespace(
                usage=None,
                choices=[SimpleNamespace(finish_reason=finish_reason, delta=None)],
            ),
            SimpleNamespace(
                usage=SimpleNamespace(prompt_tokens=3, completion_tokens=5, total_tokens=8),
                choices=[],
            ),
        ]

    async def __aenter__(self) -> FakeStream:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    def __aiter__(self) -> object:
        return self._iter()

    async def _iter(self) -> object:
        for chunk in self._chunks:
            yield chunk


class FakeCompletions:
    def __init__(self, message: SimpleNamespace) -> None:
        self.calls: list[dict] = []
        self._message = message

    async def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        finish_reason = "tool_calls" if self._message.tool_calls else "stop"
        if kwargs.get("stream"):
            return FakeStream(self._message, finish_reason)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=self._message, finish_reason=finish_reason)],
            usage=SimpleNamespace(prompt_tokens=3, completion_tokens=5, total_tokens=8),
        )


def _provider_with_fake_client(
    *,
    base_url: str,
    model: str,
    message: SimpleNamespace,
    reasoning_effort: str = "",
) -> tuple[OpenAICompatProvider, FakeCompletions]:
    provider = OpenAICompatProvider(
        api_key="test",
        base_url=base_url,
        model=model,
        reasoning_effort=reasoning_effort,
    )
    completions = FakeCompletions(message)
    provider._client = cast(Any, SimpleNamespace(chat=SimpleNamespace(completions=completions)))
    return provider, completions


def test_deepseek_chat_enables_thinking_and_preserves_reasoning_content() -> None:
    message = SimpleNamespace(
        content="",
        reasoning_content="I should call the lookup tool.",
        tool_calls=[
            SimpleNamespace(
                id="call_1",
                function=SimpleNamespace(name="lookup", arguments='{"query": "vr"}'),
            )
        ],
    )
    provider, completions = _provider_with_fake_client(
        base_url="https://api.deepseek.com",
        model="test-deepseek-model",
        message=message,
    )

    response = asyncio.run(
        provider.run_turn(
            ProviderRequest(
                conversation_id=1,
                system_prompt="",
                messages=[],
                current_user_parts=[ContentPart.from_text("search")],
                tools=[
                    {
                        "name": "lookup",
                        "description": "Search",
                        "parameters": {"type": "object", "properties": {}},
                    }
                ],
                max_tokens=4096,
            )
        )
    )

    request = completions.calls[0]
    assert request["model"] == "test-deepseek-model"
    assert request["reasoning_effort"] == "high"
    assert request["extra_body"] == {"thinking": {"type": "enabled"}}
    assert response.reasoning_content == "I should call the lookup tool."
    assert response.tool_calls == [ToolCall(id="call_1", name="lookup", arguments={"query": "vr"})]

    assert response.raw_message["content"] == ""
    assert response.raw_message["reasoning_content"] == "I should call the lookup tool."
    assert response.raw_message["tool_calls"][0]["id"] == "call_1"


def test_deepseek_chat_uses_configured_xhigh_reasoning_effort() -> None:
    message = SimpleNamespace(content="done", reasoning_content="thought", tool_calls=None)
    provider, completions = _provider_with_fake_client(
        base_url="https://opencode.ai/zen/go/v1",
        model="deepseek-v4-flash",
        message=message,
        reasoning_effort="xhigh",
    )

    asyncio.run(
        provider.run_turn(
            ProviderRequest(
                conversation_id=1,
                system_prompt="",
                messages=[],
                current_user_parts=[ContentPart.from_text("hi")],
                tools=[],
                max_tokens=1024,
            )
        )
    )

    assert completions.calls[0]["reasoning_effort"] == "xhigh"
    assert completions.calls[0]["extra_body"] == {"thinking": {"type": "enabled"}}


def test_deepseek_chat_can_disable_thinking_for_structured_helper_turns() -> None:
    message = SimpleNamespace(content="[]", tool_calls=None)
    provider, completions = _provider_with_fake_client(
        base_url="https://api.deepseek.com",
        model="test-deepseek-model",
        message=message,
    )

    response = asyncio.run(
        provider.run_turn(
            ProviderRequest(
                conversation_id=1,
                system_prompt="Return JSON only.",
                messages=[],
                current_user_parts=[ContentPart.from_text("extract")],
                tools=[],
                max_tokens=1024,
                reasoning_enabled=False,
            )
        )
    )

    request = completions.calls[0]
    assert "reasoning_effort" not in request
    assert "extra_body" not in request
    assert response.content == "[]"


def test_openai_compat_chat_forwards_temperature_when_set() -> None:
    message = SimpleNamespace(content="done", tool_calls=None)
    provider, completions = _provider_with_fake_client(
        base_url="https://llm-gateway.example.invalid/v1",
        model="Kimi-K2.6",
        message=message,
    )

    asyncio.run(
        provider.run_turn(
            ProviderRequest(
                conversation_id=1,
                system_prompt="",
                messages=[],
                current_user_parts=[ContentPart.from_text("hi")],
                tools=[],
                max_tokens=4096,
                temperature=1.0,
            )
        )
    )

    assert completions.calls[0]["temperature"] == 1.0


def test_openai_compat_chat_omits_temperature_when_none() -> None:
    message = SimpleNamespace(content="done", tool_calls=None)
    provider, completions = _provider_with_fake_client(
        base_url="https://llm-gateway.example.invalid/v1",
        model="Kimi-K2.6",
        message=message,
    )

    asyncio.run(
        provider.run_turn(
            ProviderRequest(
                conversation_id=1,
                system_prompt="",
                messages=[],
                current_user_parts=[ContentPart.from_text("hi")],
                tools=[],
                max_tokens=4096,
            )
        )
    )

    assert "temperature" not in completions.calls[0]


def test_openai_compat_chat_does_not_send_deepseek_thinking_for_other_models() -> None:
    message = SimpleNamespace(content="done", tool_calls=None)
    provider, completions = _provider_with_fake_client(
        base_url="https://api.openai.com/v1",
        model="gpt-4.1",
        message=message,
    )

    response = asyncio.run(
        provider.run_turn(
            ProviderRequest(
                conversation_id=1,
                system_prompt="",
                messages=[],
                current_user_parts=[ContentPart.from_text("hi")],
                tools=[],
                max_tokens=4096,
            )
        )
    )

    request = completions.calls[0]
    assert "reasoning_effort" not in request
    assert "extra_body" not in request
    assert response.content == "done"
    assert response.usage == {
        "prompt_tokens": 3,
        "completion_tokens": 5,
        "total_tokens": 8,
    }
    assert response.raw_message == {
        "role": "assistant",
        "content": "done",
    }


def test_keyless_endpoint_constructs_without_credentials() -> None:
    """A local server or credential-holding gateway passes no key.

    The OpenAI SDK raises "Missing credentials" at construction on an empty
    api_key, so a keyless profile (config/models.yaml `keyless: true`, or an eval
    spec with no `api_key_env`) could not build a client at all. A genuine
    missing credential is caught by the startup gate, not here.
    """
    provider = OpenAICompatProvider(
        api_key="", base_url="http://localhost:1234/v1", model="local/model"
    )

    assert provider.model == "local/model"
