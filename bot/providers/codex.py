from __future__ import annotations

import json
from typing import Any

from codex.auth import CodexAuthManager
from codex.transport import CodexTransport, sanitize_codex_input_item_for_replay
from providers.base import LLMProvider
from providers.serializers import (
    content_parts_to_openai_responses,
    text_from_content_parts,
    tool_schema_to_openai_responses,
)
from providers.tool_arguments import parse_tool_arguments
from providers.types import (
    ConversationMessage,
    GeneratedAsset,
    ProviderCapability,
    ProviderRequest,
    ProviderResponse,
    ToolCall,
)


class CodexProvider(LLMProvider):
    def __init__(
        self,
        *,
        auth_manager: CodexAuthManager | None = None,
        transport: Any | None = None,
        model: str = "gpt-5.5",
        reasoning_effort: str = "high",
        image_quality: str = "auto",
        image_format: str = "png",
    ) -> None:
        if transport is None:
            if auth_manager is None:
                raise ValueError("CodexProvider requires auth_manager or transport")
            transport = CodexTransport(auth_manager)
        self._auth_manager = auth_manager
        self._transport = transport
        self._model = model or "gpt-5.5"
        self._reasoning_effort = reasoning_effort or "high"
        self._image_quality = image_quality or "auto"
        self._image_format = image_format or "png"

    @property
    def provider_key(self) -> str:
        return "codex"

    @property
    def model(self) -> str:
        return self._model

    @property
    def capabilities(self) -> set[ProviderCapability]:
        return {
            ProviderCapability.TEXT,
            ProviderCapability.IMAGE_INPUT,
            ProviderCapability.IMAGE_OUTPUT,
            ProviderCapability.TOOL_CALLING,
            ProviderCapability.PREVIOUS_RESPONSE_ID,
        }

    def is_available(self) -> bool:
        return self._auth_manager.is_available() if self._auth_manager else True

    async def close(self) -> None:
        await self._transport.close_all()

    async def run_turn(self, request: ProviderRequest) -> ProviderResponse:
        payload = self._build_payload(request)
        response = await self._transport.send_request(
            str(request.conversation_id),
            payload,
            expected_previous_response_id=request.provider_state.get("latest_response_id"),
        )
        return self._response_from_payload(response)

    def _build_payload(self, request: ProviderRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self._model,
            "instructions": request.system_prompt or None,
            "input": self._build_input(request),
            "store": False,
            "stream": True,
            # NOTE: the ChatGPT-account Codex backend rejects max_output_tokens
            # ("Unsupported parameter"). Output length is not client-capable here.
            "reasoning": {"effort": self._effective_reasoning_effort(request)},
            "include": ["reasoning.encrypted_content"],
        }
        tools = [tool_schema_to_openai_responses(tool) for tool in request.tools]
        if ProviderCapability.IMAGE_OUTPUT in request.requested_capabilities:
            tools.append(
                {
                    "type": "image_generation",
                    "output_format": self._image_format,
                    "quality": self._image_quality,
                }
            )
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        return {key: value for key, value in payload.items() if value is not None}

    def _build_input(self, request: ProviderRequest) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for message in request.messages:
            items.extend(self._conversation_message_to_responses_items(message))
        if request.current_user_parts:
            items.append(
                {
                    "role": "user",
                    "content": content_parts_to_openai_responses(request.current_user_parts),
                }
            )
        return items

    def _conversation_message_to_responses_items(
        self,
        message: ConversationMessage,
    ) -> list[dict[str, Any]]:
        if message.raw_provider_data.get("type") == "response_output":
            output = message.raw_provider_data.get("output")
            if not isinstance(output, list):
                return []
            return [self._replay_item(item) for item in output if isinstance(item, dict)]
        if message.role == "user":
            return [
                {
                    "role": "user",
                    "content": content_parts_to_openai_responses(message.content),
                }
            ]
        if message.role == "assistant":
            text = text_from_content_parts(message.content)
            items: list[dict[str, Any]] = []
            if text:
                items.append(
                    {
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": text}],
                    }
                )
            items.extend(
                {
                    "type": "function_call",
                    "call_id": tool_call.id,
                    "name": tool_call.name,
                    "arguments": json.dumps(tool_call.arguments),
                }
                for tool_call in message.tool_calls
            )
            return items
        if message.role == "tool" and message.tool_call_id:
            return [
                {
                    "type": "function_call_output",
                    "call_id": message.tool_call_id,
                    "output": text_from_content_parts(message.content),
                }
            ]
        return []

    @staticmethod
    def _replay_item(item: dict[str, Any]) -> dict[str, Any]:
        return sanitize_codex_input_item_for_replay(item)

    def _effective_reasoning_effort(self, request: ProviderRequest) -> str:
        if not request.reasoning_enabled:
            return "low"
        return request.reasoning_effort or self._reasoning_effort

    def _response_from_payload(self, payload: dict[str, Any]) -> ProviderResponse:
        output = payload.get("output") or []
        output_items = [item for item in output if isinstance(item, dict)]
        response_id = payload.get("id")
        return ProviderResponse(
            content=self._extract_text(payload, output_items),
            tool_calls=self._parse_tool_calls(output_items),
            finish_reason=str(payload.get("status") or "completed"),
            usage=dict(payload.get("usage") or {}),
            usage_present=payload.get("usage") is not None,
            model=str(payload.get("model") or ""),
            provider_state=(
                {"latest_response_id": response_id} if isinstance(response_id, str) else {}
            ),
            generated_assets=self._parse_generated_assets(output_items),
            raw_message={
                "type": "response_output",
                "output": self._output_items_to_data(output_items),
            },
        )

    @staticmethod
    def _extract_text(payload: dict[str, Any], output_items: list[dict[str, Any]]) -> str | None:
        output_text = payload.get("output_text")
        if isinstance(output_text, str):
            return output_text
        parts: list[str] = []
        for item in output_items:
            if item.get("type") != "message":
                continue
            for block in item.get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "output_text" and isinstance(block.get("text"), str):
                    parts.append(block["text"])
                elif block.get("type") == "refusal" and isinstance(block.get("refusal"), str):
                    parts.append(f"Refusal: {block['refusal']}")
        return "".join(parts) if parts else None

    @staticmethod
    def _parse_tool_calls(output_items: list[dict[str, Any]]) -> list[ToolCall]:
        calls: list[ToolCall] = []
        for index, item in enumerate(output_items, start=1):
            if item.get("type") != "function_call":
                continue
            parsed_args = parse_tool_arguments(item.get("arguments"))
            calls.append(
                ToolCall(
                    id=str(item.get("call_id") or item.get("id") or f"call_{index}"),
                    name=str(item.get("name") or ""),
                    arguments=parsed_args,
                )
            )
        return calls

    def _parse_generated_assets(
        self,
        output_items: list[dict[str, Any]],
    ) -> list[GeneratedAsset]:
        assets: list[GeneratedAsset] = []
        for index, item in enumerate(output_items, start=1):
            if item.get("type") != "image_generation_call":
                continue
            result = item.get("result")
            if not isinstance(result, str) or not result:
                continue
            assets.append(
                GeneratedAsset(
                    kind="image",
                    media_type=self._image_media_type(),
                    data_base64=result,
                    suggested_filename=f"codex-response-{index}.{self._image_extension()}",
                )
            )
        return assets

    def _image_media_type(self) -> str:
        match self._image_extension():
            case "jpg" | "jpeg":
                return "image/jpeg"
            case "webp":
                return "image/webp"
            case _:
                return "image/png"

    def _image_extension(self) -> str:
        image_format = self._image_format.lower().lstrip(".")
        if image_format in {"jpg", "jpeg", "webp"}:
            return image_format
        return "png"

    @staticmethod
    def _output_items_to_data(output_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            CodexProvider._json_safe_data(item)
            for item in output_items
            if isinstance(CodexProvider._json_safe_data(item), dict)
        ]

    @staticmethod
    def _json_safe_data(value: Any) -> Any:
        if isinstance(value, str | int | float | bool) or value is None:
            return value
        if isinstance(value, list | tuple):
            return [CodexProvider._json_safe_data(item) for item in value]
        if isinstance(value, dict):
            return {
                str(key): CodexProvider._json_safe_data(item)
                for key, item in value.items()
                if item is not None
            }
        if hasattr(value, "model_dump"):
            return value.model_dump(mode="json", exclude_none=True)
        if hasattr(value, "__dict__"):
            return {
                key: CodexProvider._json_safe_data(item)
                for key, item in vars(value).items()
                if not key.startswith("_") and item is not None
            }
        return str(value)
