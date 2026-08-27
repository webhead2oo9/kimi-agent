from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from typing import Any

import aiohttp

from codex.auth import CodexAuthManager
from codex.types import CodexSession, CodexSessionState, PreparedCodexWebSocketRequest

CODEX_WS_BETA_HEADER = "responses_websockets=2026-02-06"
# The generated-asset writer accepts 25 MiB decoded across one response. Base64
# expands that to roughly 33.4 MiB; retain a finite 2 MiB envelope allowance for
# JSON structure, text, reasoning metadata, and the other response output items.
CODEX_MAX_WS_MESSAGE_BYTES = ((25 * 1024 * 1024 + 2) // 3) * 4 + 2 * 1024 * 1024
_RETRY_AFTER_MESSAGE_RE = re.compile(
    r"try again in\s*(\d+(?:\.\d+)?)\s*(s|ms|seconds?)",
    re.IGNORECASE,
)
# The backend resolves a bare model id against a per-client bucket before looking
# it up. Without an originator it picks a restricted bucket, so newer models come
# back as "Model not found <model>-free-1p-...". Identifying as the Codex CLI is
# what makes ids like gpt-5.6-luna resolve.
CODEX_ORIGINATOR = "codex_cli_rs"
RESPONSE_CREATE_TYPE = "response.create"
WEBSOCKET_REPLAY_STRIP_KEYS = {
    "image_generation_call": {"result"},
    "web_search_call": {"action"},
    "web_search_preview_call": {"action"},
}

log = logging.getLogger(__name__)


class CodexWebSocketRequestError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        status_code: int | None = None,
        code: str | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code
        self.code = code
        self.retry_after_seconds = retry_after_seconds


def _request_error(exc: Exception) -> CodexWebSocketRequestError:
    if isinstance(exc, CodexWebSocketRequestError):
        return exc
    return CodexWebSocketRequestError(
        str(exc),
        retryable=isinstance(exc, (aiohttp.ClientError, OSError, TimeoutError)),
    )


def stable_stringify(value: Any) -> str:
    if isinstance(value, list):
        return "[" + ",".join(stable_stringify(item) for item in value) + "]"
    if isinstance(value, dict):
        parts = []
        for key in sorted(value):
            item = value[key]
            if item is None:
                continue
            parts.append(f"{json.dumps(str(key))}:{stable_stringify(item)}")
        return "{" + ",".join(parts) + "}"
    return json.dumps(value, sort_keys=True)


def sanitize_codex_input_item_for_replay(item: dict[str, Any]) -> dict[str, Any]:
    return _sanitize_codex_replay_value(item)


def _sanitize_codex_replay_value(value: Any, root_type: str | None = None) -> Any:
    if isinstance(value, list):
        return [_sanitize_codex_replay_value(item, root_type) for item in value]
    if not isinstance(value, dict):
        return value
    value_type = value.get("type")
    next_root_type = value_type if isinstance(value_type, str) else root_type
    sanitized: dict[str, Any] = {}
    for key, item in value.items():
        if key in WEBSOCKET_REPLAY_STRIP_KEYS.get(next_root_type or "", set()):
            continue
        if item is not None:
            sanitized[str(key)] = _sanitize_codex_replay_value(item, next_root_type)
    return sanitized


def prepare_codex_websocket_request(
    payload: dict[str, Any],
    session_state: CodexSessionState,
    expected_previous_response_id: str | None = None,
) -> PreparedCodexWebSocketRequest:
    full_input = [
        sanitize_codex_input_item_for_replay(item)
        for item in payload.get("input", [])
        if isinstance(item, dict)
    ]
    base_request = _build_websocket_request_base(payload, full_input)
    request_signature = stable_stringify(
        {
            **base_request,
            "input": [],
            "previous_response_id": None,
        }
    )
    baseline = [
        *session_state.last_request_input,
        *[
            sanitize_codex_input_item_for_replay(item)
            for item in session_state.last_response_output_items
        ],
    ]
    can_use_previous = bool(
        session_state.last_response_id
        and expected_previous_response_id == session_state.last_response_id
        and session_state.last_request_signature == request_signature
        and len(baseline) < len(full_input)
        and all(
            stable_stringify(item) == stable_stringify(full_input[index])
            for index, item in enumerate(baseline)
        )
    )
    if not can_use_previous:
        return PreparedCodexWebSocketRequest(
            request=base_request,
            full_input=full_input,
            request_signature=request_signature,
            used_previous_response_id=False,
        )
    return PreparedCodexWebSocketRequest(
        request={
            **base_request,
            "previous_response_id": session_state.last_response_id,
            "input": full_input[len(baseline) :],
        },
        full_input=full_input,
        request_signature=request_signature,
        used_previous_response_id=True,
    )


def _build_websocket_request_base(
    payload: dict[str, Any],
    input_items: list[dict[str, Any]],
) -> dict[str, Any]:
    request: dict[str, Any] = {
        "type": RESPONSE_CREATE_TYPE,
        "model": payload["model"],
        "input": input_items,
        "store": False,
        "stream": True,
    }
    for key in (
        "instructions",
        "tools",
        "tool_choice",
        "parallel_tool_calls",
        "reasoning",
        "include",
        "text",
    ):
        value = payload.get(key)
        if value is not None:
            request[key] = value
    return request


def finalize_codex_websocket_response(
    *,
    response_snapshot: dict[str, Any] | None,
    stream_output_items: dict[int, dict[str, Any]],
    function_call_args: dict[int, str],
    accumulated_text: str,
    last_event_type: str,
    last_event_preview: str,
) -> dict[str, Any]:
    if not response_snapshot:
        raise CodexWebSocketRequestError(
            "WebSocket stream ended without a response.completed event. "
            f"Last event: {last_event_type}. Payload: {last_event_preview}",
            retryable=True,
        )
    completed = dict(response_snapshot)
    merged: dict[int, dict[str, Any]] = {}
    for index, item in enumerate(completed.get("output") or []):
        if isinstance(item, dict):
            merged[index] = dict(item)
    for index, item in stream_output_items.items():
        merged[index] = dict(item)
    for index, item in merged.items():
        if item.get("type") == "function_call" and not item.get("arguments"):
            args = function_call_args.get(index)
            if args:
                item["arguments"] = args
    if merged:
        completed["output"] = [item for _, item in sorted(merged.items())]
    if accumulated_text and not completed.get("output_text"):
        completed["output_text"] = accumulated_text
    return completed


class CodexTransport:
    def __init__(
        self,
        auth_manager: CodexAuthManager,
        *,
        base_url: str = "https://chatgpt.com/backend-api/codex",
        idle_timeout: int = 3000,
        read_timeout: float = 120.0,
        verbose: bool = False,
    ) -> None:
        self._auth_manager = auth_manager
        self._websocket_url = _websocket_url(base_url)
        self._idle_timeout = idle_timeout
        self._read_timeout = read_timeout
        self._verbose = verbose
        self._sessions: dict[str, CodexSession] = {}

    async def send_request(
        self,
        session_key: str,
        payload: dict[str, Any],
        *,
        expected_previous_response_id: str | None = None,
    ) -> dict[str, Any]:
        session = self._get_session(session_key)
        async with session.lock:
            session.last_used_at = time.time()
            prepared = prepare_codex_websocket_request(
                payload,
                session.state,
                expected_previous_response_id=expected_previous_response_id,
            )
            full_request = _build_websocket_request_base(payload, prepared.full_input)
            committed = False
            try:
                try:
                    response = await self._send_once(session, prepared.request)
                except Exception as exc:
                    request_error = _request_error(exc)
                    if request_error.retryable and session.reconnect_attempts < 1:
                        session.reconnect_attempts += 1
                        try:
                            await self._close_session_socket(session, code=1012, reason="retry")
                            response = await self._send_once(session, full_request)
                        except Exception as retry_exc:
                            session.reconnect_attempts = 0
                            normalized = _request_error(retry_exc)
                            raise normalized from retry_exc
                    else:
                        session.reconnect_attempts = 0
                        raise
                self._commit_session_state(session, prepared, response)
                committed = True
                self._schedule_idle_eviction(session)
                return response
            finally:
                if not committed:
                    # A failed or cancelled request (CancelledError bypasses the
                    # except above) can leave the server still streaming on this
                    # socket; _send_once has no response-id correlation, so a
                    # reused socket could hand a stale response.completed frame
                    # to the next turn. Close it so the next request reconnects.
                    await self._close_session_socket(session, code=1011, reason="failed request")
                    self._schedule_idle_eviction(session)

    async def evict_session(self, session_key: str) -> None:
        session = self._sessions.pop(session_key, None)
        if session is None:
            return
        idle_task = session.idle_task
        if idle_task and idle_task is not asyncio.current_task():
            idle_task.cancel()
        if idle_task:
            session.idle_task = None
        await self._close_session_socket(session, code=1000, reason="idle eviction")
        if session.client_session and not session.client_session.closed:
            await session.client_session.close()
        session.client_session = None

    async def close_all(self) -> None:
        keys = list(self._sessions)
        for key in keys:
            await self.evict_session(key)

    def _get_session(self, session_key: str) -> CodexSession:
        session = self._sessions.get(session_key)
        if session is not None:
            self._schedule_idle_eviction(session)
            return session
        session = CodexSession(
            key=session_key,
            last_used_at=time.time(),
            connection_session_id=str(uuid.uuid4()),
        )
        self._sessions[session_key] = session
        self._schedule_idle_eviction(session)
        return session

    def _schedule_idle_eviction(self, session: CodexSession) -> None:
        if session.idle_task:
            session.idle_task.cancel()

        async def evict_after_idle() -> None:
            try:
                await asyncio.sleep(self._idle_timeout)
                current = self._sessions.get(session.key)
                if current is not session:
                    return
                if time.time() - session.last_used_at < self._idle_timeout:
                    self._schedule_idle_eviction(session)
                    return
                if session.lock.locked():
                    # An active request holds the lock; never tear the socket
                    # down under an in-flight ws.receive(). Re-arm and re-check
                    # after the next idle window. (.locked() is a non-blocking
                    # check, so there is no deadlock with the request.)
                    self._schedule_idle_eviction(session)
                    return
                await self.evict_session(session.key)
            except asyncio.CancelledError:
                return

        session.idle_task = asyncio.create_task(evict_after_idle())

    async def _connect(self, session: CodexSession) -> aiohttp.ClientWebSocketResponse:
        if session.ws is not None and not session.ws.closed:
            return session.ws
        if session.client_session is None or session.client_session.closed:
            # connect-only timeout: a total= timeout would bound the lifetime of
            # the long-lived websocket itself, not just the handshake.
            session.client_session = aiohttp.ClientSession(
                trust_env=False,
                timeout=aiohttp.ClientTimeout(connect=30),
            )
        for attempt in range(2):
            token = await self._auth_manager.get_access_token()
            account_id = self._auth_manager.get_account_id()
            session.connection_session_id = str(uuid.uuid4())
            headers = {
                "Authorization": f"Bearer {token}",
                "OpenAI-Beta": CODEX_WS_BETA_HEADER,
                "originator": CODEX_ORIGINATOR,
                "session_id": session.connection_session_id,
                "x-client-request-id": str(uuid.uuid4()),
            }
            if account_id:
                headers["ChatGPT-Account-Id"] = account_id
            try:
                session.ws = await session.client_session.ws_connect(
                    self._websocket_url,
                    headers=headers,
                    max_msg_size=CODEX_MAX_WS_MESSAGE_BYTES,
                )
                return session.ws
            except aiohttp.WSServerHandshakeError as exc:
                if exc.status != 401:
                    raise
                if attempt == 0:
                    # The access-token timestamp can still look valid after the
                    # authority has invalidated it. Reload a same-account token
                    # written by another process, or force an OAuth refresh.
                    await self._auth_manager.refresh_tokens(force=True)
                    continue
                raise CodexWebSocketRequestError(
                    "Codex WebSocket authentication failed after token refresh",
                    retryable=False,
                ) from exc
        raise AssertionError("unreachable")

    async def _send_once(self, session: CodexSession, request: dict[str, Any]) -> dict[str, Any]:
        ws = await self._connect(session)
        request_json = json.dumps(request)
        try:
            await ws.send_str(request_json)
        except (aiohttp.ClientError, OSError) as exc:
            raise CodexWebSocketRequestError(
                f"Codex WebSocket write failed: {type(exc).__name__}: {exc}",
                retryable=True,
            ) from exc
        response_snapshot: dict[str, Any] | None = None
        stream_output_items: dict[int, dict[str, Any]] = {}
        function_call_args: dict[int, str] = {}
        accumulated_text = ""
        last_event_type = "none"
        last_event_preview = ""
        while True:
            try:
                message = await asyncio.wait_for(ws.receive(), self._read_timeout)
            except TimeoutError as exc:
                raise CodexWebSocketRequestError(
                    f"Codex WebSocket read timed out after {self._read_timeout}s "
                    f"without a response.completed event. Last event: {last_event_type}.",
                    retryable=True,
                ) from exc
            if message.type == aiohttp.WSMsgType.TEXT:
                data = message.data
            elif message.type == aiohttp.WSMsgType.BINARY:
                data = message.data.decode("utf-8")
            elif message.type in {aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE}:
                raise CodexWebSocketRequestError(
                    "Codex WebSocket closed before response.completed",
                    retryable=True,
                )
            elif message.type == aiohttp.WSMsgType.ERROR:
                raise CodexWebSocketRequestError(
                    "Codex WebSocket errored before response.completed",
                    retryable=True,
                )
            else:
                continue
            # A real frame arrived: keep the idle stamp fresh so a long but
            # actively-streaming response is not considered idle.
            session.last_used_at = time.time()
            try:
                parsed = json.loads(data)
            except json.JSONDecodeError:
                # A malformed/non-JSON frame is unparseable, not a terminal
                # event; skip it (matching the tolerant handling of unknown
                # frame types) rather than failing the whole turn non-retryably.
                log.warning(
                    "Codex WebSocket frame was not valid JSON; skipping. Preview: %s",
                    data[:200],
                )
                continue
            if not isinstance(parsed, dict):
                continue
            last_event_type = str(parsed.get("type") or "unknown")
            if last_event_type == "codex.rate_limits":
                continue
            if last_event_type not in {
                "response.output_text.delta",
                "response.function_call_arguments.delta",
            }:
                last_event_preview = data[:500]
            if last_event_type == "error":
                status_code, code, retry_after_seconds = _provider_error_metadata(parsed)
                raise CodexWebSocketRequestError(
                    f"Codex provider error: {_provider_error_message(parsed)}",
                    retryable=_is_retryable_provider_error(parsed),
                    status_code=status_code,
                    code=code,
                    retry_after_seconds=retry_after_seconds,
                )
            if last_event_type == "response.failed":
                response_payload = parsed.get("response")
                metadata_payload = (
                    response_payload if isinstance(response_payload, dict) else parsed
                )
                error_payload = metadata_payload.get("error") or parsed.get("error") or {}
                status_code, code, retry_after_seconds = _provider_error_metadata(metadata_payload)
                raise CodexWebSocketRequestError(
                    f"Codex response failed: {_provider_error_message(error_payload)}",
                    retryable=_is_retryable_provider_error(error_payload),
                    status_code=status_code,
                    code=code,
                    retry_after_seconds=retry_after_seconds,
                )
            if last_event_type == "response.incomplete":
                response = parsed.get("response")
                details = response.get("incomplete_details") if isinstance(response, dict) else None
                reason = details.get("reason") if isinstance(details, dict) else None
                raise CodexWebSocketRequestError(
                    f"Incomplete response returned, reason: {reason or 'unknown'}",
                    retryable=True,
                )
            if last_event_type in {
                "response.created",
                "response.in_progress",
                "response.completed",
            }:
                response = parsed.get("response")
                if isinstance(response, dict):
                    response_snapshot = {**(response_snapshot or {}), **response}
            if last_event_type == "response.output_text.delta" and isinstance(
                parsed.get("delta"), str
            ):
                accumulated_text += parsed["delta"]
            if last_event_type in {"response.output_item.added", "response.output_item.done"}:
                item = parsed.get("item")
                if isinstance(item, dict):
                    output_index = parsed.get("output_index")
                    key = (
                        output_index if isinstance(output_index, int) else len(stream_output_items)
                    )
                    stream_output_items[key] = item
            if last_event_type == "response.function_call_arguments.delta" and isinstance(
                parsed.get("delta"), str
            ):
                output_index = parsed.get("output_index")
                index = output_index if isinstance(output_index, int) else 0
                function_call_args[index] = function_call_args.get(index, "") + parsed["delta"]
            if last_event_type == "response.completed":
                return finalize_codex_websocket_response(
                    response_snapshot=response_snapshot,
                    stream_output_items=stream_output_items,
                    function_call_args=function_call_args,
                    accumulated_text=accumulated_text,
                    last_event_type=last_event_type,
                    last_event_preview=last_event_preview,
                )

    async def _close_session_socket(
        self,
        session: CodexSession,
        *,
        code: int,
        reason: str,
    ) -> None:
        ws = session.ws
        session.ws = None
        if ws is None or ws.closed:
            return
        await ws.close(code=code, message=reason.encode("utf-8")[:123])

    def _commit_session_state(
        self,
        session: CodexSession,
        prepared: PreparedCodexWebSocketRequest,
        response: dict[str, Any],
    ) -> None:
        output = response.get("output") if isinstance(response, dict) else []
        output_items = [
            sanitize_codex_input_item_for_replay(item)
            for item in output or []
            if isinstance(item, dict)
        ]
        response_id = response.get("id") if isinstance(response, dict) else None
        session.state = CodexSessionState(
            last_request_input=prepared.full_input,
            last_request_signature=prepared.request_signature,
            last_response_id=response_id if isinstance(response_id, str) else None,
            last_response_output_items=output_items,
        )
        session.reconnect_attempts = 0
        if self._verbose:
            log.info(
                "Codex response completed: id=%s previous_response_id=%s",
                session.state.last_response_id,
                prepared.used_previous_response_id,
            )


def _websocket_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    http_url = base if base.endswith("/responses") else f"{base}/responses"
    if http_url.startswith("https://"):
        return "wss://" + http_url[len("https://") :]
    if http_url.startswith("http://"):
        return "ws://" + http_url[len("http://") :]
    return http_url


def _provider_error_message(payload: dict[str, Any]) -> str:
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        return error["message"]
    if isinstance(payload.get("message"), str):
        return payload["message"]
    return "unknown error"


def _provider_error_metadata(
    payload: dict[str, Any],
) -> tuple[int | None, str | None, float | None]:
    nested = payload.get("error")
    detail = nested if isinstance(nested, dict) else payload
    raw_status = (
        detail.get("status_code")
        or detail.get("status")
        or payload.get("status_code")
        or payload.get("status")
    )
    try:
        status_code = int(raw_status) if raw_status is not None else None
    except TypeError, ValueError:
        status_code = None
    raw_code = detail.get("code") or payload.get("code")
    code = str(raw_code) if raw_code is not None else None
    if status_code is None and code in {"rate_limit_exceeded", "rate_limit_error"}:
        status_code = 429

    retry_after_seconds: float | None = None
    headers = payload.get("headers")
    raw_retry_after = None
    if isinstance(headers, dict):
        raw_retry_after = next(
            (value for name, value in headers.items() if str(name).lower() == "retry-after"),
            None,
        )
    try:
        if raw_retry_after is not None:
            retry_after_seconds = float(raw_retry_after)
    except TypeError, ValueError:
        retry_after_seconds = None
    if retry_after_seconds is None and code == "rate_limit_exceeded":
        message = detail.get("message")
        match = _RETRY_AFTER_MESSAGE_RE.search(message) if isinstance(message, str) else None
        if match is not None:
            value = float(match.group(1))
            retry_after_seconds = value / 1000 if match.group(2).lower() == "ms" else value
    if retry_after_seconds is not None and retry_after_seconds < 0:
        retry_after_seconds = None
    return status_code, code, retry_after_seconds


def _is_retryable_provider_error(payload: dict[str, Any]) -> bool:
    status = payload.get("status") if isinstance(payload, dict) else None
    if status is None and isinstance(payload, dict):
        status = payload.get("status_code")
    error = payload.get("error") if isinstance(payload, dict) else None
    code = error.get("code") if isinstance(error, dict) else payload.get("code")
    message = _provider_error_message(payload)
    return (
        code in {"previous_response_not_found", "websocket_connection_limit_reached"}
        or (isinstance(status, int) and status >= 500)
        or "connection limit" in message.lower()
        or "too many connections" in message.lower()
    )


__all__ = [
    "CODEX_MAX_WS_MESSAGE_BYTES",
    "CodexSessionState",
    "CodexTransport",
    "CodexWebSocketRequestError",
    "PreparedCodexWebSocketRequest",
    "finalize_codex_websocket_response",
    "prepare_codex_websocket_request",
    "sanitize_codex_input_item_for_replay",
]
