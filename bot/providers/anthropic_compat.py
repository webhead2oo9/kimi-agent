from __future__ import annotations

import json
from typing import Any

import httpx

from providers.base import LLMProvider
from providers.usage_fields import anthropic_usage_dict
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

# Pinned Anthropic Messages API version. This is the only Anthropic-specific
# header we send: the native `anthropic` provider may additionally need beta
# headers (anthropic-beta, etc.) that must NOT leak onto a third-party compat
# endpoint, which is exactly why this provider is standalone.
_ANTHROPIC_VERSION = "2023-06-01"

# One rolling prompt-cache breakpoint per request, on the last content block of
# the last message. The cached prefix is everything before it (system prompt,
# tool schemas, and the whole transcript), so a tool-heavy ReAct turn reads the
# previous iteration's prefix instead of re-reading it from scratch. A breakpoint
# inside ``system`` is silently ignored by ccflare's claude-code route, which is
# why this rides the message list. Anthropic allows 4; we send 1.
_CACHE_CONTROL = {"type": "ephemeral"}

# Thinking blocks must be echoed back verbatim, so the breakpoint goes on the
# last block that is not one.
_UNMARKABLE_BLOCK_TYPES = frozenset({"thinking", "redacted_thinking"})

# Anthropic's `output_config.effort` ladder, narrower than the agent's internal
# REASONING_EFFORT_ORDER. Mirrors config/model_config.py:ANTHROPIC_EFFORT_LEVELS,
# duplicated here so the provider stays independent of the config layer.
_ANTHROPIC_EFFORT_LEVELS = frozenset({"low", "medium", "high", "xhigh", "max"})


class AnthropicCompatProvider(LLMProvider):
    """Minimal Anthropic Messages-over-HTTP provider for compat gateways.

    Speaks the Anthropic Messages protocol (POST ``{base_url}/messages``) against
    a third-party endpoint that authenticates with an ``x-api-key`` header --
    e.g. OpenCode Zen serving MiniMax M3. Message/tool serialization reuses
    providers/serializers.py. Unlike the native ``anthropic`` provider, this sends
    no SDK/beta headers.
    """

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str,
        timeout_seconds: float = 900.0,
        prompt_caching: bool = True,
        effort: str = "",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api_key = api_key
        self._timeout = timeout_seconds
        self._prompt_caching = prompt_caching
        self._effort = effort
        self._transport = transport

    @property
    def provider_key(self) -> str:
        return "anthropic_compat"

    @property
    def model(self) -> str:
        return self._model

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def capabilities(self) -> set[ProviderCapability]:
        return {
            ProviderCapability.TEXT,
            ProviderCapability.IMAGE_INPUT,
            ProviderCapability.TOOL_CALLING,
        }

    async def run_turn(self, request: ProviderRequest) -> ProviderResponse:
        body = self._build_body(request)
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": _ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            response = await client.post(f"{self._base_url}/messages", json=body, headers=headers)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise httpx.HTTPStatusError(
                f"anthropic_compat endpoint returned {response.status_code}: {response.text[:500]}",
                request=exc.request,
                response=exc.response,
            ) from exc
        return self._response_from_json(json.loads(response.text))

    def _build_body(self, request: ProviderRequest) -> dict[str, Any]:
        messages = anthropic_messages(request)
        if self._prompt_caching:
            self._mark_cache_breakpoint(messages)
        body: dict[str, Any] = {
            "model": self._model,
            "max_tokens": request.max_tokens,
            "messages": messages,
        }
        if request.system_prompt:
            body["system"] = request.system_prompt
        if request.tools:
            body["tools"] = [tool_schema_to_anthropic(t) for t in request.tools]
        effort = self._effective_effort(request)
        if effort:
            body["output_config"] = {"effort": effort}
        return body

    def _effective_effort(self, request: ProviderRequest) -> str:
        # The turn's monotonic tool escalation wins over the profile baseline; an
        # escalation into an effort Anthropic does not accept is dropped rather
        # than sent, since a bad value is a deterministic 400 mid-turn.
        escalated = request.reasoning_effort
        if escalated and escalated in _ANTHROPIC_EFFORT_LEVELS:
            return escalated
        return self._effort

    @staticmethod
    def _mark_cache_breakpoint(messages: list[dict[str, Any]]) -> None:
        for message in reversed(messages):
            content = message.get("content")
            if not isinstance(content, list) or not content:
                continue
            index = next(
                (
                    i
                    for i in range(len(content) - 1, -1, -1)
                    if isinstance(content[i], dict)
                    and content[i].get("type") not in _UNMARKABLE_BLOCK_TYPES
                ),
                None,
            )
            if index is None:
                continue
            # Copy the list and the block: an assistant message's content list is
            # shared with the stored raw_provider_data, and a breakpoint written
            # back there would ride along in every later turn until the request
            # exceeded Anthropic's 4-breakpoint limit.
            marked = list(content)
            marked[index] = {**content[index], "cache_control": dict(_CACHE_CONTROL)}
            message["content"] = marked
            return

    def _response_from_json(self, data: dict[str, Any]) -> ProviderResponse:
        blocks = data.get("content") or []
        text_parts = [
            block.get("text", "")
            for block in blocks
            if block.get("type") == "text" and block.get("text")
        ]
        tool_calls = [
            ToolCall(
                id=block.get("id", ""),
                name=block.get("name", ""),
                arguments=dict(block.get("input") or {}),
            )
            for block in blocks
            if block.get("type") == "tool_use"
        ]
        usage = anthropic_usage_dict(data.get("usage") or {})
        return ProviderResponse(
            content="\n".join(text_parts) if text_parts else None,
            tool_calls=tool_calls,
            finish_reason=normalize_anthropic_stop_reason(data.get("stop_reason")),
            usage=usage,
            model=str(data.get("model") or ""),
            raw_message={"role": "assistant", "content": self._blocks_to_data(blocks)},
        )

    @staticmethod
    def _blocks_to_data(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        data: list[dict[str, Any]] = []
        for block in blocks:
            block_type = block.get("type")
            if block_type == "text":
                data.append({"type": "text", "text": block.get("text", "")})
            elif block_type == "tool_use":
                data.append(
                    {
                        "type": "tool_use",
                        "id": block.get("id", ""),
                        "name": block.get("name", ""),
                        "input": block.get("input") or {},
                    }
                )
            elif block_type == "thinking":
                # ccflare's claude-code route enables extended thinking, and a
                # continuation that drops these blocks can be rejected. Mirrors
                # the native anthropic provider.
                data.append(
                    {
                        "type": "thinking",
                        "thinking": block.get("thinking", ""),
                        "signature": block.get("signature", ""),
                    }
                )
            elif block_type == "redacted_thinking":
                data.append({"type": "redacted_thinking", "data": block.get("data", "")})
        return data
