from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol


class SearchProviderError(RuntimeError):
    """A backend could not produce a trustworthy response."""


class SearchBudgetExceeded(RuntimeError):
    """The current turn has spent its internet-search backend-call budget."""


@dataclass(frozen=True)
class HttpResponse:
    status: int
    payload: dict[str, Any]
    headers: dict[str, str]


JsonPost = Callable[[str, dict[str, str], dict[str, Any], float], Awaitable[HttpResponse]]


@dataclass(frozen=True)
class SearchRequest:
    query: str
    num_results: int
    content_mode: str = "highlights"
    include_domains: tuple[str, ...] = ()
    exclude_domains: tuple[str, ...] = ()
    start_published_date: str | None = None
    end_published_date: str | None = None
    country: str | None = None


@dataclass(frozen=True)
class ContentsRequest:
    urls: tuple[str, ...]
    content_mode: str = "highlights"


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    content: tuple[str, ...] = ()
    published_at: str | None = None
    author: str | None = None


@dataclass(frozen=True)
class BackendResponse:
    provider: str
    results: tuple[SearchResult, ...]
    reported_cost_usd: float | None = None


@dataclass(frozen=True)
class ChainResponse:
    results: tuple[SearchResult, ...]
    responses: tuple[BackendResponse, ...]


ConsumeCall = Callable[[], None]


class SearchBackend(Protocol):
    name: str
    supports_contents: bool
    supports_text: bool

    async def search(self, request: SearchRequest) -> BackendResponse: ...

    async def contents(self, request: ContentsRequest) -> BackendResponse: ...
