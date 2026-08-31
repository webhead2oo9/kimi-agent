from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import SecretStr

from app.tools import _register_x_search
from config.settings import Settings
from tools.registry import MessageContext, ToolRegistry
from tools.x_search import TOOL_NAME, XSearchConfig, init_x_search_tool
from trust.tiers import TrustTier
from xai.credentials import XaiCredential, XaiCredentialResolver
from xai.responses import XaiResponsesResult


class FakeManager:
    def is_available(self) -> bool:
        return True

    async def get_access_token(self) -> str:
        return "oauth"

    async def refresh_tokens(self, *, force: bool = False) -> None:
        return None


class FakeClient:
    def __init__(self, results: list[XaiResponsesResult]) -> None:
        self.results = results
        self.calls: list[dict[str, Any]] = []

    async def create(
        self,
        payload: dict[str, Any],
        *,
        credential: XaiCredential | None = None,
        allow_auth_fallback: bool = True,
        consume_call: Any = None,
    ) -> XaiResponsesResult:
        if consume_call is not None:
            consume_call()
        self.calls.append(
            {
                "payload": payload,
                "credential": credential,
                "allow_auth_fallback": allow_auth_fallback,
            }
        )
        return self.results.pop(0)


def _resolver(mode: str = "auto") -> XaiCredentialResolver:
    return XaiCredentialResolver(
        auth_mode=mode,
        oauth_manager=FakeManager(),  # type: ignore[arg-type]
        api_key="paid-key",
    )


def _context() -> MessageContext:
    return MessageContext(
        user_id="u1",
        user_name="Tester",
        guild_id="g1",
        channel_id="c1",
        thread_id=None,
        trust_tier=TrustTier.MEMBER,
        activated_tools={TOOL_NAME},
        usage_sink=[],
    )


def _registry(
    results: list[XaiResponsesResult],
    *,
    mode: str = "auto",
    max_calls: int = 10,
) -> tuple[ToolRegistry, FakeClient]:
    resolver = _resolver(mode)
    client = FakeClient(results)
    registry = ToolRegistry()
    init_x_search_tool(
        registry,
        XSearchConfig(
            client=client,  # type: ignore[arg-type]
            credential_resolver=resolver,
            model="grok-4.6",
            max_calls_per_turn=max_calls,
        ),
    )
    return registry, client


def _result(
    *,
    source: str = "oauth",
    calls: int | None = 1,
    citations: list[Any] | None = None,
    answer: str = "answer",
) -> XaiResponsesResult:
    usage: dict[str, Any] = {"input_tokens": 5, "output_tokens": 7}
    if calls is not None:
        usage["server_side_tool_usage_details"] = {"x_search_calls": calls}
    return XaiResponsesResult(
        {
            "output_text": answer,
            "citations": citations or [],
            "usage": usage,
        },
        source,
    )


def test_x_search_is_member_searchable_with_full_filter_schema() -> None:
    registry, _client = _registry([_result()])
    entry = next(item for item in registry.get_all_tools() if item.name == TOOL_NAME)

    assert entry.searchable is True
    assert entry.min_tier is TrustTier.MEMBER
    assert set(entry.parameters["properties"]) == {
        "query",
        "allowed_x_handles",
        "excluded_x_handles",
        "from_date",
        "to_date",
        "enable_image_understanding",
        "enable_video_understanding",
    }
    assert entry.parameters["properties"]["allowed_x_handles"]["maxItems"] == 20


@pytest.mark.asyncio
async def test_full_filters_are_sent_to_hosted_x_search() -> None:
    registry, client = _registry([_result(citations=["https://x.com/post/1"])])
    raw = await registry.dispatch(
        TOOL_NAME,
        {
            "query": "what happened",
            "allowed_x_handles": ["@openai", "xai"],
            "from_date": "2026-01-01",
            "to_date": "2026-01-02",
            "enable_image_understanding": True,
            "enable_video_understanding": True,
        },
        _context(),
    )

    payload = client.calls[0]["payload"]
    assert payload == {
        "model": "grok-4.6",
        "input": [{"role": "user", "content": "what happened"}],
        "tools": [
            {
                "type": "x_search",
                "allowed_x_handles": ["openai", "xai"],
                "from_date": "2026-01-01",
                "to_date": "2026-01-02",
                "enable_image_understanding": True,
                "enable_video_understanding": True,
            }
        ],
        "store": False,
    }
    assert json.loads(raw)["degraded"] is False


@pytest.mark.asyncio
async def test_auto_uses_api_key_after_degraded_oauth_and_records_both_calls() -> None:
    registry, client = _registry(
        [
            _result(source="oauth", calls=0, citations=[]),
            _result(source="api_key", calls=1, citations=["https://x.com/post/1"]),
        ]
    )
    ctx = _context()

    raw = await registry.dispatch(TOOL_NAME, {"query": "latest update"}, ctx)
    parsed = json.loads(raw)

    assert parsed["degraded"] is False
    assert parsed["citations"] == [{"url": "https://x.com/post/1"}]
    assert len(client.calls) == 2
    assert client.calls[1]["credential"].source == "api_key"
    assert client.calls[1]["allow_auth_fallback"] is False
    assert ctx.x_search_calls_this_turn == 2
    assert ctx.usage_sink is not None and len(ctx.usage_sink) == 2
    assert all(call.role == "x_search" for call in ctx.usage_sink)


@pytest.mark.asyncio
async def test_strict_oauth_returns_degraded_result_without_api_fallback() -> None:
    registry, client = _registry([_result(source="oauth", calls=None)], mode="oauth")

    raw = await registry.dispatch(TOOL_NAME, {"query": "latest update"}, _context())
    parsed = json.loads(raw)

    assert parsed["degraded"] is True
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_positive_search_call_count_allows_legitimate_empty_result() -> None:
    registry, client = _registry([_result(source="oauth", calls=1, citations=[])])

    raw = await registry.dispatch(TOOL_NAME, {"query": "nothing matches"}, _context())

    assert json.loads(raw)["degraded"] is False
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_completed_x_search_output_item_is_live_search_evidence() -> None:
    resolver = _resolver()
    client = FakeClient(
        [
            XaiResponsesResult(
                {
                    "output": [
                        {
                            "id": "search_1",
                            "type": "x_search_call",
                            "status": "completed",
                            "action": {"type": "search", "query": "nothing matches"},
                        },
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": "No posts found."}],
                        },
                    ],
                    "usage": {"input_tokens": 5, "output_tokens": 7},
                },
                "oauth",
            )
        ]
    )
    registry = ToolRegistry()
    init_x_search_tool(
        registry,
        XSearchConfig(
            client=client,  # type: ignore[arg-type]
            credential_resolver=resolver,
            model="grok-4.6",
        ),
    )

    raw = await registry.dispatch(TOOL_NAME, {"query": "nothing matches"}, _context())
    parsed = json.loads(raw)

    assert parsed["degraded"] is False
    assert parsed["x_search_calls"] == 1
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_budget_counts_upstream_requests_across_tool_invocations() -> None:
    registry, client = _registry([_result(), _result()], max_calls=1)
    ctx = _context()

    first = json.loads(await registry.dispatch(TOOL_NAME, {"query": "first"}, ctx))
    second = json.loads(await registry.dispatch(TOOL_NAME, {"query": "second"}, ctx))

    assert "error" not in first
    assert second["error"] == "X search call limit reached for this turn."
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_invalid_filters_fail_before_an_upstream_call() -> None:
    registry, client = _registry([_result()])

    raw = await registry.dispatch(
        TOOL_NAME,
        {
            "query": "query",
            "allowed_x_handles": ["one"],
            "excluded_x_handles": ["two"],
        },
        _context(),
    )

    assert "cannot be used together" in json.loads(raw)["error"]
    assert client.calls == []


def test_registration_is_explicitly_enabled_and_credential_gated() -> None:
    disabled = ToolRegistry()
    _register_x_search(Settings(_env_file=None), disabled)  # type: ignore[call-arg]
    assert not disabled.is_registered(TOOL_NAME)

    enabled = ToolRegistry()
    _register_x_search(
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            x_search_enabled=True,
            x_search_auth_mode="api_key",
            grok_api_key=SecretStr("paid-key"),
        ),
        enabled,
    )
    assert enabled.is_registered(TOOL_NAME)


@pytest.mark.asyncio
async def test_output_cap_handles_oversized_citations() -> None:
    resolver = _resolver()
    client = FakeClient(
        [
            _result(
                citations=[
                    {
                        "url": f"https://x.com/post/{index}",
                        "title": "citation " + "x" * 600,
                    }
                    for index in range(20)
                ],
                answer="answer " * 1_000,
            )
        ]
    )
    registry = ToolRegistry()
    init_x_search_tool(
        registry,
        XSearchConfig(
            client=client,  # type: ignore[arg-type]
            credential_resolver=resolver,
            model="grok-4.6",
            max_output_chars=1_000,
        ),
    )

    raw = await registry.dispatch(TOOL_NAME, {"query": "latest update"}, _context())

    assert len(raw) <= 1_000
    assert json.loads(raw)["context_is_untrusted"] is True


def test_strict_oauth_registration_ignores_present_api_key(tmp_path: Any) -> None:
    registry = ToolRegistry()
    _register_x_search(
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            x_search_enabled=True,
            x_search_auth_mode="oauth",
            xai_oauth_token_file=str(tmp_path / "missing.json"),
            grok_api_key=SecretStr("paid-key"),
        ),
        registry,
    )

    assert not registry.is_registered(TOOL_NAME)


def test_auto_does_not_register_with_unreadable_oauth_path_and_api_key(tmp_path: Any) -> None:
    token_path = tmp_path / "directory-instead-of-token-file"
    token_path.mkdir()
    registry = ToolRegistry()

    _register_x_search(
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            x_search_enabled=True,
            x_search_auth_mode="auto",
            xai_oauth_token_file=str(token_path),
            grok_api_key=SecretStr("paid-key"),
        ),
        registry,
    )

    assert not registry.is_registered(TOOL_NAME)
