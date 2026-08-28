from __future__ import annotations

import asyncio
import json
from typing import Any

import aiohttp

from search.types import HttpResponse, SearchProviderError
from utils.http import read_bounded_body

_TRANSIENT_STATUSES = {408, 409, 425, 429, 500, 502, 503, 504}
_MAX_JSON_RESPONSE_BYTES = 4 * 1024 * 1024
_MAX_JSON_NODES = 50_000
_MAX_JSON_DEPTH = 32


async def post_json(
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout_seconds: float,
) -> HttpResponse:
    """POST JSON with one bounded retry for transient failures."""
    return await _request_json("POST", url, headers, timeout_seconds, payload=payload)


async def get_json(
    url: str,
    headers: dict[str, str],
    params: dict[str, str],
    timeout_seconds: float,
) -> HttpResponse:
    """GET JSON with one bounded retry for transient failures."""
    return await _request_json("GET", url, headers, timeout_seconds, params=params)


async def _request_json(
    method: str,
    url: str,
    headers: dict[str, str],
    timeout_seconds: float,
    *,
    payload: dict[str, Any] | None = None,
    params: dict[str, str] | None = None,
) -> HttpResponse:
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            timeout = aiohttp.ClientTimeout(total=timeout_seconds)
            async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
                call = (
                    session.post(url, headers=headers, json=payload)
                    if method == "POST"
                    else session.get(url, headers=headers, params=params)
                )
                async with call as response:
                    try:
                        data = await _response_json(response)
                    except SearchProviderError:
                        if response.status not in _TRANSIENT_STATUSES or attempt == 1:
                            raise
                    else:
                        result = HttpResponse(
                            status=response.status,
                            payload=data,
                            headers={
                                key.casefold(): value for key, value in response.headers.items()
                            },
                        )
                        if response.status not in _TRANSIENT_STATUSES or attempt == 1:
                            return result
        except (aiohttp.ClientError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt == 1:
                break
        # Brave documents a one-second sliding rate-limit window. Waiting a
        # complete window also gives other transient provider failures a useful
        # bounded backoff before the single retry.
        await asyncio.sleep(1.0)
    raise SearchProviderError("Search provider request failed.") from last_error


async def _response_json(response: aiohttp.ClientResponse) -> dict[str, Any]:
    try:
        raw = await read_bounded_body(response, _MAX_JSON_RESPONSE_BYTES)
        data = json.loads(raw)
        _validate_json_structure(data)
    except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise SearchProviderError("Search provider returned invalid JSON.") from exc
    if not isinstance(data, dict):
        raise SearchProviderError("Search provider returned an invalid response shape.")
    return data


def _validate_json_structure(value: object) -> None:
    nodes = 0
    stack: list[tuple[object, int]] = [(value, 0)]
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > _MAX_JSON_NODES or depth > _MAX_JSON_DEPTH:
            raise ValueError("response JSON exceeds structure cap")
        if isinstance(current, dict):
            stack.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)
