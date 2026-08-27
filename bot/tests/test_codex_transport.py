import asyncio
import json
from typing import Any, cast

import aiohttp
import pytest

from codex.transport import (
    CODEX_MAX_WS_MESSAGE_BYTES,
    CodexSessionState,
    CodexTransport,
    CodexWebSocketRequestError,
    finalize_codex_websocket_response,
    prepare_codex_websocket_request,
    sanitize_codex_input_item_for_replay,
)
from providers import assets as asset_writer


class _WSMessage:
    def __init__(self, msg_type: Any, data: Any) -> None:
        self.type = msg_type
        self.data = data


class _Raw(str):
    """A pre-stringified frame emitted verbatim, not json.dumps-ed."""


class ScriptedWebSocket:
    """Replays a scripted list of JSON events as TEXT frames, then stays open."""

    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._events = list(events)
        self.sent: list[str] = []
        self.closed = False

    async def send_str(self, data: str) -> None:
        self.sent.append(data)

    async def receive(self) -> _WSMessage:
        if self._events:
            event = self._events.pop(0)
            data = event if isinstance(event, _Raw) else json.dumps(event)
            return _WSMessage(aiohttp.WSMsgType.TEXT, data)
        # No CLOSE/completed scripted: block so a missing read timeout would hang.
        await asyncio.sleep(60)
        return _WSMessage(aiohttp.WSMsgType.CLOSED, None)

    async def close(self, *, code: int, message: bytes) -> None:
        self.closed = True


class FailingSendWebSocket:
    """Looks open until the first write, matching a half-closed aiohttp socket."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed = False

    async def send_str(self, data: str) -> None:
        self.sent.append(data)
        raise aiohttp.ClientConnectionResetError("Cannot write to closing transport")

    async def receive(self) -> _WSMessage:  # pragma: no cover - send fails first
        raise AssertionError("receive should not be called after send failure")

    async def close(self, *, code: int, message: bytes) -> None:
        self.closed = True


class ScriptedTransport(CodexTransport):
    def __init__(self, events: list[dict[str, Any]], **kwargs: Any) -> None:
        super().__init__(auth_manager=cast(Any, object()), **kwargs)
        self.fake_ws = ScriptedWebSocket(events)

    async def _connect(self, session: Any) -> Any:
        session.ws = cast(Any, self.fake_ws)
        return self.fake_ws


class SocketSequenceTransport(CodexTransport):
    def __init__(self, sockets: list[Any], **kwargs: Any) -> None:
        super().__init__(auth_manager=cast(Any, object()), **kwargs)
        self._sockets = list(sockets)

    async def _connect(self, session: Any) -> Any:
        if session.ws is not None and not session.ws.closed:
            return session.ws
        if not self._sockets:
            raise AssertionError("no scripted websocket left")
        session.ws = cast(Any, self._sockets.pop(0))
        return session.ws


class RefreshingAuthManager:
    def __init__(self) -> None:
        self.access_token = "access-old"
        self.refresh_calls: list[bool] = []

    async def get_access_token(self) -> str:
        return self.access_token

    def get_account_id(self) -> str:
        return "acct"

    async def refresh_tokens(self, *, force: bool = False) -> None:
        self.refresh_calls.append(force)
        self.access_token = "access-new"


class HandshakeClientSession:
    def __init__(self, outcomes: list[Any]) -> None:
        self.closed = False
        self._outcomes = list(outcomes)
        self.calls: list[dict[str, str]] = []
        self.max_msg_sizes: list[int] = []

    async def ws_connect(
        self,
        _url: str,
        *,
        headers: dict[str, str],
        max_msg_size: int,
    ) -> Any:
        self.calls.append(dict(headers))
        self.max_msg_sizes.append(max_msg_size)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def close(self) -> None:
        self.closed = True


def _unauthorized_handshake() -> aiohttp.WSServerHandshakeError:
    return aiohttp.WSServerHandshakeError(
        cast(Any, None),
        (),
        status=401,
        message="Unauthorized",
        headers=None,
    )


def _basic_payload() -> dict[str, Any]:
    return {"model": "gpt-5.5", "input": [{"role": "user", "content": "hi"}]}


@pytest.mark.asyncio
async def test_codex_connect_refreshes_token_after_unauthorized_handshake() -> None:
    auth = RefreshingAuthManager()
    websocket = ScriptedWebSocket([])
    client = HandshakeClientSession([_unauthorized_handshake(), websocket])
    transport = CodexTransport(cast(Any, auth), idle_timeout=3000)
    session = transport._get_session("session-1")
    session.client_session = cast(Any, client)
    try:
        connected = await transport._connect(session)
    finally:
        await transport.close_all()

    assert connected is websocket
    assert auth.refresh_calls == [True]
    assert [call["Authorization"] for call in client.calls] == [
        "Bearer access-old",
        "Bearer access-new",
    ]


@pytest.mark.asyncio
async def test_codex_connect_stops_after_refreshed_token_is_unauthorized() -> None:
    auth = RefreshingAuthManager()
    client = HandshakeClientSession([_unauthorized_handshake(), _unauthorized_handshake()])
    transport = CodexTransport(cast(Any, auth), idle_timeout=3000)
    session = transport._get_session("session-1")
    session.client_session = cast(Any, client)
    try:
        with pytest.raises(CodexWebSocketRequestError) as exc_info:
            await transport._connect(session)
    finally:
        await transport.close_all()

    assert exc_info.value.retryable is False
    assert auth.refresh_calls == [True]
    assert len(client.calls) == 2


@pytest.mark.asyncio
async def test_codex_connect_allows_bounded_generated_image_frames() -> None:
    auth = RefreshingAuthManager()
    websocket = ScriptedWebSocket([])
    client = HandshakeClientSession([websocket])
    transport = CodexTransport(cast(Any, auth), idle_timeout=3000)
    session = transport._get_session("session-1")
    session.client_session = cast(Any, client)
    try:
        assert await transport._connect(session) is websocket
    finally:
        await transport.close_all()

    encoded_aggregate_cap = ((asset_writer._MAX_TOTAL_GENERATED_ASSET_BYTES + 2) // 3) * 4
    assert encoded_aggregate_cap + 2 * 1024 * 1024 == CODEX_MAX_WS_MESSAGE_BYTES
    assert CODEX_MAX_WS_MESSAGE_BYTES < 40 * 1024 * 1024
    assert client.max_msg_sizes == [CODEX_MAX_WS_MESSAGE_BYTES]


@pytest.mark.asyncio
async def test_codex_stream_does_not_duplicate_first_output_item() -> None:
    events: list[dict[str, Any]] = [
        {"type": "response.created", "response": {"id": "resp_1", "status": "in_progress"}},
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {"type": "message", "role": "assistant", "content": []},
        },
        {"type": "response.output_text.delta", "delta": "hi"},
        {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "hi"}],
            },
        },
        {
            "type": "response.completed",
            "response": {
                "id": "resp_1",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "hi"}],
                    }
                ],
            },
        },
    ]
    transport = ScriptedTransport(events, idle_timeout=3000)
    try:
        response = await transport.send_request("conv-1", _basic_payload())
    finally:
        await transport.close_all()

    assert [item["type"] for item in response["output"]] == ["message"]
    assert response["output"][0]["content"][0]["text"] == "hi"


@pytest.mark.asyncio
async def test_codex_stream_fills_function_call_arguments_at_index_zero() -> None:
    events: list[dict[str, Any]] = [
        {"type": "response.created", "response": {"id": "resp_1", "status": "in_progress"}},
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {"type": "function_call", "call_id": "call_1", "name": "lookup"},
        },
        {"type": "response.function_call_arguments.delta", "output_index": 0, "delta": '{"q":'},
        {"type": "response.function_call_arguments.delta", "output_index": 0, "delta": '"vr"}'},
        {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": {
                "type": "function_call",
                "call_id": "call_1",
                "name": "lookup",
                "arguments": "",
            },
        },
        {
            "type": "response.completed",
            "response": {
                "id": "resp_1",
                "status": "completed",
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call_1",
                        "name": "lookup",
                        "arguments": "",
                    }
                ],
            },
        },
    ]
    transport = ScriptedTransport(events, idle_timeout=3000)
    try:
        response = await transport.send_request("conv-1", _basic_payload())
    finally:
        await transport.close_all()

    function_calls = [item for item in response["output"] if item["type"] == "function_call"]
    assert len(function_calls) == 1
    assert function_calls[0]["arguments"] == '{"q":"vr"}'


@pytest.mark.asyncio
async def test_codex_stream_ignores_stale_in_progress_partial_output() -> None:
    events: list[dict[str, Any]] = [
        {
            "type": "response.in_progress",
            "response": {
                "id": "resp_1",
                "status": "in_progress",
                "output": [{"type": "reasoning", "id": "rs_1", "summary": []}],
            },
        },
        {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "final"}],
            },
        },
        {
            "type": "response.completed",
            "response": {"id": "resp_1", "status": "completed"},
        },
    ]
    transport = ScriptedTransport(events, idle_timeout=3000)
    try:
        response = await transport.send_request("conv-1", _basic_payload())
    finally:
        await transport.close_all()

    assert [item["type"] for item in response["output"]] == ["message"]
    assert response["output"][0]["content"][0]["text"] == "final"


@pytest.mark.asyncio
async def test_codex_send_once_times_out_when_stream_stalls() -> None:
    # Stream never sends response.completed or a close frame.
    events: list[dict[str, Any]] = [
        {"type": "response.created", "response": {"id": "resp_1", "status": "in_progress"}}
    ]
    transport = ScriptedTransport(events, idle_timeout=3000, read_timeout=0.05)
    try:
        with pytest.raises(CodexWebSocketRequestError, match="timed out"):
            await transport.send_request("conv-1", _basic_payload())
    finally:
        await transport.close_all()


@pytest.mark.asyncio
async def test_codex_incomplete_is_retried_as_stream_failure_without_committing_state() -> None:
    events: list[dict[str, Any]] = [
        {
            "type": "response.output_text.delta",
            "delta": "partial answer",
        },
        {
            "type": "response.incomplete",
            "response": {
                "id": "resp_1",
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
            },
        },
    ]
    first = ScriptedWebSocket(events)
    second = ScriptedWebSocket(events)
    transport = SocketSequenceTransport([first, second], idle_timeout=3000)
    try:
        with pytest.raises(CodexWebSocketRequestError, match="max_output_tokens") as exc_info:
            await transport.send_request("conv-1", _basic_payload())
        state = transport._sessions["conv-1"].state
    finally:
        await transport.close_all()

    assert exc_info.value.retryable is True
    assert state.last_response_id is None
    assert len(first.sent) == 1
    assert len(second.sent) == 1


@pytest.mark.asyncio
async def test_codex_wrapped_error_uses_official_status_and_header_shape() -> None:
    transport = ScriptedTransport(
        [
            {
                "type": "error",
                "status": 429,
                "error": {
                    "type": "usage_limit_reached",
                    "message": "The usage limit has been reached",
                    "plan_type": "pro",
                    "resets_at": 1738888888,
                },
                "headers": {
                    "x-codex-primary-used-percent": "100.0",
                    "x-codex-primary-window-minutes": 15,
                },
            }
        ],
        idle_timeout=3000,
    )
    try:
        with pytest.raises(CodexWebSocketRequestError) as exc_info:
            await transport.send_request("conv-1", _basic_payload())
    finally:
        await transport.close_all()

    assert exc_info.value.status_code == 429
    assert exc_info.value.code is None
    assert exc_info.value.retry_after_seconds is None
    assert exc_info.value.retryable is False
    assert len(transport.fake_ws.sent) == 1


@pytest.mark.asyncio
async def test_codex_response_failed_parses_official_rate_limit_message() -> None:
    transport = ScriptedTransport(
        [
            {
                "type": "response.failed",
                "response": {
                    "status": "failed",
                    "error": {
                        "code": "rate_limit_exceeded",
                        "message": "Rate limit reached. Please try again in 11.054s.",
                    },
                },
            }
        ],
        idle_timeout=3000,
    )
    try:
        with pytest.raises(CodexWebSocketRequestError) as exc_info:
            await transport.send_request("conv-1", _basic_payload())
    finally:
        await transport.close_all()

    assert exc_info.value.status_code == 429
    assert exc_info.value.code == "rate_limit_exceeded"
    assert exc_info.value.retry_after_seconds == 11.054
    assert exc_info.value.retryable is False


@pytest.mark.asyncio
async def test_codex_previous_response_not_found_retries_full_request() -> None:
    rejected = ScriptedWebSocket(
        [
            {
                "type": "error",
                "status": 400,
                "error": {"code": "previous_response_not_found"},
            }
        ]
    )
    completed = ScriptedWebSocket(
        [
            {
                "type": "response.completed",
                "response": {"id": "resp_retry", "status": "completed", "output": []},
            }
        ]
    )
    transport = SocketSequenceTransport([rejected, completed], idle_timeout=3000)
    payload = {
        "model": "gpt-5.5",
        "input": [
            {"role": "user", "content": "earlier"},
            {"role": "user", "content": "next"},
        ],
    }
    session = transport._get_session("conv-1")
    session.state = CodexSessionState(
        last_request_input=[{"role": "user", "content": "earlier"}],
        last_request_signature=prepare_codex_websocket_request(
            payload,
            CodexSessionState(),
        ).request_signature,
        last_response_id="resp_previous",
    )
    try:
        response = await transport.send_request(
            "conv-1",
            payload,
            expected_previous_response_id="resp_previous",
        )
    finally:
        await transport.close_all()

    assert response["id"] == "resp_retry"
    assert rejected.closed is True
    assert len(rejected.sent) == 1
    assert len(completed.sent) == 1
    incremental = json.loads(rejected.sent[0])
    assert incremental["previous_response_id"] == "resp_previous"
    assert incremental["input"] == [{"role": "user", "content": "next"}]
    full_retry = json.loads(completed.sent[0])
    assert "previous_response_id" not in full_retry
    assert full_retry["input"] == payload["input"]


@pytest.mark.asyncio
async def test_codex_transport_retries_when_socket_write_fails() -> None:
    failing = FailingSendWebSocket()
    succeeding = ScriptedWebSocket(
        [
            {
                "type": "response.completed",
                "response": {"id": "resp_retry", "status": "completed", "output": []},
            }
        ]
    )
    transport = SocketSequenceTransport([failing, succeeding], idle_timeout=3000)
    try:
        response = await transport.send_request("conv-1", _basic_payload())
    finally:
        await transport.close_all()

    assert response["id"] == "resp_retry"
    assert failing.closed is True
    assert len(failing.sent) == 1
    assert len(succeeding.sent) == 1
    assert json.loads(succeeding.sent[0])["input"] == [{"role": "user", "content": "hi"}]


class AlwaysRetryFailingTransport(CodexTransport):
    def __init__(self) -> None:
        super().__init__(auth_manager=cast(Any, object()), idle_timeout=3000)
        self.sent_requests: list[dict[str, Any]] = []

    async def _send_once(self, session: Any, request: dict[str, Any]) -> dict[str, Any]:
        self.sent_requests.append(request)
        raise CodexWebSocketRequestError("temporary failure", retryable=True)

    async def _close_session_socket(
        self,
        session: Any,
        *,
        code: int,
        reason: str,
    ) -> None:
        return None


class ConnectFailingOnceTransport(CodexTransport):
    def __init__(self) -> None:
        super().__init__(auth_manager=cast(Any, object()), idle_timeout=3000)
        self.attempts = 0

    async def _send_once(self, session: Any, request: dict[str, Any]) -> dict[str, Any]:
        self.attempts += 1
        if self.attempts == 1:
            raise aiohttp.ClientConnectionError("connect failed")
        return {"id": "resp_1", "status": "completed", "output": []}

    async def _close_session_socket(
        self,
        session: Any,
        *,
        code: int,
        reason: str,
    ) -> None:
        return None


class ConnectAlwaysFailingTransport(ConnectFailingOnceTransport):
    async def _send_once(self, session: Any, request: dict[str, Any]) -> dict[str, Any]:
        self.attempts += 1
        raise aiohttp.ClientConnectionError("connect failed")


class SlowClosingWebSocket:
    closed = False

    async def close(self, *, code: int, message: bytes) -> None:
        await asyncio.sleep(0)
        self.closed = True


class SlowClosingClientSession:
    closed = False

    async def close(self) -> None:
        await asyncio.sleep(0)
        self.closed = True


def test_codex_prepare_request_reuses_previous_response_for_strict_extension() -> None:
    initial_input = [{"role": "user", "content": "hello"}]
    assistant_output = {
        "type": "message",
        "id": "msg_1",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "hi"}],
    }
    payload = {
        "model": "gpt-5.5",
        "instructions": "base",
        "input": [*initial_input, assistant_output, {"role": "user", "content": "next"}],
        "store": False,
        "stream": True,
    }
    state = CodexSessionState(
        last_request_input=initial_input,
        last_request_signature=prepare_codex_websocket_request(
            {
                "model": "gpt-5.5",
                "instructions": "base",
                "input": initial_input,
                "store": False,
                "stream": True,
            },
            CodexSessionState(),
        ).request_signature,
        last_response_id="resp_1",
        last_response_output_items=[assistant_output],
    )

    prepared = prepare_codex_websocket_request(
        payload,
        state,
        expected_previous_response_id="resp_1",
    )

    assert prepared.used_previous_response_id is True
    assert prepared.request["previous_response_id"] == "resp_1"
    assert prepared.request["input"] == [{"role": "user", "content": "next"}]


def test_codex_prepare_request_uses_full_input_when_reasoning_changes() -> None:
    initial_input = [{"role": "user", "content": "inspect the project"}]
    assistant_output = {
        "type": "function_call",
        "call_id": "call_1",
        "name": "read_file",
        "arguments": '{"path":"app.py"}',
    }
    initial_payload = {
        "model": "gpt-5.6-sol",
        "input": initial_input,
        "reasoning": {"effort": "low"},
    }
    state = CodexSessionState(
        last_request_input=initial_input,
        last_request_signature=prepare_codex_websocket_request(
            initial_payload,
            CodexSessionState(),
        ).request_signature,
        last_response_id="resp_1",
        last_response_output_items=[assistant_output],
    )
    payload = {
        "model": "gpt-5.6-sol",
        "input": [
            *initial_input,
            assistant_output,
            {
                "type": "function_call_output",
                "call_id": "call_1",
                "output": "file contents",
            },
        ],
        "reasoning": {"effort": "high"},
    }

    prepared = prepare_codex_websocket_request(
        payload,
        state,
        expected_previous_response_id="resp_1",
    )

    assert prepared.used_previous_response_id is False
    assert "previous_response_id" not in prepared.request
    assert prepared.request["input"] == payload["input"]


def test_codex_prepare_request_falls_back_when_expected_response_mismatches() -> None:
    state = CodexSessionState(
        last_request_input=[{"role": "user", "content": "hello"}],
        last_request_signature="stale",
        last_response_id="resp_1",
        last_response_output_items=[],
    )
    payload = {
        "model": "gpt-5.5",
        "input": [{"role": "user", "content": "hello"}],
        "store": False,
        "stream": True,
    }

    prepared = prepare_codex_websocket_request(
        payload,
        state,
        expected_previous_response_id="resp_other",
    )

    assert prepared.used_previous_response_id is False
    assert "previous_response_id" not in prepared.request
    assert prepared.request["input"] == payload["input"]


def test_codex_sanitize_strips_web_search_action_payloads() -> None:
    assert sanitize_codex_input_item_for_replay(
        {
            "type": "web_search_call",
            "id": "ws_1",
            "status": "completed",
            "action": {"type": "search", "query": "vr"},
            "result": [],
        }
    ) == {
        "type": "web_search_call",
        "id": "ws_1",
        "status": "completed",
        "result": [],
    }


def test_codex_prepare_request_reuses_previous_response_after_image_generation() -> None:
    initial_input = [{"role": "user", "content": "draw a moon base"}]
    stored_image_output = {
        "type": "image_generation_call",
        "id": "img_1",
        "status": "completed",
        "result": "BIGBASE64DATA",
        "revised_prompt": "moon base",
    }
    replayed_image_output = {
        "type": "image_generation_call",
        "id": "img_1",
        "status": "completed",
        "revised_prompt": "moon base",
    }
    state = CodexSessionState(
        last_request_input=initial_input,
        last_request_signature=prepare_codex_websocket_request(
            {
                "model": "gpt-5.5",
                "input": initial_input,
                "store": False,
                "stream": True,
            },
            CodexSessionState(),
        ).request_signature,
        last_response_id="resp_1",
        last_response_output_items=[stored_image_output],
    )
    payload = {
        "model": "gpt-5.5",
        "input": [*initial_input, replayed_image_output, {"role": "user", "content": "modify it"}],
        "store": False,
        "stream": True,
    }

    prepared = prepare_codex_websocket_request(
        payload,
        state,
        expected_previous_response_id="resp_1",
    )

    assert prepared.used_previous_response_id is True
    assert prepared.request["previous_response_id"] == "resp_1"
    assert prepared.request["input"] == [{"role": "user", "content": "modify it"}]


def test_codex_finalize_merges_stream_items_and_function_arguments() -> None:
    response = finalize_codex_websocket_response(
        response_snapshot={"id": "resp_1", "status": "completed", "output": []},
        stream_output_items={
            0: {"type": "function_call", "call_id": "call_1", "name": "lookup"},
            1: {"type": "message", "content": [{"type": "output_text", "text": ""}]},
            2: {
                "type": "image_generation_call",
                "id": "img_1",
                "status": "completed",
                "result": "iVBORw0K",
            },
        },
        function_call_args={0: json.dumps({"q": "vr"})},
        accumulated_text="done",
        last_event_type="response.completed",
        last_event_preview="{}",
    )

    assert response["output_text"] == "done"
    assert response["output"][0]["arguments"] == '{"q": "vr"}'
    assert response["output"][2]["type"] == "image_generation_call"


def test_codex_finalize_requires_completed_response_snapshot() -> None:
    with pytest.raises(CodexWebSocketRequestError, match="without a response.completed"):
        finalize_codex_websocket_response(
            response_snapshot=None,
            stream_output_items={},
            function_call_args={},
            accumulated_text="",
            last_event_type="response.output_text.delta",
            last_event_preview="{}",
        )


@pytest.mark.asyncio
async def test_codex_transport_resets_retry_budget_when_full_retry_fails() -> None:
    transport = AlwaysRetryFailingTransport()
    try:
        with pytest.raises(CodexWebSocketRequestError, match="temporary failure"):
            await transport.send_request(
                "session-1",
                {"model": "gpt-5.5", "input": [{"role": "user", "content": "hi"}]},
            )

        assert len(transport.sent_requests) == 2
        assert transport._sessions["session-1"].reconnect_attempts == 0
    finally:
        await transport.close_all()


@pytest.mark.asyncio
async def test_codex_transport_retries_raw_connect_failure() -> None:
    transport = ConnectFailingOnceTransport()
    try:
        response = await transport.send_request("session-1", _basic_payload())
    finally:
        await transport.close_all()

    assert response["id"] == "resp_1"
    assert transport.attempts == 2


@pytest.mark.asyncio
async def test_codex_transport_normalizes_second_raw_connect_failure() -> None:
    transport = ConnectAlwaysFailingTransport()
    try:
        with pytest.raises(CodexWebSocketRequestError) as exc_info:
            await transport.send_request("session-1", _basic_payload())
    finally:
        await transport.close_all()

    assert exc_info.value.retryable is True
    assert transport.attempts == 2


@pytest.mark.asyncio
async def test_failed_request_rearms_idle_eviction() -> None:
    transport = AlwaysRetryFailingTransport()
    transport._idle_timeout = 0

    with pytest.raises(CodexWebSocketRequestError):
        await transport.send_request("session-1", _basic_payload())

    await asyncio.sleep(0.01)
    assert "session-1" not in transport._sessions


@pytest.mark.asyncio
async def test_codex_transport_idle_eviction_does_not_cancel_its_own_cleanup() -> None:
    transport = CodexTransport(auth_manager=cast(Any, object()), idle_timeout=0)
    session = transport._get_session("session-1")
    ws = SlowClosingWebSocket()
    client_session = SlowClosingClientSession()
    session.ws = cast(Any, ws)
    session.client_session = cast(Any, client_session)

    await asyncio.sleep(0.01)

    assert "session-1" not in transport._sessions
    assert ws.closed
    assert client_session.closed


@pytest.mark.asyncio
async def test_codex_stream_skips_malformed_frame_and_completes() -> None:
    events: list[Any] = [
        {"type": "response.created", "response": {"id": "resp_1", "status": "in_progress"}},
        _Raw("not-json{{{"),  # malformed TEXT frame interleaved
        {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "ok"}],
            },
        },
        {"type": "response.completed", "response": {"id": "resp_1", "status": "completed"}},
    ]
    transport = ScriptedTransport(events, idle_timeout=3000)
    try:
        # Must NOT raise a non-retryable CodexWebSocketRequestError on the bad frame.
        response = await transport.send_request("conv-1", _basic_payload())
    finally:
        await transport.close_all()

    assert response["output"][0]["content"][0]["text"] == "ok"


@pytest.mark.asyncio
async def test_idle_eviction_skipped_while_request_holds_lock() -> None:
    transport = CodexTransport(auth_manager=cast(Any, object()), idle_timeout=0)
    session = transport._get_session("session-1")
    ws = SlowClosingWebSocket()
    session.ws = cast(Any, ws)
    session.client_session = cast(Any, SlowClosingClientSession())

    await session.lock.acquire()
    try:
        await asyncio.sleep(0.02)
        # An active request holds the lock: eviction re-arms instead of tearing
        # the socket down under the in-flight request.
        assert "session-1" in transport._sessions
        assert ws.closed is False
    finally:
        session.lock.release()

    await asyncio.sleep(0.02)
    # Once released, the re-armed timer evicts normally.
    assert "session-1" not in transport._sessions
    assert ws.closed
