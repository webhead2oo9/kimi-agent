import json
from typing import Any

import pytest

from providers.codex import CodexProvider
from providers.errors import ProviderPolicyError
from providers.types import (
    ContentPart,
    ConversationMessage,
    ProviderCapability,
    ProviderRequest,
    ToolCall,
)


class FakeTransport:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []
        self.close_count = 0

    async def send_request(
        self,
        session_key: str,
        payload: dict[str, Any],
        *,
        expected_previous_response_id: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "session_key": session_key,
                "payload": payload,
                "expected_previous_response_id": expected_previous_response_id,
            }
        )
        return self.response

    async def close_all(self) -> None:
        self.close_count += 1


@pytest.mark.asyncio
async def test_codex_provider_sends_full_replay_input_and_delegates_delta_to_transport() -> None:
    response = {
        "id": "resp_2",
        "status": "completed",
        "output_text": "done",
        "output": [],
        "usage": {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5},
        "model": "gpt-5.5-codex-2026",
    }
    transport = FakeTransport(response)
    provider = CodexProvider(transport=transport, model="gpt-5.5")
    raw_output = {
        "type": "response_output",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "old", "annotations": []}],
            }
        ],
    }

    result = await provider.run_turn(
        ProviderRequest(
            conversation_id=42,
            system_prompt="You are helpful.",
            messages=[
                ConversationMessage(
                    role="assistant",
                    raw_provider_data=raw_output,
                )
            ],
            current_user_parts=[ContentPart.from_text("next")],
            tools=[],
            max_tokens=128,
            provider_state={"latest_response_id": "resp_1"},
        )
    )

    call = transport.calls[0]
    payload = call["payload"]
    assert call["session_key"] == "42"
    assert call["expected_previous_response_id"] == "resp_1"
    assert "previous_response_id" not in payload
    assert payload["input"] == [
        raw_output["output"][0],
        {"role": "user", "content": [{"type": "input_text", "text": "next"}]},
    ]
    # The ChatGPT-account Codex backend rejects max_output_tokens ("Unsupported
    # parameter"); the provider must not send it.
    assert "max_output_tokens" not in payload
    assert result.content == "done"
    assert result.model == "gpt-5.5-codex-2026"
    assert result.provider_state == {"latest_response_id": "resp_2"}
    assert result.usage == {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5}


@pytest.mark.asyncio
async def test_codex_provider_maps_incomplete_max_tokens_to_length() -> None:
    truncated_call = {
        "type": "function_call",
        "call_id": "call-1",
        "name": "delete_file",
        "arguments": '{"path":"important.txt"}',
    }
    provider = CodexProvider(
        transport=FakeTransport(
            {
                "id": "resp_1",
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
                "output_text": "partial answer",
                "output": [truncated_call],
            }
        ),
        model="gpt-5.5",
    )

    result = await provider.run_turn(
        ProviderRequest(
            conversation_id=1,
            system_prompt="",
            messages=[],
            current_user_parts=[ContentPart.from_text("answer")],
            tools=[],
            max_tokens=64,
        )
    )

    assert result.content == "partial answer"
    assert result.finish_reason == "length"
    assert result.has_tool_calls is False
    assert result.raw_message["output"] == []


@pytest.mark.asyncio
async def test_codex_provider_maps_incomplete_content_filter_to_policy_error() -> None:
    provider = CodexProvider(
        transport=FakeTransport(
            {
                "id": "resp_1",
                "status": "incomplete",
                "incomplete_details": {"reason": "content_filter"},
                "output": [],
            }
        ),
        model="gpt-5.5",
    )

    with pytest.raises(ProviderPolicyError):
        await provider.run_turn(
            ProviderRequest(
                conversation_id=1,
                system_prompt="",
                messages=[],
                current_user_parts=[ContentPart.from_text("answer")],
                tools=[],
                max_tokens=64,
            )
        )


@pytest.mark.asyncio
async def test_codex_provider_builds_tools_image_and_low_reasoning_for_helper_turn() -> None:
    transport = FakeTransport(
        {"id": "resp_1", "status": "completed", "output_text": "ok", "output": []}
    )
    provider = CodexProvider(
        transport=transport,
        model="gpt-5.5",
        reasoning_effort="high",
        image_quality="auto",
        image_format="png",
    )

    await provider.run_turn(
        ProviderRequest(
            conversation_id=1,
            system_prompt="",
            messages=[],
            current_user_parts=[ContentPart.from_text("draw a moon base")],
            tools=[{"name": "lookup", "description": "Search", "parameters": {}}],
            max_tokens=64,
            requested_capabilities={ProviderCapability.IMAGE_OUTPUT},
            reasoning_enabled=False,
        )
    )

    payload = transport.calls[0]["payload"]
    assert payload["reasoning"] == {"effort": "low"}
    assert payload["tools"] == [
        {
            "type": "function",
            "name": "lookup",
            "description": "Search",
            "parameters": {},
        },
        {"type": "image_generation", "output_format": "png", "quality": "auto"},
    ]
    assert payload["tool_choice"] == "auto"
    assert payload["include"] == ["reasoning.encrypted_content"]
    assert payload["store"] is False
    assert payload["stream"] is True


@pytest.mark.asyncio
async def test_codex_provider_honors_per_request_reasoning_effort() -> None:
    transport = FakeTransport(
        {"id": "resp_1", "status": "completed", "output_text": "ok", "output": []}
    )
    provider = CodexProvider(
        transport=transport,
        model="gpt-5.6-sol",
        reasoning_effort="low",
    )

    await provider.run_turn(
        ProviderRequest(
            conversation_id=1,
            system_prompt="",
            messages=[],
            current_user_parts=[ContentPart.from_text("finish the coding task")],
            tools=[],
            max_tokens=64,
            reasoning_effort="high",
        )
    )

    assert transport.calls[0]["payload"]["reasoning"] == {"effort": "high"}


@pytest.mark.asyncio
async def test_codex_provider_parses_tools_images_and_raw_replay() -> None:
    transport = FakeTransport(
        {
            "id": "resp_1",
            "status": "completed",
            "output_text": "made one",
            "output": [
                {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "lookup",
                    "arguments": json.dumps({"q": "vr"}),
                },
                {
                    "type": "image_generation_call",
                    "id": "img_1",
                    "status": "completed",
                    "result": "iVBORw0K",
                    "revised_prompt": "moon base",
                },
            ],
        }
    )
    provider = CodexProvider(transport=transport, model="gpt-5.5", image_format="webp")

    response = await provider.run_turn(
        ProviderRequest(
            conversation_id=1,
            system_prompt="",
            messages=[],
            current_user_parts=[ContentPart.from_text("make image")],
            tools=[],
            max_tokens=64,
        )
    )

    assert response.tool_calls == [ToolCall(id="call_1", name="lookup", arguments={"q": "vr"})]
    assert response.generated_assets[0].data_base64 == "iVBORw0K"
    assert response.generated_assets[0].media_type == "image/webp"
    assert response.generated_assets[0].suggested_filename == "codex-response-2.webp"
    assert response.raw_message["output"][1] == {
        "type": "image_generation_call",
        "id": "img_1",
        "status": "completed",
        "result": "iVBORw0K",
        "revised_prompt": "moon base",
    }


@pytest.mark.asyncio
async def test_codex_provider_strips_generated_image_bytes_from_replayed_input() -> None:
    transport = FakeTransport(
        {"id": "resp_2", "status": "completed", "output_text": "ok", "output": []}
    )
    provider = CodexProvider(transport=transport, model="gpt-5.5")
    raw_output = {
        "type": "response_output",
        "output": [
            {
                "type": "image_generation_call",
                "id": "img_1",
                "status": "completed",
                "result": "BIGBASE64DATA" * 1000,
                "revised_prompt": "moon base",
            }
        ],
    }

    await provider.run_turn(
        ProviderRequest(
            conversation_id=7,
            system_prompt="",
            messages=[ConversationMessage(role="assistant", raw_provider_data=raw_output)],
            current_user_parts=[ContentPart.from_text("again")],
            tools=[],
            max_tokens=64,
        )
    )

    replayed = transport.calls[0]["payload"]["input"]
    image_items = [item for item in replayed if item.get("type") == "image_generation_call"]
    assert len(image_items) == 1
    # The multi-megabyte base64 payload must not be re-sent as input on every turn.
    assert "result" not in image_items[0]
    # Metadata is preserved so the model keeps context that an image was produced.
    assert image_items[0]["id"] == "img_1"
    assert image_items[0]["revised_prompt"] == "moon base"


@pytest.mark.asyncio
async def test_codex_provider_serializes_plain_assistant_history_as_output_text() -> None:
    transport = FakeTransport(
        {"id": "resp_2", "status": "completed", "output_text": "ok", "output": []}
    )
    provider = CodexProvider(transport=transport, model="gpt-5.5")

    await provider.run_turn(
        ProviderRequest(
            conversation_id=7,
            system_prompt="",
            messages=[
                ConversationMessage(
                    role="user",
                    content=[
                        ContentPart.from_text("Alice: describe this"),
                        ContentPart.from_image_url(
                            url="data:image/png;base64,abc",
                            media_type="image/png",
                        ),
                    ],
                ),
                ConversationMessage(
                    role="assistant",
                    content=[ContentPart.from_text("Looks like concrete bags.")],
                ),
            ],
            current_user_parts=[ContentPart.from_text("how many tons?")],
            tools=[],
            max_tokens=64,
        )
    )

    replayed = transport.calls[0]["payload"]["input"]
    assert replayed[1] == {
        "role": "assistant",
        "content": [
            {"type": "output_text", "text": "Looks like concrete bags."},
        ],
    }
    assert replayed[2] == {
        "role": "user",
        "content": [{"type": "input_text", "text": "how many tons?"}],
    }


@pytest.mark.asyncio
async def test_codex_provider_rebuilds_chat_tool_call_before_tool_output() -> None:
    transport = FakeTransport(
        {"id": "resp_2", "status": "completed", "output_text": "ok", "output": []}
    )
    provider = CodexProvider(transport=transport, model="gpt-5.5")

    await provider.run_turn(
        ProviderRequest(
            conversation_id=7,
            system_prompt="",
            messages=[
                ConversationMessage(
                    role="assistant",
                    tool_calls=[ToolCall(id="call_1", name="lookup", arguments={"q": "vr"})],
                    raw_provider_data={
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [],
                    },
                ),
                ConversationMessage(
                    role="tool",
                    tool_call_id="call_1",
                    content=[ContentPart.from_text("found")],
                ),
            ],
            current_user_parts=[],
            tools=[],
            max_tokens=64,
        )
    )

    assert transport.calls[0]["payload"]["input"] == [
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "lookup",
            "arguments": '{"q": "vr"}',
        },
        {"type": "function_call_output", "call_id": "call_1", "output": "found"},
    ]


@pytest.mark.asyncio
async def test_codex_provider_close_delegates_to_transport() -> None:
    transport = FakeTransport(
        {"id": "resp_1", "status": "completed", "output_text": "ok", "output": []}
    )
    provider = CodexProvider(transport=transport, model="gpt-5.5")

    await provider.close()

    assert transport.close_count == 1


def test_codex_advertises_transport_continuity_not_server_side_context() -> None:
    """Codex replays the full request every turn and uses previous_response_id
    only for transport continuity, so it must NOT claim SERVER_SIDE_CONTEXT:
    agent/core.py drops provider state after client-side compaction for any
    provider that does, which would needlessly break Codex's continuity."""
    codex = CodexProvider(transport=FakeTransport({}), model="gpt-5.5")

    assert ProviderCapability.PREVIOUS_RESPONSE_ID in codex.capabilities
    assert ProviderCapability.SERVER_SIDE_CONTEXT not in codex.capabilities
