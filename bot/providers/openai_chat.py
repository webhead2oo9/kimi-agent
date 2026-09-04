from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from dataclasses import replace
from typing import Any

from openai import AsyncOpenAI, BadRequestError

from branding import DEFAULT_BOT_NAME, provider_identity
from providers.base import LLMProvider
from providers.errors import ProviderAvailabilityError
from providers.failure_policy import raise_for_terminal_finish_reason
from providers.serializers import (
    content_parts_to_openai_chat,
    text_from_content_parts,
    tool_schema_to_openai_chat,
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

log = logging.getLogger(__name__)

# Some OpenAI-compatible proxies WAF-block the OpenAI SDK's default User-Agent
# ("OpenAI/Python ...") with a 403 "Your request was blocked." Send a neutral
# User-Agent so requests get through; no real OpenAI-compatible API requires the
# SDK's UA.
# Sent as the bearer token for keyless endpoints, which ignore it. Deliberately
# not an empty string (the SDK will not construct) and deliberately not
# key-shaped, so it is obvious in a request log that no credential was intended.
_KEYLESS_PLACEHOLDER = "keyless"

# How often the in-flight stream watchdog reports progress (and how long a
# chunkless silence must last before it escalates to WARNING).
_STREAM_LOG_INTERVAL_SECONDS = 15.0


class _StreamAccumulator:
    """Assembles chat-completion chunks and tracks stall-diagnosis stats.

    The stats exist so a hung or deadline-cancelled request leaves evidence in
    the log of whether the backend was generating (chunks flowing) or silent.
    """

    def __init__(self, model: str) -> None:
        self.model = model
        self.started_at = time.monotonic()
        self.last_event_at = self.started_at
        self.chunks = 0
        self.content_parts: list[str] = []
        self.reasoning_parts: list[str] = []
        self.tool_call_parts: dict[int, dict[str, str]] = {}
        self.finish_reason: str | None = None
        self.usage: Any | None = None
        self.served_model = ""

    def ingest(self, chunk: Any) -> None:
        self.chunks += 1
        self.last_event_at = time.monotonic()
        if getattr(chunk, "usage", None) is not None:
            self.usage = chunk.usage
        native_model = getattr(chunk, "model", None)
        if isinstance(native_model, str) and native_model:
            self.served_model = native_model
        choices = getattr(chunk, "choices", None) or []
        if not choices:
            return
        choice = choices[0]
        if getattr(choice, "finish_reason", None):
            self.finish_reason = choice.finish_reason
        delta = getattr(choice, "delta", None)
        if delta is None:
            return
        content = getattr(delta, "content", None)
        if isinstance(content, str):
            self.content_parts.append(content)
        reasoning = OpenAIChatProvider._message_field(
            delta, "reasoning_content"
        ) or OpenAIChatProvider._message_field(delta, "reasoning")
        if reasoning:
            self.reasoning_parts.append(reasoning)
        for tc in getattr(delta, "tool_calls", None) or []:
            slot = self.tool_call_parts.setdefault(
                getattr(tc, "index", 0) or 0, {"id": "", "name": "", "arguments": ""}
            )
            if getattr(tc, "id", None):
                slot["id"] += tc.id
            function = getattr(tc, "function", None)
            if function is not None:
                if getattr(function, "name", None):
                    slot["name"] += function.name
                if getattr(function, "arguments", None):
                    slot["arguments"] += function.arguments

    def content(self) -> str | None:
        return "".join(self.content_parts) if self.content_parts else None

    def reasoning(self) -> str | None:
        return "".join(self.reasoning_parts) if self.reasoning_parts else None

    def tool_calls(self) -> list[ToolCall]:
        calls: list[ToolCall] = []
        for index in sorted(self.tool_call_parts):
            slot = self.tool_call_parts[index]
            calls.append(
                ToolCall(
                    id=slot["id"] or f"call_{index}",
                    name=slot["name"],
                    arguments=parse_tool_arguments(slot["arguments"]),
                )
            )
        return calls

    def tool_calls_complete(self) -> bool:
        if not self.tool_call_parts:
            return False
        for slot in self.tool_call_parts.values():
            if not slot["name"]:
                return False
            raw_arguments = slot["arguments"]
            if not raw_arguments:
                continue
            try:
                arguments = json.loads(raw_arguments)
            except json.JSONDecodeError:
                return False
            if not isinstance(arguments, dict):
                return False
        return True

    def idle_seconds(self) -> float:
        return time.monotonic() - self.last_event_at

    def summary(self) -> str:
        return (
            f"model={self.model} elapsed={time.monotonic() - self.started_at:.0f}s "
            f"chunks={self.chunks} last_chunk={self.idle_seconds():.0f}s_ago "
            f"reasoning={sum(len(p) for p in self.reasoning_parts)}ch "
            f"content={sum(len(p) for p in self.content_parts)}ch "
            f"tool_args={sum(len(s['arguments']) for s in self.tool_call_parts.values())}ch "
            f"finish={self.finish_reason}"
        )


class OpenAIChatProvider(LLMProvider):
    """Provider for OpenAI-compatible Chat Completions APIs."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        provider_key: str,
        service_tier: str | None = None,
        reasoning_effort: str = "",
        request_id_header: str = "",
        timeout_seconds: float = 900.0,
        stream: bool = False,
        stall_timeout_seconds: float = 90.0,
        user_agent: str = DEFAULT_BOT_NAME,
    ) -> None:
        # max_retries=0: availability retries belong to the FailoverProvider
        # chain, not silent SDK-internal attempts against the same backend.
        #
        # An empty key is a *keyless* endpoint (a local server on loopback, or a
        # gateway holding the upstream credentials), not a misconfiguration; both
        # config/model_config.py (`keyless: true`) and evals/models.py (no
        # `api_key_env`) reach here with "". The SDK refuses to construct at all
        # without a key, raising "Missing credentials" before any request, so it
        # gets a placeholder it will send and the endpoint will ignore. Any real
        # missing-credential case is caught upstream by the startup gate in
        # app/providers.py:_provider_has_credentials, which is where a genuine
        # misconfiguration should surface.
        self._client = AsyncOpenAI(
            api_key=api_key or _KEYLESS_PLACEHOLDER,
            base_url=base_url,
            default_headers={"User-Agent": provider_identity(user_agent)},
            max_retries=0,
            timeout=timeout_seconds,
        )
        self._base_url = base_url
        self._model = model
        self._provider_key = provider_key
        self._service_tier = service_tier
        self._reasoning_effort = reasoning_effort
        self._request_id_header = request_id_header.strip()
        self._stream = stream
        self._stall_timeout_seconds = stall_timeout_seconds
        self._deepseek_thinking = self._is_deepseek_target(base_url=base_url, model=model)

    async def close(self) -> None:
        await self._client.close()

    @property
    def provider_key(self) -> str:
        return self._provider_key

    @property
    def model(self) -> str:
        return self._model

    @property
    def capabilities(self) -> set[ProviderCapability]:
        caps = {
            ProviderCapability.TEXT,
            ProviderCapability.IMAGE_INPUT,
            ProviderCapability.TOOL_CALLING,
        }
        if self._service_tier == "flex":
            caps.add(ProviderCapability.FLEX_SERVICE_TIER)
        return caps

    async def run_turn(self, request: ProviderRequest) -> ProviderResponse:
        messages = self._build_messages(request)
        return await self._chat_completion(
            messages=messages,
            tools=request.tools if request.tools else None,
            max_tokens=request.max_tokens,
            request=request,
        )

    async def _chat_completion(
        self,
        *,
        messages: list[dict],
        tools: list[dict] | None,
        max_tokens: int,
        request: ProviderRequest,
    ) -> ProviderResponse:
        kwargs = self._build_request_kwargs(
            messages=messages,
            tools=tools,
            max_tokens=max_tokens,
            request=request,
        )
        if self._stream:
            return await self._streaming_chat_completion(kwargs)
        response = await self._client.chat.completions.create(**kwargs)
        return self._response_from_native(response)

    async def _streaming_chat_completion(self, kwargs: dict[str, Any]) -> ProviderResponse:
        """Streaming request with stall diagnostics and a stall abort.

        Chunk cadence is the only client-side signal that separates a backend
        that is still generating from one that has gone silent. A stream that
        keeps producing chunks may run as long as the caller's turn deadline
        allows; one that goes silent for ``stall_timeout_seconds`` (including
        never answering the initial request) is aborted with ``TimeoutError``,
        which the shared provider failure policy classifies as transient so a
        FailoverProvider chain can move to the next backend.
        """
        acc = _StreamAccumulator(self._model)
        watchdog = asyncio.create_task(self._log_stream_progress(acc))
        fallback_error: BadRequestError | None = None
        stall_seconds = self._stall_timeout_seconds
        try:
            try:
                loop = asyncio.get_running_loop()
                async with asyncio.timeout(stall_seconds) as stall_guard:
                    stream = await self._client.chat.completions.create(
                        **kwargs,
                        stream=True,
                        stream_options={"include_usage": True},
                    )
                    async with stream:
                        async for chunk in stream:
                            if acc.chunks == 0:
                                log.info(
                                    "stream first chunk: model=%s after %.1fs",
                                    self._model,
                                    time.monotonic() - acc.started_at,
                                )
                            acc.ingest(chunk)
                            stall_guard.reschedule(loop.time() + stall_seconds)
            except asyncio.CancelledError:
                log.warning("stream abandoned (caller cancelled): %s", acc.summary())
                raise
            except TimeoutError:
                log.warning(
                    "stream stalled: no data for %.0fs, aborting attempt: %s",
                    stall_seconds,
                    acc.summary(),
                )
                raise
            except BadRequestError as exc:
                if acc.chunks:
                    raise
                # Could be a gateway that rejects streaming, or a genuinely bad
                # payload; the non-streaming retry below disambiguates.
                fallback_error = exc
            except Exception as exc:
                log.warning("stream failed (%s: %s): %s", type(exc).__name__, exc, acc.summary())
                raise
        finally:
            watchdog.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watchdog
        if fallback_error is not None:
            return await self._non_streaming_fallback(kwargs, fallback_error)
        if acc.finish_reason is None:
            if acc.tool_call_parts:
                if acc.tool_calls_complete():
                    acc.finish_reason = "tool_calls"
                else:
                    log.warning("stream ended with an incomplete tool call: %s", acc.summary())
                    raise ProviderAvailabilityError(
                        "provider stream ended with an incomplete tool call"
                    )
            elif acc.content():
                acc.finish_reason = "stop"
            else:
                log.warning("stream ended without a complete response: %s", acc.summary())
                raise ProviderAvailabilityError("provider stream ended without a complete response")
            log.debug(
                "stream ended without a terminal finish reason; inferred %s", acc.finish_reason
            )
        tool_calls = acc.tool_calls()
        log.info("stream complete: %s", acc.summary())
        raise_for_terminal_finish_reason(acc.finish_reason)
        provider_response = ProviderResponse(
            content=acc.content(),
            reasoning_content=acc.reasoning(),
            tool_calls=tool_calls,
            finish_reason=acc.finish_reason,
            usage=self._usage_dict(acc.usage),
            usage_present=acc.usage is not None,
            model=acc.served_model,
        )
        return replace(
            provider_response,
            raw_message=self._assistant_message_from_response(provider_response),
        )

    async def _non_streaming_fallback(
        self,
        kwargs: dict[str, Any],
        stream_error: BadRequestError,
    ) -> ProviderResponse:
        log.warning("stream request rejected (%s); retrying without streaming", stream_error)
        response = await self._client.chat.completions.create(**kwargs)
        # Keep the downgrade request-scoped. Provider instances are shared across
        # users, and a rejection may be payload- or route-specific.
        return self._response_from_native(response)

    async def _log_stream_progress(self, acc: _StreamAccumulator) -> None:
        while True:
            await asyncio.sleep(_STREAM_LOG_INTERVAL_SECONDS)
            level = (
                logging.WARNING
                if acc.idle_seconds() >= _STREAM_LOG_INTERVAL_SECONDS
                else logging.INFO
            )
            log.log(level, "stream in flight: %s", acc.summary())

    def _build_request_kwargs(
        self,
        *,
        messages: list[dict],
        tools: list[dict] | None,
        max_tokens: int,
        request: ProviderRequest,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        if self._request_id_header:
            kwargs["extra_headers"] = {self._request_id_header: str(uuid.uuid4())}
        if request.temperature is not None:
            kwargs["temperature"] = request.temperature
        if tools:
            kwargs["tools"] = [tool_schema_to_openai_chat(t) for t in tools]
        if self._service_tier:
            kwargs["service_tier"] = self._service_tier
        if self._provider_key == "openai_compat" and request.reasoning_enabled:
            effort = request.reasoning_effort or self._reasoning_effort
            if effort:
                kwargs["reasoning_effort"] = effort
            if self._deepseek_thinking:
                kwargs.setdefault("reasoning_effort", "high")
                kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
        self._apply_provider_options(kwargs, request)
        return kwargs

    def _apply_provider_options(
        self,
        kwargs: dict[str, Any],
        request: ProviderRequest,
    ) -> None:
        return None

    def _build_messages(self, request: ProviderRequest) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        if request.system_prompt:
            messages.append({"role": "system", "content": request.system_prompt})
        for msg in request.messages:
            messages.append(self._conversation_message_to_chat(msg))
        if request.current_user_parts:
            messages.append(
                {
                    "role": "user",
                    "content": content_parts_to_openai_chat(request.current_user_parts),
                }
            )
        return messages

    def _conversation_message_to_chat(self, msg: ConversationMessage) -> dict[str, Any]:
        if self._is_chat_completion_message(msg.raw_provider_data):
            return dict(msg.raw_provider_data)
        if msg.role == "tool":
            return {
                "role": "tool",
                "tool_call_id": msg.tool_call_id,
                "content": text_from_content_parts(msg.content),
            }
        converted: dict[str, Any] = {
            "role": msg.role,
            "content": content_parts_to_openai_chat(msg.content),
        }
        if msg.role == "assistant" and msg.tool_calls:
            converted["tool_calls"] = self._tool_calls_to_chat(msg.tool_calls)
        return converted

    @staticmethod
    def _is_chat_completion_message(raw: dict[str, Any]) -> bool:
        if raw.get("role") != "assistant":
            return False
        content = raw.get("content")
        return content is None or isinstance(content, str)

    @staticmethod
    def _tool_calls_to_chat(tool_calls: list[ToolCall]) -> list[dict[str, Any]]:
        return [
            {
                "id": tool_call.id,
                "type": "function",
                "function": {
                    "name": tool_call.name,
                    "arguments": json.dumps(tool_call.arguments),
                },
            }
            for tool_call in tool_calls
        ]

    def _response_from_native(self, response: Any) -> ProviderResponse:
        choice = response.choices[0]
        raise_for_terminal_finish_reason(choice.finish_reason)
        msg = choice.message
        parsed_tool_calls = self._parse_tool_calls(getattr(msg, "tool_calls", None))
        raw_usage = getattr(response, "usage", None)
        provider_response = ProviderResponse(
            content=getattr(msg, "content", None),
            # GLM/DeepSeek expose chain-of-thought as `reasoning_content`; kimi (and some
            # OpenRouter-style routes) use `reasoning`. Prefer the former, fall back to the
            # latter so kimi's reasoning is not silently dropped.
            reasoning_content=(
                self._message_field(msg, "reasoning_content")
                or self._message_field(msg, "reasoning")
            ),
            tool_calls=parsed_tool_calls,
            finish_reason=choice.finish_reason or "stop",
            usage=self._usage_dict(raw_usage),
            usage_present=raw_usage is not None,
            model=str(getattr(response, "model", "") or ""),
        )
        return replace(
            provider_response,
            raw_message=self._assistant_message_from_response(provider_response),
        )

    @staticmethod
    def _parse_tool_calls(raw_tool_calls: Any) -> list[ToolCall]:
        parsed: list[ToolCall] = []
        for tc in raw_tool_calls or []:
            args = parse_tool_arguments(tc.function.arguments)
            parsed.append(ToolCall(id=tc.id, name=tc.function.name, arguments=args))
        return parsed

    @staticmethod
    def _usage_dict(usage: Any) -> dict[str, Any]:
        if not usage:
            return {}
        data = {
            "prompt_tokens": usage_field(usage, "prompt_tokens", 0),
            "completion_tokens": usage_field(usage, "completion_tokens", 0),
            "total_tokens": usage_field(usage, "total_tokens", 0),
        }
        prompt_details = usage_detail_dict(
            usage_field(usage, "prompt_tokens_details"),
            ("cached_tokens",),
        )
        if prompt_details:
            data["prompt_tokens_details"] = prompt_details
        return data

    @staticmethod
    def _assistant_message_from_response(response: ProviderResponse) -> dict[str, Any]:
        msg: dict[str, Any] = {"role": "assistant"}
        if response.content is not None:
            msg["content"] = response.content
        if response.reasoning_content is not None:
            msg["reasoning_content"] = response.reasoning_content
        if response.has_tool_calls:
            msg["tool_calls"] = OpenAIChatProvider._tool_calls_to_chat(response.tool_calls)
        return msg

    @staticmethod
    def _message_field(message: object, field: str) -> str | None:
        value = getattr(message, field, None)
        if isinstance(value, str):
            return value
        model_extra = getattr(message, "model_extra", None)
        if isinstance(model_extra, dict):
            extra_value = model_extra.get(field)
            if isinstance(extra_value, str):
                return extra_value
        return None

    @staticmethod
    def _is_deepseek_target(base_url: str, model: str) -> bool:
        return "deepseek" in base_url.lower() or model.lower().startswith("deepseek-")
