from __future__ import annotations

from typing import Any

from search.http import post_json
from search.normalize import canonical_url, clean_text, filter_results, unique_content
from search.types import (
    BackendResponse,
    ContentsRequest,
    JsonPost,
    SearchProviderError,
    SearchRequest,
    SearchResult,
)


class BraveSearchBackend:
    name = "brave"
    supports_contents = False
    supports_text = False
    supports_contents_text = False

    def __init__(
        self,
        api_key: str,
        *,
        context_url: str = "https://api.search.brave.com/res/v1/llm/context",
        timeout_seconds: float = 30.0,
        safesearch: str = "moderate",
        request: JsonPost = post_json,
    ) -> None:
        self._api_key = api_key
        self._context_url = context_url
        self._timeout_seconds = timeout_seconds
        self._safesearch = safesearch
        self._request = request

    async def search(self, request: SearchRequest) -> BackendResponse:
        payload: dict[str, Any] = {
            "q": request.query,
            "maximum_number_of_urls": request.num_results,
            "safesearch": self._safesearch,
        }
        if request.country:
            payload["country"] = request.country
        if request.start_published_date and request.end_published_date:
            payload["freshness"] = f"{request.start_published_date}to{request.end_published_date}"
        response = await self._request(
            self._context_url,
            {
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "Content-Type": "application/json",
                "X-Subscription-Token": self._api_key,
            },
            payload,
            self._timeout_seconds,
        )
        if not 200 <= response.status < 300:
            raise SearchProviderError(f"Brave returned HTTP {response.status}.")
        _validate_payload(response.payload)
        results = _normalize_results(response.payload)
        if _has_grounding_items(response.payload) and not results:
            raise SearchProviderError("Brave returned an invalid response shape.")
        results = filter_results(
            results,
            include_domains=request.include_domains,
            exclude_domains=request.exclude_domains,
            start_date=request.start_published_date,
            end_date=request.end_published_date,
        )
        return BackendResponse(provider=self.name, results=results)

    async def contents(self, request: ContentsRequest) -> BackendResponse:
        raise SearchProviderError("Brave does not support page reading.")


def _normalize_results(payload: dict[str, Any]) -> tuple[SearchResult, ...]:
    grounding = payload.get("grounding")
    if not isinstance(grounding, dict):
        return ()
    raw_results: list[object] = []
    generic = grounding.get("generic")
    if isinstance(generic, list):
        raw_results.extend(generic)
    poi = grounding.get("poi")
    if isinstance(poi, dict):
        raw_results.append(poi)
    maps = grounding.get("map")
    if isinstance(maps, list):
        raw_results.extend(maps)

    sources = payload.get("sources")
    source_map = sources if isinstance(sources, dict) else {}
    results: list[SearchResult] = []
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        url = clean_text(item.get("url"))
        if not canonical_url(url):
            continue
        source = source_map.get(url)
        source_data = source if isinstance(source, dict) else {}
        snippets = item.get("snippets")
        content_values = snippets if isinstance(snippets, list) else []
        results.append(
            SearchResult(
                title=clean_text(item.get("title") or source_data.get("title")),
                url=url,
                content=unique_content(content_values),
                published_at=_published_at(source_data.get("age")),
            )
        )
    return tuple(results)


def _validate_payload(payload: dict[str, Any]) -> None:
    grounding = payload.get("grounding")
    if not isinstance(grounding, dict):
        raise SearchProviderError("Brave returned an invalid response shape.")
    if not isinstance(grounding.get("generic"), list):
        raise SearchProviderError("Brave returned an invalid response shape.")
    if not isinstance(payload.get("sources"), dict):
        raise SearchProviderError("Brave returned an invalid response shape.")
    if grounding.get("map") is not None and not isinstance(grounding.get("map"), list):
        raise SearchProviderError("Brave returned an invalid response shape.")
    if grounding.get("poi") is not None and not isinstance(grounding.get("poi"), dict):
        raise SearchProviderError("Brave returned an invalid response shape.")


def _has_grounding_items(payload: dict[str, Any]) -> bool:
    grounding = payload["grounding"]
    assert isinstance(grounding, dict)
    return bool(grounding.get("generic") or grounding.get("poi") or grounding.get("map"))


def _published_at(raw: object) -> str | None:
    if not isinstance(raw, list):
        return None
    for index in (3, 1):
        if index < len(raw):
            value = clean_text(raw[index])
            if value:
                return value
    return None
