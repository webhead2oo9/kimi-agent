from __future__ import annotations

import asyncio
import json
from dataclasses import replace

from discord_adapter.gateway import (
    DiscordGatewayError,
    MemberCandidate,
    MemberLookup,
    MemberProfile,
)
from tests.helpers import make_message_context
from tools.member import init_member_lookup_tool
from tools.registry import UNTRUSTED_CONTEXT_NOTE, MessageContext, ToolRegistry
from trust.tiers import TrustTier


class _Gateway:
    def __init__(self, result: MemberLookup | None = None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[dict] = []

    async def resolve_member(
        self,
        ctx: MessageContext,
        *,
        user_id: str | None = None,
        query: str | None = None,
    ) -> MemberLookup:
        self.calls.append({"user_id": user_id, "query": query})
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


def _ctx(activated: set[str] | None = None) -> MessageContext:
    return make_message_context(activated, user_name="Alice")


def _profile() -> MemberProfile:
    return MemberProfile(
        user_id="42",
        username="webhead",
        display_name="Web",
        is_bot=False,
        avatar_url="https://cdn.discordapp.com/avatar.png",
        account_created_at="2019-04-01T12:00:00+00:00",
        joined_at="2021-06-15T09:30:00+00:00",
        roles=["Admin", "Moderator"],
        role_count=14,
        trust_tier="staff",
    )


def test_lookup_member_is_search_only_and_hidden_until_activated() -> None:
    registry = ToolRegistry()
    init_member_lookup_tool(registry, _Gateway(MemberLookup(match="none")))
    entry = next(item for item in registry.get_all_tools() if item.name == "lookup_member")

    visible = [s["name"] for s in registry.get_tool_schemas(TrustTier.MEMBER)]
    assert entry.untrusted is True
    assert "lookup_member" not in visible

    catalog_names = [entry.name for entry in registry.catalog(TrustTier.MEMBER)]
    assert "lookup_member" in catalog_names

    raw = asyncio.run(registry.dispatch("lookup_member", {"query": "web"}, _ctx()))
    assert "browse_tools" in json.loads(raw)["error"]


def test_lookup_member_exact_returns_full_untrusted_member() -> None:
    gateway = _Gateway(MemberLookup(match="exact", profile=_profile()))
    registry = ToolRegistry()
    init_member_lookup_tool(registry, gateway)

    raw = asyncio.run(
        registry.dispatch("lookup_member", {"user_id": "42"}, _ctx({"lookup_member"}))
    )

    assert gateway.calls == [{"user_id": "42", "query": None}]
    assert json.loads(raw) == {
        "match": "exact",
        "context_is_untrusted": True,
        "note": UNTRUSTED_CONTEXT_NOTE,
        "member": {
            "user_id": "42",
            "username": "webhead",
            "display_name": "Web",
            "is_bot": False,
            "avatar_url": "https://cdn.discordapp.com/avatar.png",
            "account_created_at": "2019-04-01T12:00:00+00:00",
            "joined_at": "2021-06-15T09:30:00+00:00",
            "roles": ["Admin", "Moderator"],
            "role_count": 14,
            "trust_tier": "staff",
        },
    }


def test_lookup_member_omits_trust_tier_when_gateway_redacts_it() -> None:
    profile = replace(_profile(), trust_tier=None)
    gateway = _Gateway(MemberLookup(match="exact", profile=profile))
    registry = ToolRegistry()
    init_member_lookup_tool(registry, gateway)

    raw = asyncio.run(
        registry.dispatch("lookup_member", {"user_id": "42"}, _ctx({"lookup_member"}))
    )

    member = json.loads(raw)["member"]
    assert "trust_tier" not in member
    assert member["roles"] == ["Admin", "Moderator"]  # roles are public and stay visible


def test_lookup_member_candidates_returns_slim_list() -> None:
    candidates = [
        MemberCandidate(user_id="1", username="webhead", display_name="Web"),
        MemberCandidate(user_id="2", username="webby", display_name="Webby"),
    ]
    gateway = _Gateway(MemberLookup(match="candidates", candidates=candidates))
    registry = ToolRegistry()
    init_member_lookup_tool(registry, gateway)

    raw = asyncio.run(registry.dispatch("lookup_member", {"query": "web"}, _ctx({"lookup_member"})))

    assert json.loads(raw) == {
        "match": "candidates",
        "context_is_untrusted": True,
        "note": UNTRUSTED_CONTEXT_NOTE,
        "candidates": [
            {"user_id": "1", "username": "webhead", "display_name": "Web"},
            {"user_id": "2", "username": "webby", "display_name": "Webby"},
        ],
    }


def test_lookup_member_no_match_returns_error() -> None:
    gateway = _Gateway(MemberLookup(match="none"))
    registry = ToolRegistry()
    init_member_lookup_tool(registry, gateway)

    raw = asyncio.run(
        registry.dispatch("lookup_member", {"query": "nobody"}, _ctx({"lookup_member"}))
    )

    assert json.loads(raw) == {"error": "No member matched."}


def test_lookup_member_requires_user_id_or_query() -> None:
    gateway = _Gateway(MemberLookup(match="none"))
    registry = ToolRegistry()
    init_member_lookup_tool(registry, gateway)

    raw = asyncio.run(registry.dispatch("lookup_member", {}, _ctx({"lookup_member"})))

    assert json.loads(raw) == {"error": "Provide a user_id or a query."}
    assert gateway.calls == []


def test_lookup_member_gateway_error_returns_safe_error() -> None:
    gateway = _Gateway(error=DiscordGatewayError("Member lookup is only available in a server."))
    registry = ToolRegistry()
    init_member_lookup_tool(registry, gateway)

    raw = asyncio.run(registry.dispatch("lookup_member", {"query": "web"}, _ctx({"lookup_member"})))

    assert json.loads(raw) == {"error": "Member lookup is only available in a server."}
