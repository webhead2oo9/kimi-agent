from providers.serializers import (
    content_parts_to_anthropic,
    content_parts_to_openai_chat,
    content_parts_to_openai_responses,
    conversation_message_to_anthropic,
    normalize_anthropic_stop_reason,
    tool_schema_to_anthropic,
    tool_schema_to_openai_chat,
    tool_schema_to_openai_responses,
)
from providers.types import ContentPart, ConversationMessage, ToolCall


def test_normalize_anthropic_stop_reason() -> None:
    # Truncation maps onto the provider-neutral value the agent loop checks for
    # (finish_reason == "length").
    assert normalize_anthropic_stop_reason("max_tokens") == "length"
    # Everything else passes through; missing collapses to end_turn.
    assert normalize_anthropic_stop_reason("end_turn") == "end_turn"
    assert normalize_anthropic_stop_reason("tool_use") == "tool_use"
    assert normalize_anthropic_stop_reason(None) == "end_turn"
    assert normalize_anthropic_stop_reason("") == "end_turn"


def test_content_parts_convert_to_openai_chat_shape() -> None:
    parts = [
        ContentPart.from_text("What is this?"),
        ContentPart.from_image_url(
            url="data:image/png;base64,abc",
            media_type="image/png",
        ),
    ]

    assert content_parts_to_openai_chat(parts) == [
        {"type": "text", "text": "What is this?"},
        {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64,abc", "detail": "auto"},
        },
    ]


def test_single_text_part_converts_to_openai_chat_string() -> None:
    assert content_parts_to_openai_chat([ContentPart.from_text("hello")]) == "hello"


def test_content_parts_convert_to_openai_responses_shape() -> None:
    parts = [
        ContentPart.from_text("What is this?"),
        ContentPart.from_image_url(
            url="data:image/png;base64,abc",
            media_type="image/png",
        ),
    ]

    assert content_parts_to_openai_responses(parts) == [
        {"type": "input_text", "text": "What is this?"},
        {
            "type": "input_image",
            "image_url": "data:image/png;base64,abc",
            "detail": "auto",
        },
    ]


def test_content_parts_convert_to_anthropic_shape() -> None:
    parts = [
        ContentPart.from_image_url(
            url="data:image/jpeg;base64,abc",
            media_type="image/jpeg",
        ),
        ContentPart.from_text("Describe this."),
    ]

    assert content_parts_to_anthropic(parts) == [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": "abc",
            },
        },
        {"type": "text", "text": "Describe this."},
    ]


def test_foreign_chat_raw_assistant_replays_normalized_anthropic_tool_use() -> None:
    message = ConversationMessage(
        role="assistant",
        content=[ContentPart.from_text("Checking.")],
        tool_calls=[ToolCall(id="call_1", name="lookup", arguments={"q": "vr"})],
        raw_provider_data={
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": '{"q":"vr"}'},
                }
            ],
        },
    )

    assert conversation_message_to_anthropic(message) == {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "Checking."},
            {
                "type": "tool_use",
                "id": "call_1",
                "name": "lookup",
                "input": {"q": "vr"},
            },
        ],
    }


def test_content_parts_normalize_mismatched_data_url_media_type() -> None:
    part = ContentPart.from_image_url(
        url="data:image/webp;base64,iVBORw0KGgo=",
        media_type="image/webp",
    )

    assert content_parts_to_openai_chat([part]) == [
        {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64,iVBORw0KGgo=", "detail": "auto"},
        }
    ]
    assert content_parts_to_openai_responses([part]) == [
        {
            "type": "input_image",
            "image_url": "data:image/png;base64,iVBORw0KGgo=",
            "detail": "auto",
        }
    ]
    assert content_parts_to_anthropic([part]) == [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": "iVBORw0KGgo=",
            },
        }
    ]


def test_tool_schema_converts_for_provider_shapes() -> None:
    schema = {
        "name": "lookup",
        "description": "Look something up",
        "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
    }

    assert tool_schema_to_openai_chat(schema) == {
        "type": "function",
        "function": schema,
    }
    assert tool_schema_to_openai_responses(schema) == {
        "type": "function",
        "name": "lookup",
        "description": "Look something up",
        "parameters": schema["parameters"],
    }
    assert tool_schema_to_anthropic(schema) == {
        "name": "lookup",
        "description": "Look something up",
        "input_schema": schema["parameters"],
    }
