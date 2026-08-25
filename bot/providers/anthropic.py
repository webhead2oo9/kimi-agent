from __future__ import annotations

from typing import Any

from anthropic import AsyncAnthropic, Timeout

from providers.base import LLMProvider
from providers.serializers import (
    anthropic_messages,
    normalize_anthropic_stop_reason,
    tool_schema_to_anthropic,
)
from providers.types import (
    ProviderCapability,
    ProviderRequest,
    ProviderResponse,
    ToolCall,
)
from providers.usage_fields import anthropic_usage_dict

_ADAPTIVE_THINKING_MODELS = (
    "claude-fable-5",
    "claude-mythos-5",
    "claude-mythos-preview",
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-sonnet-4-6",
)


class AnthropicProvider(LLMProvider):
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        timeout_seconds: float = 900.0,
    ) -> None:
        self._client = AsyncAnthropic(
            api_key=api_key,
            timeout=Timeout(timeout_seconds, connect=5.0),
            # Availability retries belong to FailoverProvider.
            max_retries=0,
        )
        self._model = model

    async def close(self) -> None:
        await self._client.close()

    @property
    def provider_key(self) -> str:
        return "anthropic"

    @property
    def model(self) -> str:
        return self._model

    @property
    def capabilities(self) -> set[ProviderCapability]:
        return {
            ProviderCapability.TEXT,
            ProviderCapability.IMAGE_INPUT,
            ProviderCapability.TOOL_CALLING,
        }

    async def run_turn(self, request: ProviderRequest) -> ProviderResponse:
        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": request.max_tokens,
            "messages": anthropic_messages(request),
        }
        if request.system_prompt:
            kwargs["system"] = request.system_prompt
        if request.tools:
            kwargs["tools"] = [tool_schema_to_anthropic(t) for t in request.tools]
        if request.reasoning_enabled and self._uses_adaptive_thinking():
            kwargs["thinking"] = {"type": "adaptive"}
            kwargs["output_config"] = {"effort": "high"}

        response = await self._create_message(kwargs)
        content = list(getattr(response, "content", []) or [])
        return ProviderResponse(
            content=self._text_from_blocks(content),
            tool_calls=self._parse_tool_calls(content),
            finish_reason=normalize_anthropic_stop_reason(getattr(response, "stop_reason", None)),
            usage=anthropic_usage_dict(getattr(response, "usage", None)),
            model=str(getattr(response, "model", "") or ""),
            raw_message={
                "role": "assistant",
                "content": self._blocks_to_data(content),
            },
        )

    async def _create_message(self, kwargs: dict[str, Any]) -> Any:
        stream = getattr(self._client.messages, "stream", None)
        if callable(stream):
            async with stream(**kwargs) as message_stream:
                return await message_stream.get_final_message()
        return await self._client.messages.create(**kwargs)

    @staticmethod
    def _parse_tool_calls(content: list[Any]) -> list[ToolCall]:
        calls: list[ToolCall] = []
        for block in content:
            if getattr(block, "type", None) == "tool_use":
                calls.append(
                    ToolCall(
                        id=block.id,
                        name=block.name,
                        arguments=dict(block.input or {}),
                    )
                )
        return calls

    @staticmethod
    def _text_from_blocks(content: list[Any]) -> str | None:
        text_parts = [
            block.text
            for block in content
            if getattr(block, "type", None) == "text" and getattr(block, "text", None)
        ]
        return "\n".join(text_parts) if text_parts else None

    @staticmethod
    def _blocks_to_data(content: list[Any]) -> list[dict[str, Any]]:
        data: list[dict[str, Any]] = []
        for block in content:
            block_type = getattr(block, "type", None)
            if block_type == "text":
                data.append({"type": "text", "text": getattr(block, "text", "")})
            elif block_type == "tool_use":
                data.append(
                    {
                        "type": "tool_use",
                        "id": getattr(block, "id", ""),
                        "name": getattr(block, "name", ""),
                        "input": getattr(block, "input", {}) or {},
                    }
                )
            elif block_type == "thinking":
                data.append(
                    {
                        "type": "thinking",
                        "thinking": getattr(block, "thinking", ""),
                        "signature": getattr(block, "signature", ""),
                    }
                )
            elif block_type == "redacted_thinking":
                data.append(
                    {
                        "type": "redacted_thinking",
                        "data": getattr(block, "data", ""),
                    }
                )
        return data

    def _uses_adaptive_thinking(self) -> bool:
        model = self._model.lower()
        return any(model.startswith(prefix) for prefix in _ADAPTIVE_THINKING_MODELS)
