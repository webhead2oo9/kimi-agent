from __future__ import annotations

from typing import Any

from openai import AsyncOpenAI

from branding import DEFAULT_BOT_NAME, provider_identity
from providers.base import LLMProvider
from providers.errors import ProviderAvailabilityError, ProviderError
from providers.failure_policy import raise_for_terminal_finish_reason

# The WAF-defeating User-Agent and the keyless-endpoint placeholder are shared
# with the Chat Completions providers: this provider accepts the same kind of
# arbitrary base_url gateways, so the same hardening applies.
from providers.openai_chat import _KEYLESS_PLACEHOLDER
from providers.serializers import (
    content_parts_to_openai_responses,
    text_from_content_parts,
    tool_schema_to_openai_responses,
)
from providers.tool_arguments import parse_tool_arguments
from providers.types import (
    ConversationMessage,
    ProviderCapability,
    ProviderRequest,
    ProviderResponse,
    ToolCall,
)
from providers.usage_fields import usage_detail_dict, usage_field

_RATE_LIMIT_ERROR_CODES = frozenset({"rate_limit_exceeded", "rate_limit_error"})


class _OpenAIResponsesRequestError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        code: str,
        retry_after_seconds: float | None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.retry_after_seconds = retry_after_seconds


class OpenAIResponsesProvider(LLMProvider):
    """Stateless OpenAI-compatible Responses API provider.

    Every request replays local conversation history and explicitly sends
    ``store=false``. Remote response IDs are neither recorded nor consumed.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str | None = None,
        service_tier: str | None = None,
        reasoning_effort: str = "",
        timeout_seconds: float | None = None,
        user_agent: str = DEFAULT_BOT_NAME,
    ) -> None:
        kwargs: dict[str, Any] = {
            # Empty key = keyless gateway holding the upstream credentials; see
            # openai_chat.py for why it gets a placeholder rather than "".
            "api_key": api_key or _KEYLESS_PLACEHOLDER,
            "max_retries": 0,
            "default_headers": {"User-Agent": provider_identity(user_agent)},
        }
        if base_url:
            kwargs["base_url"] = base_url
        if timeout_seconds is not None:
            kwargs["timeout"] = timeout_seconds
        self._client = AsyncOpenAI(**kwargs)
        self._base_url = str(self._client.base_url).rstrip("/")
        self._model = model
        self._service_tier = service_tier
        self._reasoning_effort = reasoning_effort

    async def close(self) -> None:
        await self._client.close()

    @property
    def provider_key(self) -> str:
        return "openai_responses"

    @property
    def model(self) -> str:
        return self._model

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def capabilities(self) -> set[ProviderCapability]:
        capabilities = {
            ProviderCapability.TEXT,
            ProviderCapability.IMAGE_INPUT,
            ProviderCapability.TOOL_CALLING,
        }
        if self._service_tier == "flex":
            capabilities.add(ProviderCapability.FLEX_SERVICE_TIER)
        return capabilities

    async def run_turn(self, request: ProviderRequest) -> ProviderResponse:
        kwargs: dict[str, Any] = {
            "model": self._model,
            "instructions": request.system_prompt or None,
            "input": self._build_input(request),
            "max_output_tokens": request.max_tokens,
            "store": False,
        }
        tools = [tool_schema_to_openai_responses(tool) for tool in request.tools]
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        if self._service_tier:
            kwargs["service_tier"] = self._service_tier
        effort = self._effective_reasoning_effort(request)
        if effort:
            kwargs["reasoning"] = {"effort": effort}
            # store=false discards server-side reasoning state, so continuity
            # across tool-call rounds exists only if the encrypted reasoning
            # items come back and are replayed (_replayable_output keeps them).
            kwargs["include"] = ["reasoning.encrypted_content"]

        response = await self._client.responses.create(
            **{key: value for key, value in kwargs.items() if value is not None}
        )
        return self._response_from_native(response)

    def _effective_reasoning_effort(self, request: ProviderRequest) -> str:
        # When neither the profile nor the turn requests reasoning, send no
        # reasoning parameter at all, so compat gateways behind this provider
        # keep seeing the request shape they saw before reasoning was wired up.
        # Once reasoning is in play, a reasoning-disabled turn (compaction,
        # finalizers) pins the cheapest effort rather than the profile baseline,
        # matching codex.py; otherwise the turn's monotonic escalation wins over
        # that baseline.
        if not self._reasoning_effort and not request.reasoning_effort:
            return ""
        if not request.reasoning_enabled:
            return "low"
        return request.reasoning_effort or self._reasoning_effort

    def _build_input(self, request: ProviderRequest) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for message in request.messages:
            items.extend(self._conversation_message_to_items(message))
        if request.current_user_parts:
            items.append(
                {
                    "role": "user",
                    "content": content_parts_to_openai_responses(request.current_user_parts),
                }
            )
        return items

    @staticmethod
    def _conversation_message_to_items(
        message: ConversationMessage,
    ) -> list[dict[str, Any]]:
        if message.raw_provider_data.get("type") == "response_output":
            output = message.raw_provider_data.get("output")
            return list(output) if isinstance(output, list) else []
        if message.role in {"user", "assistant"}:
            return [
                {
                    "role": message.role,
                    "content": content_parts_to_openai_responses(message.content),
                }
            ]
        if message.role == "tool" and message.tool_call_id:
            return [
                {
                    "type": "function_call_output",
                    "call_id": message.tool_call_id,
                    "output": text_from_content_parts(message.content),
                }
            ]
        return []

    def _response_from_native(self, response: Any) -> ProviderResponse:
        output = list(getattr(response, "output", []) or [])
        raw_usage = getattr(response, "usage", None)
        finish_reason = self._finish_reason(response)
        is_incomplete = getattr(response, "status", None) == "incomplete"
        replay_output = (
            [item for item in output if getattr(item, "type", None) != "function_call"]
            if is_incomplete
            else output
        )
        return ProviderResponse(
            content=getattr(response, "output_text", None),
            tool_calls=[] if is_incomplete else self._parse_tool_calls(output),
            finish_reason=finish_reason,
            usage=self._usage_dict(raw_usage),
            usage_present=raw_usage is not None,
            model=str(getattr(response, "model", "") or ""),
            raw_message={
                "type": "response_output",
                "output": self._replayable_output(replay_output),
            },
        )

    @staticmethod
    def _finish_reason(response: Any) -> str:
        status = getattr(response, "status", "completed") or "completed"
        if status == "failed":
            error = getattr(response, "error", None)
            code = error.get("code") if isinstance(error, dict) else getattr(error, "code", None)
            suffix = f" ({code})" if isinstance(code, str) and code else ""
            if code == "server_error":
                raise ProviderAvailabilityError(f"Responses API returned a failed response{suffix}")
            if code in _RATE_LIMIT_ERROR_CODES:
                raise _OpenAIResponsesRequestError(
                    f"Responses API returned a failed response{suffix}",
                    status_code=429,
                    code=code,
                    retry_after_seconds=_retry_after_seconds(response, error),
                )
            raise ProviderError(f"Responses API returned a failed response{suffix}")
        if status == "incomplete":
            details = getattr(response, "incomplete_details", None)
            reason = (
                details.get("reason")
                if isinstance(details, dict)
                else getattr(details, "reason", None)
            )
            if reason == "max_output_tokens":
                return "length"
            raise_for_terminal_finish_reason(reason)
        return str(status)

    @staticmethod
    def _parse_tool_calls(output: list[Any]) -> list[ToolCall]:
        calls: list[ToolCall] = []
        for item in output:
            if getattr(item, "type", None) != "function_call":
                continue
            calls.append(
                ToolCall(
                    id=str(getattr(item, "call_id", getattr(item, "id", ""))),
                    name=str(getattr(item, "name", "")),
                    arguments=parse_tool_arguments(str(getattr(item, "arguments", "") or "{}")),
                )
            )
        return calls

    @staticmethod
    def _usage_dict(usage: Any) -> dict[str, Any]:
        if not usage:
            return {}
        data = {
            "input_tokens": usage_field(usage, "input_tokens", 0),
            "output_tokens": usage_field(usage, "output_tokens", 0),
            "total_tokens": usage_field(usage, "total_tokens", 0),
        }
        details = usage_detail_dict(
            usage_field(usage, "input_tokens_details"),
            ("cached_tokens",),
        )
        if details:
            data["input_tokens_details"] = details
        return data

    @staticmethod
    def _replayable_output(output: list[Any]) -> list[dict[str, Any]]:
        replayable: list[dict[str, Any]] = []
        for item in output:
            item_type = str(getattr(item, "type", "") or "")
            if item_type not in {"function_call", "message", "reasoning"}:
                continue
            if hasattr(item, "model_dump"):
                data = item.model_dump(mode="json", exclude_none=True)
            elif isinstance(item, dict):
                data = dict(item)
            else:
                data = {
                    key: value
                    for key, value in vars(item).items()
                    if not key.startswith("_") and value is not None
                }
            if isinstance(data, dict):
                replayable.append(data)
        return replayable


def _retry_after_seconds(response: Any, error: Any) -> float | None:
    def field(value: Any, name: str) -> Any:
        return value.get(name) if isinstance(value, dict) else getattr(value, name, None)

    raw_seconds = field(error, "retry_after_seconds")
    if raw_seconds is None:
        raw_seconds = field(error, "retry_after")
    if raw_seconds is None:
        raw_seconds = field(response, "retry_after_seconds")
    if raw_seconds is None:
        raw_seconds = field(response, "retry_after")
    raw_milliseconds = field(error, "retry_after_ms")
    if raw_milliseconds is None:
        raw_milliseconds = field(response, "retry_after_ms")
    try:
        seconds = (
            float(raw_seconds)
            if raw_seconds is not None
            else float(raw_milliseconds) / 1000
            if raw_milliseconds is not None
            else None
        )
    except TypeError, ValueError:
        return None
    return seconds if seconds is not None and seconds >= 0 else None
