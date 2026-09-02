from __future__ import annotations

import json
from typing import Any

import pytest

from video_understanding import client as video_client
from video_understanding.client import (
    GeminiVideoClient,
    VideoInteractionError,
    VideoUploadRequest,
    _parse_interaction,
)


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(video_client, "_RETRY_BASE_DELAY_SECONDS", 0.0)


class _Content:
    def __init__(self, response: _Response, body: bytes) -> None:
        self.response = response
        self.body = body

    async def iter_chunked(self, size: int):
        self.response.read_called = True
        for offset in range(0, len(self.body), size):
            chunk = self.body[offset : offset + size]
            self.response.bytes_read += len(chunk)
            yield chunk


class _Response:
    def __init__(
        self,
        status: int,
        payload: object | None = None,
        *,
        json_error: Exception | None = None,
        raw_body: bytes | None = None,
        headers: dict[str, str] | None = None,
        enter_error: Exception | None = None,
    ) -> None:
        self.status = status
        self.payload = payload
        self.json_error = json_error
        self.headers = headers or {}
        self.enter_error = enter_error
        self.json_called = False
        self.read_called = False
        self.bytes_read = 0
        self.closed = False
        if raw_body is not None:
            body = raw_body
        elif payload is None:
            body = b"provider body" if json_error is not None else b""
        else:
            body = json.dumps(payload).encode()
        self.content = _Content(self, body)

    async def __aenter__(self) -> _Response:
        if self.enter_error is not None:
            raise self.enter_error
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def json(self, *, content_type: object = None) -> object:
        self.json_called = True
        if self.json_error is not None:
            raise self.json_error
        return self.payload

    async def read(self) -> bytes:
        self.read_called = True
        return self.content.body

    def close(self) -> None:
        self.closed = True


class _Session:
    closed = False

    def __init__(
        self,
        responses: list[_Response],
        *,
        get_responses: list[_Response] | None = None,
    ) -> None:
        self.responses = responses
        self.get_responses = get_responses if get_responses is not None else []
        self.posts: list[dict[str, Any]] = []
        self.deletes: list[str] = []
        self.gets: list[dict[str, Any]] = []

    def post(self, url: str, **kwargs: Any) -> _Response:
        self.posts.append({"url": url, **kwargs})
        if len(self.responses) == 1:
            return self.responses[0]
        return self.responses.pop(0)

    def get(self, url: str, **kwargs: Any) -> _Response:
        self.gets.append({"url": url, **kwargs})
        return self.get_responses.pop(0)

    def delete(self, url: str, **kwargs: Any) -> _Response:
        self.deletes.append(url)
        return self.responses.pop(0)


def _file_response(
    file_id: str,
    *,
    size: int,
    state: str = "ACTIVE",
    duration: str | None = "10s",
) -> dict[str, object]:
    file: dict[str, object] = {
        "name": f"files/{file_id}",
        "displayName": "clip.mp4",
        "mimeType": "video/mp4",
        "sizeBytes": str(size),
        "uri": f"https://generativelanguage.googleapis.com/v1beta/files/{file_id}",
        "state": state,
    }
    if duration is not None:
        file["videoMetadata"] = {"videoDuration": duration}
    return {"file": file}


def _response(interaction_id: str) -> dict[str, object]:
    return {
        "id": interaction_id,
        "model": "gemini-3.7-flash",
        "status": "completed",
        "steps": [
            {
                "type": "model_output",
                "content": [
                    {
                        "type": "text",
                        "text": '{"answer":"ok","evidence":[],"limitations":[]}',
                    }
                ],
            }
        ],
        "usage": {"total_input_tokens": 10, "total_output_tokens": 2},
    }


@pytest.mark.asyncio
async def test_http_payload_starts_and_continues_stored_interaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = GeminiVideoClient("secret")
    session = _Session([_Response(200, _response("one")), _Response(200, _response("two"))])

    async def get_session() -> Any:
        return session

    monkeypatch.setattr(client, "_get_session", get_session)
    first = await client.start(
        url="https://www.youtube.com/watch?v=abcdefghijk",
        question="Question",
        model="gemini-3.7-flash",
        thinking_level="low",
        max_output_tokens=1024,
    )
    second = await client.ask(
        previous_interaction_id=first.interaction_id,
        question="Follow up",
        model="gemini-3.7-flash",
        thinking_level="low",
        max_output_tokens=512,
    )

    assert second.interaction_id == "two"
    first_payload = session.posts[0]["json"]
    second_payload = session.posts[1]["json"]
    assert first_payload["store"] is True
    assert first_payload["input"][0] == {
        "type": "video",
        "uri": "https://www.youtube.com/watch?v=abcdefghijk",
    }
    assert "previous_interaction_id" not in first_payload
    assert second_payload["previous_interaction_id"] == "one"
    assert second_payload["input"] == "Follow up"
    assert second_payload["system_instruction"] == first_payload["system_instruction"]
    await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "message"),
    [(429, "busy"), (503, "temporarily unavailable")],
)
async def test_non_json_http_error_keeps_status_specific_message(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    message: str,
) -> None:
    response = _Response(status, json_error=ValueError("not json"))
    session = _Session([response])
    client = GeminiVideoClient("secret")

    async def get_session() -> Any:
        return session

    monkeypatch.setattr(client, "_get_session", get_session)
    with pytest.raises(VideoInteractionError, match=message):
        await client.start(
            url="https://www.youtube.com/watch?v=abcdefghijk",
            question="Question",
            model="gemini-3.7-flash",
            thinking_level="low",
            max_output_tokens=1024,
        )

    assert response.read_called is True
    assert response.json_called is False
    await client.close()


@pytest.mark.asyncio
async def test_interaction_json_response_is_byte_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _Response(200, raw_body=b"{" + b"x" * 128)
    session = _Session([response])
    client = GeminiVideoClient("secret")
    monkeypatch.setattr(video_client, "_MAX_JSON_RESPONSE_BYTES", 32)

    async def get_session() -> Any:
        return session

    monkeypatch.setattr(client, "_get_session", get_session)
    with pytest.raises(VideoInteractionError, match="invalid response"):
        await client.start(
            url="https://www.youtube.com/watch?v=abcdefghijk",
            question="Question",
            model="gemini-3.7-flash",
            thinking_level="low",
            max_output_tokens=1024,
        )

    assert response.bytes_read <= 33
    await client.close()


@pytest.mark.asyncio
async def test_interaction_json_response_is_depth_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nested: object = "leaf"
    for _ in range(5):
        nested = [nested]
    response = _Response(200, raw_body=json.dumps({"nested": nested}).encode())
    session = _Session([response])
    client = GeminiVideoClient("secret")
    monkeypatch.setattr(video_client, "_MAX_JSON_DEPTH", 3)

    async def get_session() -> Any:
        return session

    monkeypatch.setattr(client, "_get_session", get_session)
    with pytest.raises(VideoInteractionError, match="invalid response"):
        await client.start(
            url="https://www.youtube.com/watch?v=abcdefghijk",
            question="Question",
            model="gemini-3.7-flash",
            thinking_level="low",
            max_output_tokens=1024,
        )

    await client.close()


@pytest.mark.asyncio
async def test_oversized_video_error_body_is_not_fully_buffered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = [_Response(503, raw_body=b"x" * 128) for _ in range(3)]
    session = _Session(list(responses))
    client = GeminiVideoClient("secret")
    monkeypatch.setattr(video_client, "_MAX_ERROR_RESPONSE_BYTES", 16)

    async def get_session() -> Any:
        return session

    monkeypatch.setattr(client, "_get_session", get_session)
    with pytest.raises(VideoInteractionError, match="temporarily unavailable"):
        await client.start(
            url="https://www.youtube.com/watch?v=abcdefghijk",
            question="Question",
            model="gemini-3.7-flash",
            thinking_level="low",
            max_output_tokens=1024,
        )

    for response in responses:
        assert response.bytes_read <= 17
        assert response.closed is True
    await client.close()


@pytest.mark.asyncio
async def test_delete_treats_provider_404_as_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session([_Response(404)])
    client = GeminiVideoClient("secret", max_concurrency=1)

    async def get_session() -> Any:
        return session

    monkeypatch.setattr(client, "_get_session", get_session)
    await client._analysis_semaphore.acquire()
    try:
        await client.delete("interaction/one")
    finally:
        client._analysis_semaphore.release()

    assert session.deletes[0].endswith("/interaction%2Fone")
    await client.close()


@pytest.mark.asyncio
async def test_analysis_queue_wait_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    client = GeminiVideoClient("secret", max_concurrency=1)
    await client._analysis_semaphore.acquire()
    monkeypatch.setattr(video_client, "_QUEUE_TIMEOUT_SECONDS", 0.001)
    try:
        with pytest.raises(VideoInteractionError, match="busy"):
            await client.start(
                url="https://www.youtube.com/watch?v=abcdefghijk",
                question="Question",
                model="gemini-3.7-flash",
                thinking_level="low",
                max_output_tokens=1024,
            )
    finally:
        client._analysis_semaphore.release()
        await client.close()


@pytest.mark.asyncio
async def test_resumable_upload_streams_chunks_and_finalizes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def source() -> Any:
        yield b"abcdef"
        yield b"gh"

    monkeypatch.setattr(video_client, "_UPLOAD_CHUNK_SIZE_BYTES", 3)
    upload_url = "https://generativelanguage.googleapis.com/upload/v1beta/files/session"
    session = _Session(
        [
            _Response(200, headers={"X-Goog-Upload-URL": upload_url}),
            _Response(200),
            _Response(200),
            _Response(200, _file_response("kv-test", size=8)),
        ]
    )
    client = GeminiVideoClient("secret")

    async def get_session() -> Any:
        return session

    monkeypatch.setattr(client, "_get_session", get_session)
    uploaded = await client.upload_video(
        VideoUploadRequest(
            file_id="kv-test",
            display_name="clip.mp4",
            mime_type="video/mp4",
            declared_size_bytes=8,
            source=source(),
        )
    )

    assert uploaded.duration_seconds == 10
    chunk_calls = session.posts[1:]
    assert [call["headers"]["X-Goog-Upload-Offset"] for call in chunk_calls] == ["0", "3", "6"]
    assert [call["data"] for call in chunk_calls] == [b"abc", b"def", b"gh"]
    assert chunk_calls[-1]["headers"]["X-Goog-Upload-Command"] == "upload, finalize"
    assert session.posts[0]["json"]["file"]["name"] == "files/kv-test"
    await client.close()


@pytest.mark.asyncio
async def test_ambiguous_final_chunk_queries_offset_before_empty_finalize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def source() -> Any:
        yield b"abc"

    monkeypatch.setattr(video_client, "_UPLOAD_CHUNK_SIZE_BYTES", 3)
    upload_url = "https://generativelanguage.googleapis.com/upload/v1beta/files/session"
    session = _Session(
        [
            _Response(200, headers={"X-Goog-Upload-URL": upload_url}),
            _Response(503),
            _Response(
                200,
                headers={
                    "X-Goog-Upload-Status": "active",
                    "X-Goog-Upload-Size-Received": "3",
                },
            ),
            _Response(200, _file_response("kv-test", size=3)),
        ]
    )
    client = GeminiVideoClient("secret")

    async def get_session() -> Any:
        return session

    monkeypatch.setattr(client, "_get_session", get_session)
    uploaded = await client.upload_video(
        VideoUploadRequest("kv-test", "clip.mp4", "video/mp4", 3, source())
    )

    assert uploaded.state == "ACTIVE"
    assert session.posts[2]["headers"]["X-Goog-Upload-Command"] == "query"
    assert session.posts[3]["data"] == b""
    assert session.posts[3]["headers"]["X-Goog-Upload-Offset"] == "3"
    await client.close()


@pytest.mark.asyncio
async def test_source_read_failure_is_sanitized(monkeypatch: pytest.MonkeyPatch) -> None:
    async def source() -> Any:
        raise ValueError("private path detail")
        yield b""  # pragma: no cover

    upload_url = "https://generativelanguage.googleapis.com/upload/v1beta/files/session"
    session = _Session([_Response(200, headers={"X-Goog-Upload-URL": upload_url})])
    client = GeminiVideoClient("secret")

    async def get_session() -> Any:
        return session

    monkeypatch.setattr(client, "_get_session", get_session)
    with pytest.raises(VideoInteractionError, match="could not be read") as error:
        await client.upload_video(
            VideoUploadRequest("kv-test", "clip.mp4", "video/mp4", 1, source())
        )

    assert "private path" not in str(error.value)
    assert error.value.file_name == "files/kv-test"
    await client.close()


@pytest.mark.asyncio
async def test_upload_queue_wait_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    async def source() -> Any:
        yield b"x"

    client = GeminiVideoClient("secret")
    await client._upload_semaphore.acquire()
    monkeypatch.setattr(video_client, "_QUEUE_TIMEOUT_SECONDS", 0.001)
    try:
        with pytest.raises(VideoInteractionError, match="busy"):
            await client.upload_video(
                VideoUploadRequest("kv-test", "clip.mp4", "video/mp4", 1, source())
            )
    finally:
        client._upload_semaphore.release()
        await client.close()


@pytest.mark.asyncio
async def test_upload_rejects_untrusted_upload_url_before_reading_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    consumed = False

    async def source() -> Any:
        nonlocal consumed
        consumed = True
        yield b"x"

    session = _Session([_Response(200, headers={"X-Goog-Upload-URL": "https://evil.test/upload"})])
    client = GeminiVideoClient("secret")

    async def get_session() -> Any:
        return session

    monkeypatch.setattr(client, "_get_session", get_session)
    with pytest.raises(VideoInteractionError, match="unexpected upload location"):
        await client.upload_video(
            VideoUploadRequest("kv-test", "clip.mp4", "video/mp4", 1, source())
        )

    assert consumed is False
    await client.close()


@pytest.mark.asyncio
async def test_upload_polls_until_active_and_rejects_overlong_video(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def source() -> Any:
        yield b"x"

    monkeypatch.setattr(video_client, "_ACTIVATION_POLL_INITIAL_DELAY_SECONDS", 0)
    upload_url = "https://generativelanguage.googleapis.com/upload/v1beta/files/session"
    session = _Session(
        [
            _Response(200, headers={"X-Goog-Upload-URL": upload_url}),
            _Response(
                200,
                _file_response("kv-test", size=1, state="PROCESSING", duration=None),
            ),
        ],
        get_responses=[_Response(200, _file_response("kv-test", size=1, duration="3600.1s"))],
    )
    client = GeminiVideoClient("secret")

    async def get_session() -> Any:
        return session

    monkeypatch.setattr(client, "_get_session", get_session)
    with pytest.raises(VideoInteractionError, match="exceeds") as error:
        await client.upload_video(
            VideoUploadRequest("kv-test", "clip.mp4", "video/mp4", 1, source())
        )

    assert error.value.file_name == "files/kv-test"
    assert len(session.gets) == 1
    await client.close()


@pytest.mark.asyncio
async def test_uploaded_file_interaction_and_delete_payloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session([_Response(200, _response("one")), _Response(404)])
    client = GeminiVideoClient("secret")

    async def get_session() -> Any:
        return session

    monkeypatch.setattr(client, "_get_session", get_session)
    result = await client.start_from_file(
        file_uri="https://generativelanguage.googleapis.com/v1beta/files/kv-test",
        mime_type="video/mp4",
        question="Question",
        model="gemini-3.7-flash",
        thinking_level="low",
        max_output_tokens=512,
    )
    await client.delete_file("files/kv-test")

    assert result.interaction_id == "one"
    assert session.posts[0]["json"]["input"][0] == {
        "type": "video",
        "uri": "https://generativelanguage.googleapis.com/v1beta/files/kv-test",
        "mime_type": "video/mp4",
    }
    assert session.deletes[0].endswith("/kv-test")
    await client.close()


def test_parse_interaction_normalizes_structured_output_and_cached_usage() -> None:
    result = _parse_interaction(
        {
            "id": "interaction-1",
            "model": "gemini-3.7-flash",
            "status": "completed",
            "steps": [
                {"type": "thought", "content": []},
                {
                    "type": "model_output",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                '{"answer":"Answer","evidence":[{'
                                '"start_seconds":20,"end_seconds":10,'
                                '"basis":"visual","claim":"Visible"}],'
                                '"limitations":["One FPS"]}'
                            ),
                        }
                    ],
                },
            ],
            "usage": {
                "total_input_tokens": 1000,
                "total_cached_tokens": 800,
                "total_output_tokens": 50,
                "total_thought_tokens": 25,
            },
        }
    )

    assert result.interaction_id == "interaction-1"
    assert result.evidence[0].start_seconds == 10
    assert result.evidence[0].end_seconds == 20
    assert result.usage.input_tokens == 200
    assert result.usage.cached_tokens == 800
    assert result.usage.output_tokens == 75
    assert result.usage_present is True


def test_parse_interaction_distinguishes_missing_usage_from_reported_zero() -> None:
    missing_payload = _response("missing")
    missing_payload.pop("usage")
    zero_payload = _response("zero")
    zero_payload["usage"] = {}

    missing = _parse_interaction(missing_payload)
    reported_zero = _parse_interaction(zero_payload)

    assert missing.usage == reported_zero.usage
    assert missing.usage_present is False
    assert reported_zero.usage_present is True


def test_parse_interaction_rejects_noncompleted_or_unstructured_response() -> None:
    with pytest.raises(VideoInteractionError):
        _parse_interaction({"id": "x", "status": "failed", "steps": []})

    with pytest.raises(VideoInteractionError) as malformed:
        _parse_interaction(
            {
                "id": "x",
                "model": "gemini-3.7-flash",
                "status": "completed",
                "steps": [
                    {
                        "type": "model_output",
                        "content": [{"type": "text", "text": "not json"}],
                    }
                ],
                "usage": {"total_input_tokens": 25, "total_output_tokens": 5},
            }
        )
    assert malformed.value.interaction_id == "x"
    assert malformed.value.model == "gemini-3.7-flash"
    assert malformed.value.usage is not None
    assert malformed.value.usage.input_tokens == 25
    assert malformed.value.usage_present is True


@pytest.mark.asyncio
async def test_create_retries_on_transient_http_error_and_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resp_429 = _Response(429, headers={"Retry-After": "1"})
    resp_200 = _Response(200, payload=_response("success"))
    session = _Session([resp_429, resp_200])
    client = GeminiVideoClient("secret")

    async def get_session() -> Any:
        return session

    monkeypatch.setattr(client, "_get_session", get_session)
    result = await client.start(
        url="https://www.youtube.com/watch?v=abcdefghijk",
        question="Question",
        model="custom-video-model",
        thinking_level="low",
        max_output_tokens=1024,
    )
    assert result.interaction_id == "success"
    assert len(session.posts) == 2
    await client.close()


@pytest.mark.asyncio
async def test_create_fails_immediately_on_400_without_retrying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resp_400 = _Response(400)
    session = _Session([resp_400])
    client = GeminiVideoClient("secret")

    async def get_session() -> Any:
        return session

    monkeypatch.setattr(client, "_get_session", get_session)
    with pytest.raises(VideoInteractionError, match="could not access or analyze"):
        await client.start(
            url="https://www.youtube.com/watch?v=abcdefghijk",
            question="Question",
            model="custom-video-model",
            thinking_level="low",
            max_output_tokens=1024,
        )
    assert len(session.posts) == 1
    await client.close()


@pytest.mark.asyncio
async def test_create_retries_on_transport_error_and_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import aiohttp

    resp_error = _Response(500, enter_error=aiohttp.ClientError("network failure"))
    resp_200 = _Response(200, payload=_response("recovered"))
    session = _Session([resp_error, resp_200])
    client = GeminiVideoClient("secret")

    async def get_session() -> Any:
        return session

    monkeypatch.setattr(client, "_get_session", get_session)
    result = await client.start(
        url="https://www.youtube.com/watch?v=abcdefghijk",
        question="Question",
        model="custom-video-model",
        thinking_level="low",
        max_output_tokens=1024,
    )
    assert result.interaction_id == "recovered"
    assert len(session.posts) == 2
    await client.close()


@pytest.mark.asyncio
async def test_start_upload_retries_on_503_and_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upload_url = "https://generativelanguage.googleapis.com/upload/v1beta/files/session"
    resp_503 = _Response(503)
    resp_200 = _Response(200, headers={"X-Goog-Upload-URL": upload_url})
    session = _Session([resp_503, resp_200])
    client = GeminiVideoClient("secret")

    url = await client._start_upload(
        session,  # type: ignore[arg-type]
        file_id="test-file",
        display_name="test.mp4",
        mime_type="video/mp4",
        declared_size=100,
    )
    assert url == upload_url
    assert len(session.posts) == 2


@pytest.mark.asyncio
async def test_start_upload_fails_immediately_on_400(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resp_400 = _Response(400)
    session = _Session([resp_400])
    client = GeminiVideoClient("secret")

    with pytest.raises(VideoInteractionError, match="could not access or analyze"):
        await client._start_upload(
            session,  # type: ignore[arg-type]
            file_id="test-file",
            display_name="test.mp4",
            mime_type="video/mp4",
            declared_size=100,
        )
    assert len(session.posts) == 1

