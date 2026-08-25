from __future__ import annotations

from typing import Any

from search.http import post_json
from search.normalize import canonical_url, clean_text, filter_results, unique_content
from search.types import (
    BackendResponse,
    ContentsRequest,
    HttpResponse,
    JsonPost,
    SearchProviderError,
    SearchRequest,
    SearchResult,
)


class ExaSearchBackend:
    name = "exa"
    supports_contents = True
    supports_text = True

    def __init__(
        self,
        api_key: str,
        *,
        api_base: str = "https://api.exa.ai",
        timeout_seconds: float = 30.0,
        request: JsonPost = post_json,
    ) -> None:
        self._api_key = api_key
        self._api_base = api_base.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._request = request

    async def search(self, request: SearchRequest) -> BackendResponse:
        payload: dict[str, Any] = {
            "query": request.query,
            "numResults": request.num_results,
            "contents": {request.content_mode: True},
        }
        if request.include_domains:
            payload["includeDomains"] = list(request.include_domains)
        if request.exclude_domains:
            payload["excludeDomains"] = list(request.exclude_domains)
        if request.start_published_date:
            payload["startPublishedDate"] = request.start_published_date
        if request.end_published_date:
            payload["endPublishedDate"] = request.end_published_date
        if request.country:
            payload["userLocation"] = request.country

        response = await self._post("/search", payload)
        raw_results = _required_list(response.payload, "results")
        results = _normalize_results(raw_results, request.content_mode)
        if raw_results and not results:
            raise SearchProviderError("Exa returned an invalid response shape.")
        results = filter_results(
            results,
            include_domains=request.include_domains,
            exclude_domains=request.exclude_domains,
            start_date=request.start_published_date,
            end_date=request.end_published_date,
        )
        return BackendResponse(
            provider=self.name,
            results=results,
            reported_cost_usd=_cost_dollars(response.payload),
        )

    async def contents(self, request: ContentsRequest) -> BackendResponse:
        payload: dict[str, Any] = {
            "urls": list(request.urls),
            request.content_mode: True,
        }
        response = await self._post("/contents", payload)
        raw_results = _required_list(response.payload, "results")
        statuses = _required_list(response.payload, "statuses")
        successful = _successful_content_ids(statuses)
        if not successful:
            raise SearchProviderError("Exa could not read the requested pages.")
        results = tuple(
            result
            for result in _normalize_results(raw_results, request.content_mode)
            if canonical_url(result.url) in successful
        )
        if not results:
            raise SearchProviderError("Exa returned an invalid response shape.")
        return BackendResponse(
            provider=self.name,
            results=results,
            reported_cost_usd=_cost_dollars(response.payload),
        )

    async def _post(self, path: str, payload: dict[str, Any]) -> HttpResponse:
        response = await self._request(
            f"{self._api_base}{path}",
            {
                "Content-Type": "application/json",
                "x-api-key": self._api_key,
            },
            payload,
            self._timeout_seconds,
        )
        if not 200 <= response.status < 300:
            raise SearchProviderError(f"Exa returned HTTP {response.status}.")
        return response


def _normalize_results(raw: object, content_mode: str) -> tuple[SearchResult, ...]:
    if not isinstance(raw, list):
        return ()
    results: list[SearchResult] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        url = clean_text(item.get("url"))
        if not canonical_url(url):
            continue
        if content_mode == "text":
            content_values = [item.get("text")]
        else:
            highlights = item.get("highlights")
            content_values = highlights if isinstance(highlights, list) else []
        results.append(
            SearchResult(
                title=clean_text(item.get("title")),
                url=url,
                content=unique_content(content_values),
                published_at=clean_text(item.get("publishedDate")) or None,
                author=clean_text(item.get("author")) or None,
            )
        )
    return tuple(results)


def _successful_content_ids(raw: object) -> set[str]:
    if not isinstance(raw, list):
        return set()
    return {
        canonical_url(clean_text(item.get("id")))
        for item in raw
        if isinstance(item, dict)
        and item.get("status") == "success"
        and canonical_url(clean_text(item.get("id")))
    }


def _required_list(payload: dict[str, Any], field: str) -> list[object]:
    value = payload.get(field)
    if not isinstance(value, list):
        raise SearchProviderError("Exa returned an invalid response shape.")
    return value


def _cost_dollars(payload: dict[str, Any]) -> float | None:
    raw = payload.get("costDollars")
    if isinstance(raw, int | float) and not isinstance(raw, bool):
        return float(raw)
    if isinstance(raw, dict):
        total = raw.get("total")
        if isinstance(total, int | float) and not isinstance(total, bool):
            return float(total)
    return None
