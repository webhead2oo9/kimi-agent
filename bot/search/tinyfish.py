from __future__ import annotations

import asyncio
from typing import Any

from search.http import get_json, post_json
from search.normalize import (
    canonical_url,
    clean_text,
    filter_results,
    is_safe_fetch_url,
    unique_content,
)
from search.types import (
    BackendResponse,
    ContentsRequest,
    HttpResponse,
    JsonGet,
    JsonPost,
    SearchProviderError,
    SearchRequest,
    SearchResult,
)

# The fetch endpoint accepts at most ten URLs per request.
_FETCH_URL_BATCH = 10
# Documented bounds for the per-URL fetch budget, in milliseconds.
_MIN_PER_URL_TIMEOUT_MS = 1
_MAX_PER_URL_TIMEOUT_MS = 110_000


class TinyFishSearchBackend:
    name = "tinyfish"
    supports_contents = True
    # Search returns snippets only; fetch returns whole pages as markdown.
    supports_text = False
    supports_contents_text = True

    def __init__(
        self,
        api_key: str,
        *,
        search_url: str = "https://api.search.tinyfish.ai",
        fetch_url: str = "https://api.fetch.tinyfish.ai",
        timeout_seconds: float = 30.0,
        get: JsonGet = get_json,
        request: JsonPost = post_json,
    ) -> None:
        self._api_key = api_key
        self._search_url = search_url.rstrip("/")
        self._fetch_url = fetch_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._get = get
        self._request = request

    async def search(self, request: SearchRequest) -> BackendResponse:
        # Search has no result-count parameter: it returns a fixed page, and the
        # chain truncates to num_results.
        params: dict[str, str] = {"query": request.query}
        if request.country:
            params["location"] = request.country
        if request.include_domains:
            params["include_domains"] = ",".join(request.include_domains)
        if request.exclude_domains:
            params["exclude_domains"] = ",".join(request.exclude_domains)
        if request.start_published_date:
            params["after_date"] = request.start_published_date
        if request.end_published_date:
            params["before_date"] = request.end_published_date

        response = await self._get_search(params)
        raw_results = _required_list(response.payload, "results")
        results = _normalize_search_results(raw_results)
        if raw_results and not results:
            raise SearchProviderError("TinyFish returned an invalid response shape.")
        results = filter_results(
            results,
            include_domains=request.include_domains,
            exclude_domains=request.exclude_domains,
            start_date=request.start_published_date,
            end_date=request.end_published_date,
        )
        return BackendResponse(provider=self.name, results=results)

    async def contents(self, request: ContentsRequest) -> BackendResponse:
        if not all(is_safe_fetch_url(url) for url in request.urls):
            raise SearchProviderError("TinyFish can only read public HTTP(S) URLs.")
        batches = [
            request.urls[start : start + _FETCH_URL_BATCH]
            for start in range(0, len(request.urls), _FETCH_URL_BATCH)
        ]
        batch_results = await asyncio.gather(*(self._try_fetch_batch(batch) for batch in batches))

        results = [
            result
            for batch_result in batch_results
            if batch_result is not None
            for result in batch_result
        ]
        if not results:
            # Per-URL failures ride along with HTTP 200 in errors[], so an empty
            # result set is the only signal that every page failed.
            raise SearchProviderError("TinyFish could not read the requested pages.")
        return BackendResponse(provider=self.name, results=tuple(results))

    async def _try_fetch_batch(self, urls: tuple[str, ...]) -> tuple[SearchResult, ...] | None:
        try:
            response = await self._post_fetch(urls)
            return _normalize_fetch_results(_required_list(response.payload, "results"))
        except SearchProviderError:
            return None

    async def _get_search(self, params: dict[str, str]) -> HttpResponse:
        response = await self._get(
            self._search_url,
            {"Accept": "application/json", "X-API-Key": self._api_key},
            params,
            self._timeout_seconds,
        )
        if not 200 <= response.status < 300:
            raise SearchProviderError(f"TinyFish returned HTTP {response.status}.")
        return response

    async def _post_fetch(self, urls: tuple[str, ...]) -> HttpResponse:
        response = await self._request(
            self._fetch_url,
            {
                "Content-Type": "application/json",
                "X-API-Key": self._api_key,
            },
            {
                "urls": list(urls),
                "format": "markdown",
                "per_url_timeout_ms": self._per_url_timeout_ms(),
            },
            self._timeout_seconds,
        )
        if not 200 <= response.status < 300:
            raise SearchProviderError(f"TinyFish returned HTTP {response.status}.")
        return response

    def _per_url_timeout_ms(self) -> int:
        # Leave headroom inside our own deadline so a slow page fails as a
        # per-URL error instead of timing out the whole turn.
        budget = int(self._timeout_seconds * 800)
        return max(_MIN_PER_URL_TIMEOUT_MS, min(budget, _MAX_PER_URL_TIMEOUT_MS))


def _normalize_search_results(raw: list[object]) -> tuple[SearchResult, ...]:
    results: list[SearchResult] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        url = clean_text(item.get("url"))
        if not canonical_url(url):
            continue
        results.append(
            SearchResult(
                title=clean_text(item.get("title")),
                url=url,
                content=unique_content([item.get("snippet")]),
                published_at=clean_text(item.get("date")) or None,
                author=None,
            )
        )
    return tuple(results)


def _normalize_fetch_results(raw: list[object]) -> tuple[SearchResult, ...]:
    results: list[SearchResult] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        url = clean_text(item.get("final_url")) or clean_text(item.get("url"))
        if not canonical_url(url):
            continue
        results.append(
            SearchResult(
                title=clean_text(item.get("title")),
                url=url,
                content=unique_content([item.get("text")]),
                published_at=clean_text(item.get("published_date")) or None,
                author=clean_text(item.get("author")) or None,
            )
        )
    return tuple(results)


def _required_list(payload: dict[str, Any], field: str) -> list[object]:
    value = payload.get(field)
    if not isinstance(value, list):
        raise SearchProviderError("TinyFish returned an invalid response shape.")
    return value
