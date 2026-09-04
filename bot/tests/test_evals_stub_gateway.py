import asyncio
import json

from evals.stub_gateway import StubBlockedUserStore, StubGateway, install_safe_stubs
from tools.config_spec import KIND_INT, ToolConfigField
from tools.registry import MessageContext, ToolRegistry
from trust.tiers import TrustTier


def _ctx():
    return MessageContext(
        user_id="u",
        user_name="n",
        guild_id="g",
        channel_id="c",
        thread_id=None,
        trust_tier=TrustTier.STAFF,
    )


def test_stub_gateway_channel_context_defaults_empty():
    gateway = StubGateway()
    result = asyncio.run(gateway.collect_recent_channel_context(_ctx(), limit=5))
    assert result == []


def test_install_safe_stubs_replaces_core_writer_with_canned_ack():
    async def real_memory_write(args, ctx):
        raise AssertionError("real memory write must not run during eval")

    registry = ToolRegistry()
    registry.register(
        name="remember_user_memory",
        description="d",
        parameters={"type": "object", "properties": {}},
        handler=real_memory_write,
    )
    install_safe_stubs(registry)
    result = asyncio.run(registry.dispatch("remember_user_memory", {}, _ctx()))
    assert json.loads(result)["status"] == "stubbed"


def test_install_safe_stubs_covers_plugin_declared_writers(monkeypatch):
    """A plugin's production-writing tool stubs out without core naming it."""
    from app import tool_surfaces

    monkeypatch.setattr(tool_surfaces, "_SURFACE_TOOLS", {})

    async def real_report(args, ctx):
        raise AssertionError("real plugin write must not run during eval")

    registry = ToolRegistry()
    registry.register(
        name="plugin_report",
        description="d",
        parameters={"type": "object", "properties": {}},
        handler=real_report,
    )
    tool_surfaces.declare_surface_tools("eval_stub", ["plugin_report"])

    install_safe_stubs(registry)
    result = asyncio.run(registry.dispatch("plugin_report", {}, _ctx()))
    assert json.loads(result)["status"] == "stubbed"


def test_install_safe_stubs_preserves_staff_only_visibility():
    async def real_teach(args, ctx):
        raise AssertionError("real teach must not run during eval")

    registry = ToolRegistry()
    registry.register(
        name="teach",
        description="staff write",
        parameters={"type": "object", "properties": {"content": {"type": "string"}}},
        handler=real_teach,
        min_tier=TrustTier.STAFF,
    )
    install_safe_stubs(registry)

    member_tools = {tool.name for tool in registry.get_tools_for_tier(TrustTier.MEMBER)}
    staff_tools = {tool.name for tool in registry.get_tools_for_tier(TrustTier.STAFF)}
    assert "teach" not in member_tools
    assert "teach" in staff_tools
    result = asyncio.run(registry.dispatch("teach", {"content": "x"}, _ctx()))
    assert json.loads(result)["status"] == "stubbed"


def test_install_safe_stubs_preserves_owner_and_guild_scope():
    async def real_memory_write(args, ctx):
        raise AssertionError("real memory write must not run during eval")

    registry = ToolRegistry(owner_user_id="owner")
    registry.register(
        name="remember_user_memory",
        description="scoped write",
        parameters={},
        handler=real_memory_write,
        owner_only=True,
        guild_ids=frozenset({"allowed"}),
    )
    install_safe_stubs(registry)

    allowed = _ctx()
    allowed.user_id = "owner"
    allowed.guild_id = "allowed"
    wrong_owner = _ctx()
    wrong_owner.guild_id = "allowed"
    wrong_guild = _ctx()
    wrong_guild.user_id = "owner"

    assert (
        json.loads(asyncio.run(registry.dispatch("remember_user_memory", {}, allowed)))["status"]
        == "stubbed"
    )
    assert json.loads(asyncio.run(registry.dispatch("remember_user_memory", {}, wrong_owner))) == {
        "error": "Unknown tool: remember_user_memory"
    }
    assert json.loads(asyncio.run(registry.dispatch("remember_user_memory", {}, wrong_guild))) == {
        "error": "Unknown tool: remember_user_memory"
    }


def test_install_safe_stubs_preserves_config_spec():
    async def real_teach(args, ctx):
        raise AssertionError("real teach must not run during eval")

    spec = (
        ToolConfigField(
            field="limit",
            label="Limit",
            kind=KIND_INT,
            default=2,
            minimum=1,
            maximum=5,
        ),
    )
    registry = ToolRegistry()
    registry.register(
        name="teach",
        description="configured write",
        parameters={},
        handler=real_teach,
        config_spec=spec,
    )

    install_safe_stubs(registry)

    assert registry.config_specs() == {"teach": spec}


def test_stub_blocked_user_store_records_in_memory_only():
    store = StubBlockedUserStore()
    assert asyncio.run(store.block_user("42", blocked_by="42", reason="spam")) is True
    # Re-blocking the same user reports created=False, like a real store.
    assert asyncio.run(store.block_user("42", blocked_by="42", reason="again")) is False
    assert store.blocked == {"42": "again"}


def test_stub_gateway_resolve_member_returns_sticky_fixture():
    from discord_adapter.gateway import MemberLookup, MemberProfile

    gateway = StubGateway()
    # Default: nothing matched.
    assert asyncio.run(gateway.resolve_member(_ctx())).match == "none"

    profile = MemberProfile(
        user_id="42",
        username="bob",
        display_name="Bob",
        is_bot=False,
        avatar_url="",
        account_created_at=None,
        joined_at=None,
        roles=[],
        role_count=0,
        trust_tier="member",
    )
    gateway.set_member_fixture(MemberLookup(match="exact", profile=profile))
    result = asyncio.run(gateway.resolve_member(_ctx(), user_id="42"))
    assert result.match == "exact"
    assert result.profile is not None
    assert result.profile.username == "bob"
