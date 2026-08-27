from __future__ import annotations

from typing import Any

import pytest

from video_understanding import client as video_client
from video_understanding.client import GeminiVideoClient, VideoInteractionError, _parse_interaction


class _Response:
    def __init__(
        self,
        status: int,
        payload: object | None = None,
        *,
        json_error: Exception | None = None,
    ) -> None:
        self.status = status
        self.payload = payload
        self.json_error = json_error
        self.json_called = False
        self.read_called = False

    async def __aenter__(self) -> _Response:
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
        return b"provider body"


class _Session:
    closed = False

    def __init__(self, responses: list[_Response]) -> None:
        self.responses = responses
        self.posts: list[dict[str, Any]] = []
        self.deletes: list[str] = []

    def post(self, url: str, **kwargs: Any) -> _Response:
        self.posts.append({"url": url, **kwargs})
        return self.responses.pop(0)

    def delete(self, url: str, **kwargs: Any) -> _Response:
        self.deletes.append(url)
        return self.responses.pop(0)


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
