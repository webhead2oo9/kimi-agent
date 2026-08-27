from __future__ import annotations

import base64
from typing import Any

from utils.image_types import normalize_image_data_url
from providers.types import ContentPart, ContentPartType, ConversationMessage, ProviderRequest

_ANTHROPIC_ASSISTANT_BLOCK_TYPES = frozenset({"text", "tool_use", "thinking", "redacted_thinking"})


def content_parts_to_openai_chat(parts: list[ContentPart]) -> str | list[dict[str, Any]]:
    if len(parts) == 1 and parts[0].type is ContentPartType.TEXT:
        return parts[0].text or ""

    converted: list[dict[str, Any]] = []
    for part in parts:
        if part.type is ContentPartType.TEXT:
            converted.append({"type": "text", "text": part.text or ""})
        elif part.type is ContentPartType.IMAGE and part.image_url:
            image_url_value, _media_type = _normalized_image_url_and_media_type(part)
            image_url: dict[str, Any] = {"url": image_url_value}
            if part.detail:
                image_url["detail"] = part.detail
            converted.append({"type": "image_url", "image_url": image_url})
    return converted


def content_parts_to_openai_responses(parts: list[ContentPart]) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for part in parts:
        if part.type is ContentPartType.TEXT:
            converted.append({"type": "input_text", "text": part.text or ""})
        elif part.type is ContentPartType.IMAGE and part.image_url:
            image_url_value, _media_type = _normalized_image_url_and_media_type(part)
            item: dict[str, Any] = {
                "type": "input_image",
                "image_url": image_url_value,
            }
            if part.detail:
                item["detail"] = part.detail
            converted.append(item)
    return converted


def content_parts_to_anthropic(parts: list[ContentPart]) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for part in parts:
        if part.type is ContentPartType.TEXT:
            converted.append({"type": "text", "text": part.text or ""})
        elif part.type is ContentPartType.IMAGE and part.image_url and part.media_type:
            image_url_value, media_type = _normalized_image_url_and_media_type(part)
            converted.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type or part.media_type,
                        "data": _data_url_payload(image_url_value),
                    },
                }
            )
    return converted


def conversation_message_to_anthropic(
    message: ConversationMessage,
) -> dict[str, Any] | None:
    """Serialize normalized conversation state for an Anthropic Messages request.

    Provider-native raw assistant blocks are replayed verbatim so signed thinking
    blocks survive a same-provider tool continuation. Raw payloads from another
    provider are deliberately ignored: the provider-neutral content and tool
    calls are enough to rebuild a valid assistant turn before its tool results.
    """
    if _is_anthropic_assistant_message(message.raw_provider_data):
        return dict(message.raw_provider_data)
    if message.role == "tool" and message.tool_call_id:
        return {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": message.tool_call_id,
                    "content": text_from_content_parts(message.content),
                }
            ],
        }
    if message.role == "user":
        return {
            "role": "user",
            "content": content_parts_to_anthropic(message.content),
        }
    if message.role == "assistant":
        content = content_parts_to_anthropic(message.content)
        content.extend(
            {
                "type": "tool_use",
                "id": tool_call.id,
                "name": tool_call.name,
                "input": dict(tool_call.arguments),
            }
            for tool_call in message.tool_calls
        )
        return {"role": "assistant", "content": content}
    return None


def anthropic_messages(request: ProviderRequest) -> list[dict[str, Any]]:
    """The request's history plus this turn's user parts, Anthropic-shaped.

    Shared by the SDK and compatibility-gateway providers so both emit the same
    Anthropic message shape.
    """

    messages = [
        converted
        for msg in request.messages
        if (converted := conversation_message_to_anthropic(msg)) is not None
    ]
    if request.current_user_parts:
        messages.append(
            {
                "role": "user",
                "content": content_parts_to_anthropic(request.current_user_parts),
            }
        )
    return messages


def _is_anthropic_assistant_message(raw: dict[str, Any]) -> bool:
    if raw.get("role") != "assistant":
        return False
    content = raw.get("content")
    return isinstance(content, list) and all(
        isinstance(block, dict) and block.get("type") in _ANTHROPIC_ASSISTANT_BLOCK_TYPES
        for block in content
    )


def tool_schema_to_openai_chat(schema: dict[str, Any]) -> dict[str, Any]:
    return {"type": "function", "function": schema}


def tool_schema_to_openai_responses(schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "name": schema["name"],
        "description": schema.get("description", ""),
        "parameters": schema.get("parameters", {"type": "object", "properties": {}}),
    }


def tool_schema_to_anthropic(schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": schema["name"],
        "description": schema.get("description", ""),
        "input_schema": schema.get("parameters", {"type": "object", "properties": {}}),
    }


def text_from_content_parts(parts: list[ContentPart]) -> str:
    return "\n".join(part.text or "" for part in parts if part.type is ContentPartType.TEXT)


def normalize_anthropic_stop_reason(stop_reason: str | None) -> str:
    """Map an Anthropic stop_reason onto the provider-neutral finish_reason values.

    Anthropic reports token truncation as "max_tokens", but the agent loop
    detects truncation via finish_reason == "length" (the value the OpenAI-shaped
    providers emit), so normalize it here.
    """
    if not stop_reason:
        return "end_turn"
    if stop_reason == "max_tokens":
        return "length"
    return stop_reason


def _data_url_payload(value: str) -> str:
    if value.startswith("data:") and "," in value:
        return value.split(",", 1)[1]
    base64.b64decode(value, validate=True)
    return value


def _normalized_image_url_and_media_type(part: ContentPart) -> tuple[str, str | None]:
    if not part.image_url:
        return "", part.media_type
    return normalize_image_data_url(part.image_url, part.media_type)
