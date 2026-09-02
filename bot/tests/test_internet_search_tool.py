from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest
from pydantic import SecretStr

from search.chain import SearchChain
from search.types import BackendResponse, ContentsRequest, SearchRequest, SearchResult
from app.tools import _register_internet_search
from config.settings import Settings
from tools.config_spec import default_config
from tools.internet_search import TOOL_NAME, InternetSearchConfig, init_internet_search_tool
from tools.registry import BudgetName, MessageContext, ToolRegistry, TurnBudget
from trust.tiers import TrustTier


@dataclass
class FakeBackend:
    name: str
    results: tuple[SearchResult, ...]
    cost: float | None = None
    supports_contents: bool = True
    supports_text: bool = True
    supports_contents_text: bool = True
    search_requests: list[SearchRequest] = field(default_factory=list)

    async def search(self, request: SearchRequest) -> BackendResponse:
        self.search_requests.append(request)
        return BackendResponse(self.name, self.results, self.cost)

    async def contents(self, request: ContentsRequest) -> BackendResponse:
        return BackendResponse(self.name, self.results, self.cost)


class RecordingUsageStore:
    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def record_paid_usage(self, **kwargs: Any) -> None:
        self.calls.extend(kwargs["calls"])


def _context(
    *,
    usage_store: Any = None,
    strategy: str = "blend",
    budget_cap: int = 10,
) -> MessageContext:
    return MessageContext(
        user_id="u1",
        user_name="Tester",
        guild_id="g1",
        channel_id="c1",
        thread_id=None,
        trust_tier=TrustTier.MEMBER,
        usage_store=usage_store,
        tool_configs={TOOL_NAME: {"strategy": strategy}},
        budget=TurnBudget(caps={BudgetName.INTERNET_SEARCH_BACKEND_CALLS: budget_cap}),
    )


def _registry(
    *backends: FakeBackend,
    limit: int = 10,
    max_results: int = 10,
) -> ToolRegistry:
    registry = ToolRegistry()
    init_internet_search_tool(
        registry,
        InternetSearchConfig(
            chain=SearchChain(backends, timeout_seconds=1),
            max_results=max_results,
            max_backend_calls_per_turn=limit,
            fallback_cost_usd={("brave", "search"): 0.004},
        ),
    )
    return registry


def test_tool_is_core_member_surface_with_blend_default() -> None:
    registry = _registry(FakeBackend("exa", ()))
    entry = next(item for item in registry.get_all_tools() if item.name == TOOL_NAME)

    assert entry.searchable is False
    assert entry.min_tier is TrustTier.MEMBER
    assert entry.untrusted is True
    assert default_config(registry.config_specs()[TOOL_NAME]) == {"strategy": "blend"}


def test_registration_follows_provider_keys() -> None:
    disabled = ToolRegistry()
    _register_internet_search(
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            exa_api_key=SecretStr(""),
            brave_api_key=SecretStr(""),
        ),
        disabled,
    )
    assert not disabled.is_registered(TOOL_NAME)

    exa_only = ToolRegistry()
    _register_internet_search(
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            exa_api_key=SecretStr("exa-secret"),
            brave_api_key=SecretStr(""),
        ),
        exa_only,
    )
    assert exa_only.is_registered(TOOL_NAME)


@pytest.mark.asyncio
async def test_configured_result_cap_is_schema_maximum_and_request_default() -> None:
    backend = FakeBackend("exa", ())
    registry = _registry(backend, max_results=4)
    entry = next(item for item in registry.get_all_tools() if item.name == TOOL_NAME)

    assert entry.parameters["properties"]["num_results"]["maximum"] == 4
    await registry.dispatch(
        TOOL_NAME,
        {"mode": "search", "query": "bounded"},
        _context(strategy="failover"),
    )
    assert backend.search_requests[0].num_results == 4

    rejected = json.loads(
        await registry.dispatch(
            TOOL_NAME,
            {"mode": "search", "query": "bounded", "num_results": 5},
            _context(strategy="failover"),
        )
    )
    assert rejected["error"] == "num_results must be between 1 and 4"


@pytest.mark.asyncio
async def test_output_is_clean_and_records_reported_and_fallback_costs() -> None:
    exa = FakeBackend(
        "exa",
        (
            SearchResult(
                title="Exa result",
                url="https://example.com/a",
                content=("Useful context",),
                author="Writer",
            ),
        ),
        cost=0.012,
    )
    brave = FakeBackend(
        "brave",
        (SearchResult(title="Brave result", url="https://other.example/b"),),
    )
    usage = RecordingUsageStore()
    ctx = _context(usage_store=usage)

    raw = await _registry(exa, brave).dispatch(
        TOOL_NAME, {"mode": "search", "query": "new information"}, ctx
    )
    payload = json.loads(raw)

    assert payload["results"] == [
        {
            "title": "Exa result",
            "url": "https://example.com/a",
            "author": "Writer",
            "content": "Useful context",
        },
        {"title": "Brave result", "url": "https://other.example/b"},
    ]
    assert payload["context_is_untrusted"] is True
    assert "provider" not in raw
    assert "cost" not in raw
    assert [call.cost_usd for call in usage.calls] == [0.012, 0.004]
    assert ctx.budget_used(BudgetName.INTERNET_SEARCH_BACKEND_CALLS) == 2


@pytest.mark.asyncio
async def test_zero_matches_is_success_not_tool_failure() -> None:
    raw = await _registry(FakeBackend("exa", ())).dispatch(
        TOOL_NAME, {"mode": "search", "query": "no such thing"}, _context()
    )
    payload = json.loads(raw)

    assert payload["results"] == []
    assert payload["message"] == "No matching results found."
    assert "error" not in payload


@pytest.mark.asyncio
async def test_ten_call_budget_is_per_message_context() -> None:
    registry = _registry(FakeBackend("exa", ()), limit=10)
    ctx = _context(strategy="failover")
    args = {"mode": "search", "query": "repeatable"}

    for _ in range(10):
        assert "error" not in json.loads(await registry.dispatch(TOOL_NAME, args, ctx))
    assert json.loads(await registry.dispatch(TOOL_NAME, args, ctx))["error"] == (
        "Internet search call limit reached for this turn."
    )

    fresh = _context(strategy="failover")
    assert "error" not in json.loads(await registry.dispatch(TOOL_NAME, args, fresh))


@pytest.mark.asyncio
async def test_contents_requires_urls_and_search_query_respects_brave_limits() -> None:
    registry = _registry(FakeBackend("exa", ()))
    missing = json.loads(await registry.dispatch(TOOL_NAME, {"mode": "contents"}, _context()))
    too_long = json.loads(
        await registry.dispatch(
            TOOL_NAME,
            {"mode": "search", "query": " ".join(["word"] * 51)},
            _context(),
        )
    )

    assert missing["error"] == "urls is required when mode is contents."
    assert too_long["error"] == "query must be 50 words or fewer."
