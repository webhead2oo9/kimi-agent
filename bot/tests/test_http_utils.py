from __future__ import annotations

from typing import Any, cast

import aiohttp
import pytest

from utils.http import read_bounded_body


class _Content:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def iter_chunked(self, _size: int):
        for chunk in self._chunks:
            yield chunk


def _response(*, declared: str | None, chunks: list[bytes]) -> aiohttp.ClientResponse:
    headers = {} if declared is None else {"Content-Length": declared}
    return cast(Any, type("Response", (), {"headers": headers, "content": _Content(chunks)})())


@pytest.mark.asyncio
@pytest.mark.parametrize("declared", ("not-a-number", "-1"))
async def test_read_bounded_body_rejects_invalid_declared_size(declared: str) -> None:
    with pytest.raises(ValueError):
        await read_bounded_body(_response(declared=declared, chunks=[]), 4)


@pytest.mark.asyncio
async def test_read_bounded_body_enforces_streamed_size_without_header() -> None:
    with pytest.raises(ValueError, match="byte cap"):
        await read_bounded_body(_response(declared=None, chunks=[b"123", b"45"]), 4)


@pytest.mark.asyncio
async def test_read_bounded_body_returns_body_within_cap() -> None:
    assert await read_bounded_body(_response(declared="4", chunks=[b"12", b"34"]), 4) == b"1234"
