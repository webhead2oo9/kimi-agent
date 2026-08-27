import asyncio
from types import SimpleNamespace
from typing import Any, cast

from providers.openrouter import OpenRouterProvider
from providers.types import ContentPart, ProviderCapability, ProviderRequest


class FakeCompletions:
    def __init__(self, response: SimpleNamespace) -> None:
        self.calls: list[dict[str, Any]] = []
        self._response = response

    async def create(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        return self._response


def test_openrouter_provider_sends_routing_headers_and_modalities() -> None:
    message = SimpleNamespace(content="done", tool_calls=None, images=None)
    native = SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=2, total_tokens=3),
        model="openai/gpt-4.1",
        openrouter_metadata={"provider_name": "OpenAI"},
    )
    provider = OpenRouterProvider(
        api_key="test",
        model="openai/gpt-4.1",
        provider_routing={"require_parameters": True, "data_collection": "deny"},
        app_url="https://example.com",
        app_name="Kímí 🤖\r\nInjected: value",
    )
    completions = FakeCompletions(native)
    provider._client = cast(Any, SimpleNamespace(chat=SimpleNamespace(completions=completions)))

    response = asyncio.run(
        provider.run_turn(
            ProviderRequest(
                conversation_id=1,
                system_prompt="",
                messages=[],
                current_user_parts=[ContentPart.from_text("hi")],
                tools=[],
                max_tokens=128,
                requested_capabilities={ProviderCapability.IMAGE_OUTPUT},
            )
        )
    )

    request = completions.calls[0]
    assert request["extra_headers"]["HTTP-Referer"] == "https://example.com"
    assert request["extra_headers"]["X-Title"] == "Kimi Injected- value"
    assert request["extra_body"]["provider"] == {
        "require_parameters": True,
        "data_collection": "deny",
    }
    assert request["modalities"] == ["image", "text"]
    assert response.content == "done"
    assert response.provider_state["openrouter_metadata"]["provider_name"] == "OpenAI"
    assert response.model == "openai/gpt-4.1"


def test_openrouter_provider_does_not_send_openai_reasoning_effort() -> None:
    message = SimpleNamespace(content="done", tool_calls=None, images=None)
    native = SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=2, total_tokens=3),
        model="x/y",
        openrouter_metadata={},
    )
    provider = OpenRouterProvider(api_key="test", model="x/y")
    completions = FakeCompletions(native)
    provider._client = cast(Any, SimpleNamespace(chat=SimpleNamespace(completions=completions)))

    asyncio.run(
        provider.run_turn(
            ProviderRequest(
                conversation_id=1,
                system_prompt="",
                messages=[],
                current_user_parts=[ContentPart.from_text("hi")],
                tools=[],
                max_tokens=128,
                reasoning_effort="high",
            )
        )
    )

    assert "reasoning_effort" not in completions.calls[0]


def test_openrouter_provider_captures_reasoning_field() -> None:
    # OpenRouter returns chain-of-thought in `message.reasoning`, not the
    # `reasoning_content` field the base OpenAI-chat provider looks for.
    message = SimpleNamespace(
        content="answer", tool_calls=None, images=None, reasoning="step-by-step thinking"
    )
    native = SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=2, total_tokens=3),
        model="x/y",
        openrouter_metadata={},
    )
    provider = OpenRouterProvider(api_key="test", model="x/y")
    completions = FakeCompletions(native)
    provider._client = cast(Any, SimpleNamespace(chat=SimpleNamespace(completions=completions)))

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

    assert response.reasoning_content == "step-by-step thinking"


def test_openrouter_provider_extracts_generated_images() -> None:
    image = SimpleNamespace(image_url=SimpleNamespace(url="data:image/png;base64,abc"))
    message = SimpleNamespace(content="made one", tool_calls=None, images=[image])
    native = SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        usage=None,
        openrouter_metadata={},
    )
    provider = OpenRouterProvider(api_key="test", model="google/gemini-image")
    completions = FakeCompletions(native)
    provider._client = cast(Any, SimpleNamespace(chat=SimpleNamespace(completions=completions)))

    response = asyncio.run(
        provider.run_turn(
            ProviderRequest(
                conversation_id=1,
                system_prompt="",
                messages=[],
                current_user_parts=[ContentPart.from_text("make image")],
                tools=[],
                max_tokens=128,
            )
        )
    )

    assert response.generated_assets[0].data_base64 == "abc"
