import json

import pytest

from tools.browse import init_browse_tools
from tools.registry import MessageContext, ToolRegistry
from trust.tiers import TrustTier


async def _noop_handler(args: dict, ctx: MessageContext) -> str:
    return json.dumps({"ok": True})


def _make_ctx(
    *,
    tier: TrustTier = TrustTier.MEMBER,
    channel_id: str = "c1",
    activated: set[str] | None = None,
    blocked: frozenset[str] = frozenset(),
) -> MessageContext:
    return MessageContext(
        user_id="123",
        user_name="test",
        guild_id="g1",
        channel_id=channel_id,
        thread_id=None,
        trust_tier=tier,
        activated_tools=activated or set(),
        blocked_tools=blocked,
    )


def _registry_with_browse() -> ToolRegistry:
    reg = ToolRegistry()
    init_browse_tools(reg)
    reg.register(
        "lookup_member",
        "Look up a server member",
        {"type": "object", "properties": {"query": {"type": "string"}}},
        _noop_handler,
        searchable=True,
        category="Discord",
    )
    reg.register(
        "scholar_lookup",
        "Search scholarly metadata",
        {"type": "object", "properties": {"query": {"type": "string"}}},
        _noop_handler,
        searchable=True,
        category="Research",
    )
    reg.register(
        "staff_report",
        "Generate a staff report",
        {"type": "object", "properties": {}},
        _noop_handler,
        min_tier=TrustTier.STAFF,
        searchable=True,
        category="Admin",
    )
    reg.register(
        "extra_lookup",
        "Another searchable lookup",
        {"type": "object", "properties": {}},
        _noop_handler,
        searchable=True,
        category="Discord",
    )
    return reg


@pytest.mark.asyncio
async def test_browse_catalog_groups_visible_tools_without_schemas() -> None:
    reg = _registry_with_browse()

    raw = await reg.dispatch("browse_tools", {}, _make_ctx())
    parsed = json.loads(raw)

    assert parsed["count"] == 3
    assert list(parsed["categories"]) == ["Discord", "Research"]
    assert parsed["categories"]["Discord"] == [
        {"name": "extra_lookup", "description": "Another searchable lookup"},
        {"name": "lookup_member", "description": "Look up a server member"},
    ]
    assert parsed["categories"]["Research"] == [
        {"name": "scholar_lookup", "description": "Search scholarly metadata"}
    ]
    assert "staff_report" not in json.dumps(parsed)
    assert "properties" not in json.dumps(parsed)
    assert "parameters" not in json.dumps(parsed)


@pytest.mark.asyncio
async def test_browse_load_activates_exact_visible_names_and_suggests_typos() -> None:
    reg = _registry_with_browse()
    ctx = _make_ctx()

    raw = await reg.dispatch(
        "browse_tools",
        {"load": ["lookup_member", "lookpu_member", "staff_report", "extra_lookup"]},
        ctx,
    )
    parsed = json.loads(raw)

    assert parsed["loaded"] == ["lookup_member", "extra_lookup"]
    assert parsed["unknown"] == ["lookpu_member", "staff_report"]
    assert parsed["did_you_mean"] == {"lookpu_member": "lookup_member"}
    assert ctx.activated_tools == {"lookup_member", "extra_lookup"}


@pytest.mark.asyncio
async def test_browse_hides_and_refuses_to_load_blocked_tool() -> None:
    # An operator-blocked searchable tool must not appear in the catalog and must
    # not be loadable via browse_tools: it reports as unknown, with no name leak
    # (no did_you_mean suggestion pointing at it) and no activation.
    reg = _registry_with_browse()
    ctx = _make_ctx(blocked=frozenset({"scholar_lookup"}))

    catalog = json.loads(await reg.dispatch("browse_tools", {}, ctx))
    assert "scholar_lookup" not in json.dumps(catalog)

    loaded = json.loads(
        await reg.dispatch(
            "browse_tools",
            {"load": ["scholar_lookup", "lookup_member"]},
            ctx,
        )
    )
    assert loaded["loaded"] == ["lookup_member"]
    assert loaded["unknown"] == ["scholar_lookup"]
    assert "scholar_lookup" not in loaded["did_you_mean"].values()
    assert "scholar_lookup" not in ctx.activated_tools

    # And even if it were somehow activated, dispatch still masks it.
    ctx.activated_tools.add("scholar_lookup")
    masked = json.loads(await reg.dispatch("scholar_lookup", {"query": "x"}, ctx))
    assert masked == {"error": "Unknown tool: scholar_lookup"}


@pytest.mark.asyncio
async def test_loaded_searchable_tool_schema_appears_and_dispatches() -> None:
    reg = _registry_with_browse()
    ctx = _make_ctx()

    await reg.dispatch("browse_tools", {"load": ["lookup_member"]}, ctx)

    schemas = reg.get_tool_schemas(
        TrustTier.MEMBER,
        activated=ctx.activated_tools,
    )
    assert "lookup_member" in {schema["name"] for schema in schemas}

    raw = await reg.dispatch("lookup_member", {"query": "Alice"}, ctx)
    assert json.loads(raw) == {"ok": True}


def test_registry_catalog_matches_load_visibility_boundaries() -> None:
    reg = _registry_with_browse()

    member_names = [entry.name for entry in reg.catalog(TrustTier.MEMBER)]
    staff_names = [entry.name for entry in reg.catalog(TrustTier.STAFF)]

    assert member_names == ["extra_lookup", "lookup_member", "scholar_lookup"]
    assert staff_names == [
        "extra_lookup",
        "lookup_member",
        "scholar_lookup",
        "staff_report",
    ]
    assert reg.get_searchable_entry("lookup_member", TrustTier.MEMBER) is not None
    assert reg.get_searchable_entry("staff_report", TrustTier.MEMBER) is None
