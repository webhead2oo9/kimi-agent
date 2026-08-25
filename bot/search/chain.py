from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Sequence
from typing import TypeVar

from search.normalize import canonical_url
from search.types import (
    BackendResponse,
    ChainResponse,
    ConsumeCall,
    ContentsRequest,
    SearchBackend,
    SearchBudgetExceeded,
    SearchProviderError,
    SearchRequest,
    SearchResult,
)

log = logging.getLogger(__name__)
T = TypeVar("T")


class SearchChain:
    def __init__(self, backends: Sequence[SearchBackend], *, timeout_seconds: float) -> None:
        self._backends = tuple(backends)
        self._timeout_seconds = timeout_seconds

    async def search(
        self,
        request: SearchRequest,
        *,
        strategy: str,
        consume_call: ConsumeCall,
    ) -> ChainResponse:
        eligible = tuple(
            backend
            for backend in self._backends
            if request.content_mode != "text" or backend.supports_text
        )
        if not eligible:
            raise SearchProviderError("No configured search backend supports this request.")
        if strategy == "failover":
            return await self._failover_search(eligible, request, consume_call)
        return await self._blend_search(eligible, request, consume_call)

    async def contents(
        self,
        request: ContentsRequest,
        *,
        consume_call: ConsumeCall,
    ) -> ChainResponse:
        eligible = tuple(
            backend
            for backend in self._backends
            if backend.supports_contents
            and (request.content_mode != "text" or backend.supports_text)
        )
        if not eligible:
            raise SearchProviderError("No page-reading provider is configured.")
        errors: list[Exception] = []
        for backend in eligible:
            consume_call()
            try:
                response = await self._call(backend.contents(request))
                return ChainResponse(response.results, (response,))
            except SearchBudgetExceeded:
                raise
            except Exception as exc:
                errors.append(exc)
                log.warning(
                    "Search backend %s failed while reading pages", backend.name, exc_info=exc
                )
        raise SearchProviderError("Page reading is temporarily unavailable.") from errors[-1]

    async def _failover_search(
        self,
        backends: tuple[SearchBackend, ...],
        request: SearchRequest,
        consume_call: ConsumeCall,
    ) -> ChainResponse:
        errors: list[Exception] = []
        for backend in backends:
            consume_call()
            try:
                response = await self._call(backend.search(request))
                return ChainResponse(response.results[: request.num_results], (response,))
            except SearchBudgetExceeded:
                raise
            except Exception as exc:
                errors.append(exc)
                log.warning("Search backend %s failed", backend.name, exc_info=exc)
        raise SearchProviderError("Internet search is temporarily unavailable.") from errors[-1]

    async def _blend_search(
        self,
        backends: tuple[SearchBackend, ...],
        request: SearchRequest,
        consume_call: ConsumeCall,
    ) -> ChainResponse:
        async def run(backend: SearchBackend) -> BackendResponse:
            consume_call()
            return await self._call(backend.search(request))

        outcomes = await asyncio.gather(
            *(run(backend) for backend in backends), return_exceptions=True
        )
        for backend, outcome in zip(backends, outcomes, strict=True):
            if isinstance(outcome, Exception) and not isinstance(outcome, SearchBudgetExceeded):
                log.warning("Search backend %s failed", backend.name, exc_info=outcome)
        responses = tuple(item for item in outcomes if isinstance(item, BackendResponse))
        if not responses:
            budget_error = next(
                (item for item in outcomes if isinstance(item, SearchBudgetExceeded)), None
            )
            if budget_error is not None:
                raise budget_error
            raise SearchProviderError("Internet search is temporarily unavailable.")
        merged = _round_robin_results(responses, request.num_results)
        return ChainResponse(merged, responses)

    async def _call(self, awaitable: Awaitable[T]) -> T:
        async with asyncio.timeout(self._timeout_seconds):
            return await awaitable


def _round_robin_results(
    responses: tuple[BackendResponse, ...], limit: int
) -> tuple[SearchResult, ...]:
    output: list[SearchResult] = []
    seen: set[str] = set()
    # Backends are ordered by authority. Reserve every URL returned by a
    # higher-ranked backend before interleaving so a lower-ranked duplicate
    # cannot win merely because it appeared earlier in its own result list.
    reserved_by_higher: list[set[str]] = []
    reserved: set[str] = set()
    for response in responses:
        reserved_by_higher.append(set(reserved))
        reserved.update(
            identity for result in response.results if (identity := canonical_url(result.url))
        )
    max_length = max((len(response.results) for response in responses), default=0)
    for index in range(max_length):
        for response_index, response in enumerate(responses):
            if index >= len(response.results):
                continue
            result = response.results[index]
            identity = canonical_url(result.url)
            if identity and identity in reserved_by_higher[response_index]:
                continue
            if identity and identity in seen:
                continue
            if identity:
                seen.add(identity)
            output.append(result)
            if len(output) >= limit:
                return tuple(output)
    return tuple(output)
