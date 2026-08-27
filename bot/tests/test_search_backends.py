from __future__ import annotations

from typing import Any

import pytest

from search.brave import BraveSearchBackend
from search.exa import ExaSearchBackend, _is_safe_contents_url
from search.types import ContentsRequest, HttpResponse, SearchProviderError, SearchRequest


class RecordingPost:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.response_payload = payload
        self.calls: list[tuple[str, dict[str, str], dict[str, Any], float]] = []

    async def __call__(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout_seconds: float,
    ) -> HttpResponse:
        self.calls.append((url, headers, payload, timeout_seconds))
        return HttpResponse(200, self.response_payload, {})


@pytest.mark.asyncio
async def test_exa_search_uses_highlights_and_normalizes_reported_cost() -> None:
    post = RecordingPost(
        {
            "costDollars": {"total": 0.012},
            "results": [
                {
                    "title": " Example  page ",
                    "url": "https://example.com/a",
                    "highlights": [" useful  text ", "useful text"],
                    "publishedDate": "2026-08-20T00:00:00Z",
                    "author": "A. Writer",
                }
            ],
        }
    )
    backend = ExaSearchBackend("secret", request=post)

    response = await backend.search(
        SearchRequest(
            query="waffles",
            num_results=10,
            include_domains=("example.com",),
            country="US",
        )
    )

    assert post.calls[0][0] == "https://api.exa.ai/search"
    assert post.calls[0][2] == {
        "query": "waffles",
        "numResults": 10,
        "contents": {"highlights": True},
        "includeDomains": ["example.com"],
        "userLocation": "US",
    }
    assert post.calls[0][1]["x-api-key"] == "secret"
    assert response.reported_cost_usd == 0.012
    assert response.results[0].content == ("useful text",)


@pytest.mark.asyncio
async def test_exa_contents_uses_top_level_content_option_and_statuses() -> None:
    post = RecordingPost(
        {
            "statuses": [
                {"id": "https://example.com/good", "status": "success"},
                {"id": "https://example.com/bad", "status": "error"},
            ],
            "results": [
                {"url": "https://example.com/good", "text": "complete page"},
                {"url": "https://example.com/bad", "text": "should be omitted"},
            ],
        }
    )
    backend = ExaSearchBackend("secret", request=post)

    response = await backend.contents(
        ContentsRequest(
            urls=("https://example.com/good", "https://example.com/bad"),
            content_mode="text",
        )
    )

    assert post.calls[0][0] == "https://api.exa.ai/contents"
    assert post.calls[0][2] == {
        "urls": ["https://example.com/good", "https://example.com/bad"],
        "text": True,
    }
    assert [item.url for item in response.results] == ["https://example.com/good"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    (
        "file:///etc/passwd",
        "https://user:password@example.com/private",
        "http://localhost/admin",
        "https://service.localhost/internal",
        "http://127.0.0.1/admin",
        "http://10.0.0.1/internal",
        "http://169.254.169.254/latest/meta-data",
        "https://192.0.2.1/reserved",
        "http://[::1]/admin",
        "http://[::ffff:127.0.0.1]/admin",
        "http://2130706433/admin",
        "http://127.1/admin",
        "http://0x7f000001/admin",
        "http://017700000001/admin",
    ),
)
async def test_exa_contents_rejects_unsafe_urls_before_request(url: str) -> None:
    post = RecordingPost({})
    backend = ExaSearchBackend("secret", request=post)

    with pytest.raises(SearchProviderError, match=r"public HTTP\(S\) URLs"):
        await backend.contents(ContentsRequest(urls=(url,)))

    assert post.calls == []


@pytest.mark.parametrize("url", ("https://example.com/page", "https://8.8.8.8/page"))
def test_exa_contents_url_validation_preserves_public_hosts(url: str) -> None:
    assert _is_safe_contents_url(url) is True


@pytest.mark.asyncio
async def test_brave_context_request_and_clean_response() -> None:
    post = RecordingPost(
        {
            "grounding": {
                "generic": [
                    {
                        "url": "https://example.com/post",
                        "title": "Post",
                        "snippets": ["first", "first", '{"table":[1,2]}'],
                    }
                ],
                "map": [],
            },
            "sources": {
                "https://example.com/post": {
                    "age": ["long", "2026-08-21", "3 days ago", "2026-08-21T12:30:00Z"]
                }
            },
        }
    )
    backend = BraveSearchBackend("brave-secret", request=post, safesearch="moderate")

    response = await backend.search(
        SearchRequest(
            query="recent example",
            num_results=10,
            start_published_date="2026-08-01",
            end_published_date="2026-08-24",
            country="US",
        )
    )

    assert post.calls[0][2] == {
        "q": "recent example",
        "maximum_number_of_urls": 10,
        "safesearch": "moderate",
        "country": "US",
        "freshness": "2026-08-01to2026-08-24",
    }
    assert post.calls[0][1]["X-Subscription-Token"] == "brave-secret"
    assert response.results[0].published_at == "2026-08-21T12:30:00Z"
    assert response.results[0].content == ("first", '{"table":[1,2]}')


@pytest.mark.asyncio
async def test_brave_enforces_domain_filter_locally() -> None:
    post = RecordingPost(
        {
            "grounding": {
                "generic": [
                    {"url": "https://wanted.example/a", "title": "Keep", "snippets": ["yes"]},
                    {"url": "https://other.example/a", "title": "Drop", "snippets": ["no"]},
                ]
            },
            "sources": {},
        }
    )
    backend = BraveSearchBackend("secret", request=post)

    response = await backend.search(
        SearchRequest(query="anything", num_results=10, include_domains=("wanted.example",))
    )

    assert [item.title for item in response.results] == ["Keep"]
    assert "include_domains" not in post.calls[0][2]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["exa", "brave"])
async def test_http_200_missing_required_response_shape_is_not_zero_matches(
    provider: str,
) -> None:
    post = RecordingPost({})
    backend = (
        ExaSearchBackend("secret", request=post)
        if provider == "exa"
        else BraveSearchBackend("secret", request=post)
    )

    with pytest.raises(SearchProviderError, match="invalid response shape"):
        await backend.search(SearchRequest(query="anything", num_results=10))


@pytest.mark.asyncio
async def test_exa_contents_all_failed_is_not_zero_matches() -> None:
    post = RecordingPost(
        {
            "results": [],
            "statuses": [{"id": "https://example.com", "status": "error"}],
        }
    )

    with pytest.raises(SearchProviderError, match="could not read"):
        await ExaSearchBackend("secret", request=post).contents(
            ContentsRequest(urls=("https://example.com",))
        )
