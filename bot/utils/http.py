"""Shared bounded HTTP response readers."""

from __future__ import annotations

from collections.abc import Callable

import aiohttp


async def read_bounded_body(
    response: aiohttp.ClientResponse,
    max_bytes: int,
    *,
    error: Callable[[str], Exception] = ValueError,
) -> bytes:
    """Read a response body without trusting its declared or streamed size."""
    declared = response.headers.get("Content-Length")
    if declared is not None:
        try:
            declared_size = int(declared)
        except ValueError as exc:
            raise error("invalid Content-Length") from exc
        if declared_size < 0 or declared_size > max_bytes:
            raise error("response body exceeds byte cap")

    body = bytearray()
    async for chunk in response.content.iter_chunked(min(65_536, max_bytes + 1)):
        if len(chunk) > max_bytes - len(body):
            raise error("response body exceeds byte cap")
        body.extend(chunk)
    return bytes(body)
