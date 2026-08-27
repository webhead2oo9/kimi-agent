from __future__ import annotations

import json
from collections.abc import Sequence
from unittest.mock import AsyncMock

import pytest

from search import http
from search.types import SearchProviderError


class _Content:
    def __init__(self, body: bytes) -> None:
        self.body = body

    async def iter_chunked(self, size: int):
        for offset in range(0, len(self.body), size):
            yield self.body[offset : offset + size]


class _Response:
    def __init__(
        self,
        body: bytes,
        *,
        status: int = 200,
        content_length: int | None = None,
    ) -> None:
        self.content = _Content(body)
        self.headers = {} if content_length is None else {"Content-Length": str(content_length)}
        self.status = status

    async def __aenter__(self) -> _Response:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None


class _Session:
    def __init__(self, factory: _SessionFactory) -> None:
        self._factory = factory

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    def post(self, *_: object, **__: object) -> _Response:
        self._factory.calls += 1
        return self._factory.responses.pop(0)


class _SessionFactory:
    def __init__(self, responses: Sequence[_Response]) -> None:
        self.responses = list(responses)
        self.calls = 0

    def __call__(self, **_: object) -> _Session:
        return _Session(self)


@pytest.mark.asyncio
async def test_search_json_response_has_a_byte_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(http, "_MAX_JSON_RESPONSE_BYTES", 32)

    with pytest.raises(SearchProviderError, match="invalid JSON"):
        await http._response_json(_Response(b'{"result":"' + b"x" * 64 + b'"}'))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_search_json_response_has_a_depth_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(http, "_MAX_JSON_DEPTH", 3)
    nested: object = "leaf"
    for _ in range(5):
        nested = [nested]

    with pytest.raises(SearchProviderError, match="invalid JSON"):
        await http._response_json(
            _Response(json.dumps({"result": nested}).encode())  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("body", (b"service unavailable", b'{"error":'))
async def test_transient_status_retries_an_invalid_json_body(
    monkeypatch: pytest.MonkeyPatch,
    body: bytes,
) -> None:
    sessions = _SessionFactory(
        (
            _Response(body, status=503),
            _Response(b'{"results": []}', status=200),
        )
    )
    sleep = AsyncMock()
    monkeypatch.setattr(http.aiohttp, "ClientSession", sessions)
    monkeypatch.setattr(http.asyncio, "sleep", sleep)

    response = await http.post_json("https://search.example", {}, {}, 5)

    assert response.status == 200
    assert response.payload == {"results": []}
    assert sessions.calls == 2
    sleep.assert_awaited_once_with(1.0)


@pytest.mark.asyncio
async def test_terminal_status_does_not_retry_an_invalid_json_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = _SessionFactory((_Response(b"bad request", status=400),))
    sleep = AsyncMock()
    monkeypatch.setattr(http.aiohttp, "ClientSession", sessions)
    monkeypatch.setattr(http.asyncio, "sleep", sleep)

    with pytest.raises(SearchProviderError, match="invalid JSON"):
        await http.post_json("https://search.example", {}, {}, 5)

    assert sessions.calls == 1
    sleep.assert_not_awaited()
