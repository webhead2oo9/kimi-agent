import asyncio
import uuid
from types import SimpleNamespace
from typing import Any, cast

import httpx2
import pytest
from openai import BadRequestError

from providers.errors import ProviderAvailabilityError, ProviderPolicyError
from providers.failure_policy import CooldownPolicy, generic_failure_policy
from providers.openai_chat import OpenAIChatProvider
from providers.openai_compat import OpenAICompatProvider
from providers.types import (
    ContentPart,
    ConversationMessage,
    ProviderCapability,
    ProviderRequest,
    ToolCall,
)


class FakeCompletions:
    def __init__(self, message: SimpleNamespace, usage: SimpleNamespace | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._message = message
        self._usage = usage or SimpleNamespace(
            prompt_tokens=3,
            completion_tokens=5,
            total_tokens=8,
        )

    async def create(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=self._message,
                    finish_reason="tool_calls" if self._message.tool_calls else "stop",
                )
            ],
            usage=self._usage,
            model="gpt-4.1-2026-01-01",
        )


def test_chat_provider_sends_text_image_and_tools() -> None:
    message = SimpleNamespace(
        content="",
        reasoning_content="I should call lookup.",
        tool_calls=[
            SimpleNamespace(
                id="call_1",
                function=SimpleNamespace(name="lookup", arguments='{"q": "vr"}'),
            )
        ],
    )
    provider = OpenAIChatProvider(
        api_key="test",
        base_url="https://api.openai.com/v1",
        model="gpt-4.1",
        provider_key="openai_compat",
    )
    completions = FakeCompletions(message)
    provider._client = cast(Any, SimpleNamespace(chat=SimpleNamespace(completions=completions)))

    response = asyncio.run(
        provider.run_turn(
            ProviderRequest(
                conversation_id=1,
                system_prompt="You are helpful.",
                messages=[],
                current_user_parts=[
                    ContentPart.from_text("describe"),
                    ContentPart.from_image_url(
                        url="data:image/png;base64,abc",
                        media_type="image/png",
                    ),
                ],
                tools=[
                    {
                        "name": "lookup",
                        "description": "Search",
                        "parameters": {"type": "object", "properties": {}},
                    }
                ],
                max_tokens=128,
                requested_capabilities={
                    ProviderCapability.TEXT,
                    ProviderCapability.IMAGE_INPUT,
                    ProviderCapability.TOOL_CALLING,
                },
            )
        )
    )

    request = completions.calls[0]
    assert request["messages"][0] == {"role": "system", "content": "You are helpful."}
    assert request["messages"][1]["content"][1]["type"] == "image_url"
    assert request["tools"][0]["type"] == "function"
    assert response.reasoning_content == "I should call lookup."
    assert response.model == "gpt-4.1-2026-01-01"
    assert response.tool_calls == [ToolCall(id="call_1", name="lookup", arguments={"q": "vr"})]


def test_chat_provider_generates_distinct_request_id_headers() -> None:
    message = SimpleNamespace(content="ok", reasoning_content=None, tool_calls=[])
    provider = OpenAIChatProvider(
        api_key="test",
        base_url="https://gateway.example/v1",
        model="deepseek-v4-flash",
        provider_key="openai_compat",
        request_id_header="X-Client-Request-Id",
    )
    completions = FakeCompletions(message)
    provider._client = cast(Any, SimpleNamespace(chat=SimpleNamespace(completions=completions)))
    request = ProviderRequest(
        conversation_id=1,
        system_prompt="",
        messages=[],
        current_user_parts=[ContentPart.from_text("hello")],
        tools=[],
        max_tokens=128,
    )

    asyncio.run(provider.run_turn(request))
    asyncio.run(provider.run_turn(request))

    first = completions.calls[0]["extra_headers"]["X-Client-Request-Id"]
    second = completions.calls[1]["extra_headers"]["X-Client-Request-Id"]
    assert uuid.UUID(first)
    assert uuid.UUID(second)
    assert first != second


def test_chat_provider_sends_configured_reasoning_effort_to_non_deepseek_target() -> None:
    message = SimpleNamespace(content="ok", reasoning_content=None, tool_calls=[])
    provider = OpenAIChatProvider(
        api_key="test",
        base_url="https://api.z.ai/api/coding/paas/v4",
        model="glm-5.3-flash",
        provider_key="openai_compat",
        reasoning_effort="medium",
    )
    completions = FakeCompletions(message)
    provider._client = cast(Any, SimpleNamespace(chat=SimpleNamespace(completions=completions)))

    asyncio.run(
        provider.run_turn(
            ProviderRequest(
                conversation_id=1,
                system_prompt="",
                messages=[],
                current_user_parts=[ContentPart.from_text("hello")],
                tools=[],
                max_tokens=128,
            )
        )
    )

    assert completions.calls[0]["reasoning_effort"] == "medium"
    assert "extra_body" not in completions.calls[0]


def test_chat_provider_non_object_tool_args_fall_back_to_raw() -> None:
    message = SimpleNamespace(
        content="",
        reasoning_content=None,
        tool_calls=[
            SimpleNamespace(
                id="call_1",
                function=SimpleNamespace(name="lookup", arguments="1"),
            )
        ],
    )
    provider = OpenAIChatProvider(
        api_key="test",
        base_url="https://api.openai.com/v1",
        model="gpt-4.1",
        provider_key="openai_compat",
    )
    completions = FakeCompletions(message)
    provider._client = cast(Any, SimpleNamespace(chat=SimpleNamespace(completions=completions)))

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

    assert response.tool_calls == [ToolCall(id="call_1", name="lookup", arguments={"_raw": "1"})]


def test_chat_provider_preserves_prompt_token_details() -> None:
    message = SimpleNamespace(content="done", reasoning_content=None, tool_calls=None)
    usage = SimpleNamespace(
        prompt_tokens=12,
        completion_tokens=5,
        total_tokens=17,
        prompt_tokens_details=SimpleNamespace(cached_tokens=7),
    )
    provider = OpenAIChatProvider(
        api_key="test",
        base_url="https://api.openai.com/v1",
        model="gpt-4.1",
        provider_key="openai_compat",
    )
    completions = FakeCompletions(message, usage=usage)
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

    assert response.usage == {
        "prompt_tokens": 12,
        "completion_tokens": 5,
        "total_tokens": 17,
        "prompt_tokens_details": {"cached_tokens": 7},
    }


def test_chat_provider_sends_flex_only_when_enabled() -> None:
    message = SimpleNamespace(content="done", tool_calls=None)
    provider = OpenAIChatProvider(
        api_key="test",
        base_url="https://api.openai.com/v1",
        model="gpt-5.5",
        provider_key="openai_compat",
        service_tier="flex",
    )
    completions = FakeCompletions(message)
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

    assert completions.calls[0]["service_tier"] == "flex"
    assert response.content == "done"


def test_chat_provider_close_closes_client() -> None:
    provider = OpenAIChatProvider(
        api_key="test",
        base_url="https://api.openai.com/v1",
        model="gpt-4.1",
        provider_key="openai_compat",
    )
    closed = {"n": 0}

    async def fake_close() -> None:
        closed["n"] += 1

    provider._client = cast(Any, SimpleNamespace(close=fake_close))
    asyncio.run(provider.close())
    assert closed["n"] == 1


def _run_simple(provider: OpenAIChatProvider, message: SimpleNamespace) -> Any:
    completions = FakeCompletions(message)
    provider._client = cast(Any, SimpleNamespace(chat=SimpleNamespace(completions=completions)))
    return asyncio.run(
        provider.run_turn(
            ProviderRequest(
                conversation_id=1,
                system_prompt="",
                messages=[],
                current_user_parts=[ContentPart.from_text("q")],
                tools=[],
                max_tokens=64,
            )
        )
    )


def test_chat_provider_captures_kimi_reasoning_field_fallback() -> None:
    # kimi exposes chain-of-thought as `reasoning` (no `reasoning_content`) on the
    # OpenCode Go route; it must be captured, not silently dropped.
    message = SimpleNamespace(
        content="The ball costs $0.05.", reasoning="kimi cot here", tool_calls=None
    )
    provider = OpenAIChatProvider(
        api_key="t",
        base_url="https://opencode.ai/zen/go/v1",
        model="kimi-k2.6",
        provider_key="openai_compat",
    )
    response = _run_simple(provider, message)
    assert response.reasoning_content == "kimi cot here"


def test_chat_provider_prefers_reasoning_content_over_reasoning() -> None:
    message = SimpleNamespace(
        content="x", reasoning_content="primary", reasoning="secondary", tool_calls=None
    )
    provider = OpenAIChatProvider(
        api_key="t", base_url="https://e/v1", model="glm-5.1", provider_key="openai_compat"
    )
    response = _run_simple(provider, message)
    assert response.reasoning_content == "primary"


class FakeStream:
    def __init__(self, chunks: list[SimpleNamespace]) -> None:
        self._chunks = chunks

    async def __aenter__(self) -> FakeStream:
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        return None

    def __aiter__(self) -> Any:
        return self._iter()

    async def _iter(self) -> Any:
        for chunk in self._chunks:
            yield chunk


class FakeStreamingCompletions:
    def __init__(
        self,
        chunks: list[SimpleNamespace],
        stream_error: Exception | None = None,
        message: SimpleNamespace | None = None,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self._chunks = chunks
        self._stream_error = stream_error
        self._message = message

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if kwargs.get("stream"):
            if self._stream_error is not None:
                raise self._stream_error
            return FakeStream(self._chunks)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=self._message, finish_reason="stop")],
            usage=None,
        )


def _delta_chunk(**delta_fields: Any) -> SimpleNamespace:
    return SimpleNamespace(
        usage=None,
        choices=[SimpleNamespace(finish_reason=None, delta=SimpleNamespace(**delta_fields))],
    )


def _streaming_provider(completions: FakeStreamingCompletions) -> OpenAICompatProvider:
    provider = OpenAICompatProvider(
        api_key="t", base_url="https://opencode.ai/zen/go/v1", model="glm-5.2"
    )
    provider._client = cast(Any, SimpleNamespace(chat=SimpleNamespace(completions=completions)))
    return provider


def _run_streaming(provider: OpenAICompatProvider) -> Any:
    return asyncio.run(
        provider.run_turn(
            ProviderRequest(
                conversation_id=1,
                system_prompt="",
                messages=[],
                current_user_parts=[ContentPart.from_text("q")],
                tools=[{"name": "lookup", "description": "Search", "parameters": {}}],
                max_tokens=64,
            )
        )
    )


def test_openai_compat_streams_and_assembles_chunks() -> None:
    chunks = [
        _delta_chunk(content=None, reasoning_content="think ", tool_calls=None),
        _delta_chunk(content=None, reasoning="more", tool_calls=None),
        _delta_chunk(
            content=None,
            tool_calls=[
                SimpleNamespace(
                    index=0,
                    id="call_1",
                    function=SimpleNamespace(name="lookup", arguments='{"q": '),
                )
            ],
        ),
        _delta_chunk(
            content=None,
            tool_calls=[
                SimpleNamespace(
                    index=0,
                    id=None,
                    function=SimpleNamespace(name=None, arguments='"vr"}'),
                )
            ],
        ),
        SimpleNamespace(
            usage=None,
            choices=[SimpleNamespace(finish_reason="tool_calls", delta=None)],
        ),
        SimpleNamespace(
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=4, total_tokens=14),
            choices=[],
        ),
    ]
    completions = FakeStreamingCompletions(chunks)
    provider = _streaming_provider(completions)

    response = _run_streaming(provider)

    assert completions.calls[0]["stream"] is True
    assert completions.calls[0]["stream_options"] == {"include_usage": True}
    assert response.reasoning_content == "think more"
    assert response.tool_calls == [ToolCall(id="call_1", name="lookup", arguments={"q": "vr"})]
    assert response.finish_reason == "tool_calls"
    assert response.usage == {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14}
    assert response.raw_message["tool_calls"][0]["function"]["name"] == "lookup"


def test_openai_compat_streams_text_content() -> None:
    chunks = [
        _delta_chunk(content="Hello ", tool_calls=None),
        _delta_chunk(content="world", tool_calls=None),
        SimpleNamespace(
            usage=None,
            choices=[SimpleNamespace(finish_reason="stop", delta=None)],
        ),
    ]
    provider = _streaming_provider(FakeStreamingCompletions(chunks))

    response = _run_streaming(provider)

    assert response.content == "Hello world"
    assert response.finish_reason == "stop"
    assert response.tool_calls == []


def test_openai_compat_clean_eof_defaults_text_stream_to_stop() -> None:
    provider = _streaming_provider(
        FakeStreamingCompletions(
            [_delta_chunk(content="partial output must not escape", tool_calls=None)]
        )
    )

    response = _run_streaming(provider)

    assert response.content == "partial output must not escape"
    assert response.finish_reason == "stop"


def test_openai_compat_clean_eof_infers_tool_call_finish_reason() -> None:
    provider = _streaming_provider(
        FakeStreamingCompletions(
            [
                _delta_chunk(
                    content=None,
                    tool_calls=[
                        SimpleNamespace(
                            index=0,
                            id="call_1",
                            function=SimpleNamespace(
                                name="lookup",
                                arguments='{"q":"vr"}',
                            ),
                        )
                    ],
                )
            ]
        )
    )

    response = _run_streaming(provider)

    assert response.finish_reason == "tool_calls"
    assert response.tool_calls[0].name == "lookup"


@pytest.mark.parametrize(
    "chunks",
    (
        [],
        [
            _delta_chunk(
                content=None,
                tool_calls=[
                    SimpleNamespace(
                        index=0,
                        id="call_1",
                        function=SimpleNamespace(name="lookup", arguments='{"q":'),
                    )
                ],
            )
        ],
        [
            _delta_chunk(
                content="I will call a tool",
                tool_calls=[
                    SimpleNamespace(
                        index=0,
                        id="call_1",
                        function=SimpleNamespace(name="lookup", arguments='{"q":'),
                    )
                ],
            )
        ],
    ),
)
def test_openai_compat_clean_eof_rejects_empty_or_incomplete_stream(chunks: list[Any]) -> None:
    provider = _streaming_provider(FakeStreamingCompletions(chunks))

    with pytest.raises(ProviderAvailabilityError):
        _run_streaming(provider)


def test_openai_chat_native_content_filter_discards_partial_output() -> None:
    provider = OpenAIChatProvider(
        api_key="test",
        base_url="https://api.openai.com/v1",
        model="gpt-4.1",
        provider_key="openai_compat",
    )
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="content_filter",
                message=SimpleNamespace(
                    content="partial output must not escape",
                    reasoning_content=None,
                    tool_calls=[],
                ),
            )
        ],
        usage=None,
        model="gpt-4.1",
    )

    with pytest.raises(ProviderPolicyError):
        provider._response_from_native(response)


def test_openai_compat_stream_content_filter_discards_partial_output() -> None:
    chunks = [
        _delta_chunk(content="partial output must not escape", tool_calls=None),
        SimpleNamespace(
            usage=None,
            choices=[SimpleNamespace(finish_reason="content_filter", delta=None)],
        ),
    ]
    provider = _streaming_provider(FakeStreamingCompletions(chunks))

    with pytest.raises(ProviderPolicyError):
        _run_streaming(provider)


class HangingStream:
    """Yields `chunks`, then goes silent forever (a stalled backend)."""

    def __init__(self, chunks: list[SimpleNamespace]) -> None:
        self._chunks = chunks

    async def __aenter__(self) -> HangingStream:
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        return None

    def __aiter__(self) -> Any:
        return self._iter()

    async def _iter(self) -> Any:
        for chunk in self._chunks:
            yield chunk
        await asyncio.Event().wait()


def test_openai_compat_aborts_stalled_stream_as_availability_error() -> None:
    class StallCompletions:
        async def create(self, **kwargs: Any) -> Any:
            return HangingStream([_delta_chunk(content="partial ", tool_calls=None)])

    provider = OpenAICompatProvider(
        api_key="t",
        base_url="https://opencode.ai/zen/go/v1",
        model="glm-5.2",
        stall_timeout_seconds=0.05,
    )
    provider._client = cast(
        Any, SimpleNamespace(chat=SimpleNamespace(completions=StallCompletions()))
    )

    async def run() -> BaseException:
        try:
            await provider.run_turn(
                ProviderRequest(
                    conversation_id=1,
                    system_prompt="",
                    messages=[],
                    current_user_parts=[ContentPart.from_text("q")],
                    tools=[],
                    max_tokens=64,
                )
            )
        except BaseException as exc:
            return exc
        raise AssertionError("expected the stalled stream to raise")

    exc = asyncio.run(run())
    assert isinstance(exc, TimeoutError)
    assert generic_failure_policy(exc, CooldownPolicy(), 0).disposition == "retry"


def test_openai_compat_slow_but_moving_stream_outlives_stall_timeout() -> None:
    class SlowStream:
        """Chunk gaps below the stall timeout, total duration well above it."""

        async def __aenter__(self) -> SlowStream:
            return self

        async def __aexit__(self, *exc_info: Any) -> None:
            return None

        def __aiter__(self) -> Any:
            return self._iter()

        async def _iter(self) -> Any:
            for _ in range(10):
                await asyncio.sleep(0.02)
                yield _delta_chunk(content="x", tool_calls=None)
            yield SimpleNamespace(
                usage=None,
                choices=[SimpleNamespace(finish_reason="stop", delta=None)],
            )

    class SlowCompletions:
        async def create(self, **kwargs: Any) -> Any:
            return SlowStream()

    provider = OpenAICompatProvider(
        api_key="t",
        base_url="https://opencode.ai/zen/go/v1",
        model="glm-5.2",
        stall_timeout_seconds=0.05,
    )
    provider._client = cast(
        Any, SimpleNamespace(chat=SimpleNamespace(completions=SlowCompletions()))
    )

    response = _run_streaming(provider)

    # 10 chunks x 0.02s gaps = 0.2s total, 4x the 0.05s stall timeout: the
    # per-chunk re-arm must keep a moving stream alive.
    assert response.content == "x" * 10
    assert response.finish_reason == "stop"


def test_openai_compat_falls_back_when_backend_rejects_streaming() -> None:
    bad_request = BadRequestError(
        "stream not supported",
        response=httpx2.Response(400, request=httpx2.Request("POST", "https://e/v1")),
        body=None,
    )
    message = SimpleNamespace(content="plain", reasoning_content=None, tool_calls=None)
    completions = FakeStreamingCompletions([], stream_error=bad_request, message=message)
    provider = _streaming_provider(completions)

    response = _run_streaming(provider)

    assert response.content == "plain"
    # First call streamed and was rejected; the retry did not stream.
    assert completions.calls[0].get("stream") is True
    assert completions.calls[1].get("stream") is None
    # The downgrade is request-scoped; a shared provider must try streaming for
    # the next user/request rather than mutating fleet-wide behavior.
    _run_streaming(provider)
    assert completions.calls[2].get("stream") is True
    assert completions.calls[3].get("stream") is None


def test_openai_chat_rebuilds_codex_assistant_tool_calls_from_normalized_data() -> None:
    provider = _streaming_provider(FakeStreamingCompletions([]))
    message = ConversationMessage(
        role="assistant",
        content=[ContentPart.from_text("checking")],
        tool_calls=[ToolCall(id="call_1", name="lookup", arguments={"q": "vr"})],
        raw_provider_data={"type": "response_output", "output": []},
    )

    converted = provider._conversation_message_to_chat(message)

    assert converted == {
        "role": "assistant",
        "content": "checking",
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "lookup", "arguments": '{"q": "vr"}'},
            }
        ],
    }
