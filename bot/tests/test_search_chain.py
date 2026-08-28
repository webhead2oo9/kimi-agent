from __future__ import annotations

from dataclasses import dataclass

import pytest

from search.chain import SearchChain
from search.types import (
    BackendResponse,
    ContentsRequest,
    SearchBudgetExceeded,
    SearchProviderError,
    SearchRequest,
    SearchResult,
)


@dataclass
class FakeBackend:
    name: str
    search_response: BackendResponse | Exception
    supports_contents: bool = False
    supports_text: bool = False
    supports_contents_text: bool = False
    calls: int = 0

    async def search(self, request: SearchRequest) -> BackendResponse:
        self.calls += 1
        if isinstance(self.search_response, Exception):
            raise self.search_response
        return self.search_response

    async def contents(self, request: ContentsRequest) -> BackendResponse:
        self.calls += 1
        if isinstance(self.search_response, Exception):
            raise self.search_response
        return self.search_response


def _response(provider: str, *urls: str) -> BackendResponse:
    return BackendResponse(
        provider,
        tuple(SearchResult(title=f"{provider}-{index}", url=url) for index, url in enumerate(urls)),
    )


@pytest.mark.asyncio
async def test_blend_is_exa_first_and_exa_wins_even_late_duplicates() -> None:
    exa = FakeBackend(
        "exa",
        _response("exa", "https://exa.example/1", "https://shared.example/page"),
    )
    brave = FakeBackend(
        "brave",
        _response(
            "brave",
            "http://www.shared.example/page?utm_source=x",
            "https://brave.example/2",
        ),
    )
    spent = 0

    def consume() -> None:
        nonlocal spent
        spent += 1

    result = await SearchChain([exa, brave], timeout_seconds=1).search(
        SearchRequest(query="q", num_results=10), strategy="blend", consume_call=consume
    )

    assert [item.url for item in result.results] == [
        "https://exa.example/1",
        "https://shared.example/page",
        "https://brave.example/2",
    ]
    assert spent == 2


@pytest.mark.asyncio
async def test_blend_returns_partial_success_and_failover_stops_on_zero_matches() -> None:
    broken = FakeBackend("exa", SearchProviderError("down"))
    empty = FakeBackend("brave", _response("brave"))
    calls = 0

    def consume() -> None:
        nonlocal calls
        calls += 1

    blended = await SearchChain([broken, empty], timeout_seconds=1).search(
        SearchRequest(query="q", num_results=10), strategy="blend", consume_call=consume
    )
    assert blended.results == ()
    assert calls == 2

    first_empty = FakeBackend("exa", _response("exa"))
    unused = FakeBackend("brave", _response("brave", "https://example.com"))
    failed_over = await SearchChain([first_empty, unused], timeout_seconds=1).search(
        SearchRequest(query="q", num_results=10), strategy="failover", consume_call=lambda: None
    )
    assert failed_over.results == ()
    assert unused.calls == 0


@pytest.mark.asyncio
async def test_backend_budget_failure_is_distinct_from_provider_failure() -> None:
    backend = FakeBackend("exa", _response("exa", "https://example.com"))

    def exhausted() -> None:
        raise SearchBudgetExceeded

    with pytest.raises(SearchBudgetExceeded):
        await SearchChain([backend], timeout_seconds=1).search(
            SearchRequest(query="q", num_results=10),
            strategy="blend",
            consume_call=exhausted,
        )


@pytest.mark.asyncio
async def test_text_mode_splits_search_and_page_read_eligibility() -> None:
    # TinyFish's shape: snippets from search, whole pages from fetch.
    backend = FakeBackend(
        "tinyfish",
        _response("tinyfish", "https://example.com/a"),
        supports_contents=True,
        supports_text=False,
        supports_contents_text=True,
    )
    chain = SearchChain([backend], timeout_seconds=1)

    with pytest.raises(SearchProviderError, match="No configured search backend"):
        await chain.search(
            SearchRequest(query="anything", num_results=5, content_mode="text"),
            strategy="blend",
            consume_call=lambda: None,
        )
    assert backend.calls == 0

    response = await chain.contents(
        ContentsRequest(urls=("https://example.com/a",), content_mode="text"),
        consume_call=lambda: None,
    )
    assert backend.calls == 1
    assert [item.url for item in response.results] == ["https://example.com/a"]
