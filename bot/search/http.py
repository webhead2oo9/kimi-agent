from __future__ import annotations

import asyncio
from typing import Any

import aiohttp

from search.types import HttpResponse, SearchProviderError

_TRANSIENT_STATUSES = {408, 409, 425, 429, 500, 502, 503, 504}


async def post_json(
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout_seconds: float,
) -> HttpResponse:
    """POST JSON with one bounded retry for transient transport failures."""
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            timeout = aiohttp.ClientTimeout(total=timeout_seconds)
            async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
                async with session.post(url, headers=headers, json=payload) as response:
                    data = await _response_json(response)
                    result = HttpResponse(
                        status=response.status,
                        payload=data,
                        headers={key.casefold(): value for key, value in response.headers.items()},
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
        data = await response.json(content_type=None)
    except (aiohttp.ContentTypeError, ValueError) as exc:
        raise SearchProviderError("Search provider returned invalid JSON.") from exc
    if not isinstance(data, dict):
        raise SearchProviderError("Search provider returned an invalid response shape.")
    return data
