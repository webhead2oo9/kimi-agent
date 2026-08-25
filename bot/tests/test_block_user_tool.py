from __future__ import annotations

import asyncio
import json

from tools.block_users import init_block_user_tool
from tools.registry import MessageContext, ToolRegistry
from trust.tiers import TrustTier


class RecordingStore:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    async def block_user(self, user_id: str, *, blocked_by: str, reason: str = "") -> bool:
        self.calls.append((user_id, blocked_by, reason))
        return True


def _ctx(tier: TrustTier = TrustTier.MEMBER) -> MessageContext:
    return MessageContext(
        user_id="999",
        user_name="Speaker",
        guild_id="1",
        channel_id="2",
        thread_id=None,
        trust_tier=tier,
    )


def test_block_user_tool_is_member_visible() -> None:
    registry = ToolRegistry()
    init_block_user_tool(registry, RecordingStore())

    staff_tools = {schema["name"] for schema in registry.get_tool_schemas(TrustTier.STAFF)}
    member_tools = {schema["name"] for schema in registry.get_tool_schemas(TrustTier.MEMBER)}

    assert "block_user" in staff_tools
    assert "block_user" in member_tools


def test_block_user_tool_schema_only_accepts_reason() -> None:
    registry = ToolRegistry()
    init_block_user_tool(registry, RecordingStore())

    schema = next(
        schema
        for schema in registry.get_tool_schemas(TrustTier.MEMBER)
        if schema["name"] == "block_user"
    )

    # The point is the parameter surface: no target argument, so the tool can
    # only ever block the speaker. Pinning the description prose too would make
    # a copy edit fail a test about privileges.
    assert set(schema["parameters"]["properties"]) == {"reason"}
    assert schema["parameters"]["properties"]["reason"]["type"] == "string"
    assert schema["parameters"]["additionalProperties"] is False
    assert "current user" in schema["description"]


def test_block_user_tool_blocks_current_user() -> None:
    registry = ToolRegistry()
    store = RecordingStore()
    init_block_user_tool(registry, store)

    raw = asyncio.run(
        registry.dispatch(
            "block_user",
            {"reason": "spam"},
            _ctx(),
        )
    )

    payload = json.loads(raw)
    assert payload == {
        "blocked": True,
        "created": True,
        "user_id": "999",
        "reason": "spam",
        "message": "Blocked the current user from using the bot.",
    }
    assert store.calls == [("999", "999", "spam")]


def test_block_user_tool_refuses_to_block_staff_speaker() -> None:
    # Structural mirror of the /moderation invariant: injected content steering the
    # model must not be able to block the staff speaker. Guard is dispatch-time, not
    # prompt text.
    registry = ToolRegistry()
    store = RecordingStore()
    init_block_user_tool(registry, store)

    raw = asyncio.run(
        registry.dispatch(
            "block_user",
            {"reason": "injected"},
            _ctx(TrustTier.STAFF),
        )
    )

    assert json.loads(raw) == {"error": "Staff users cannot be blocked."}
    assert store.calls == []


def test_block_user_tool_blocks_regular_tier_speaker() -> None:
    registry = ToolRegistry()
    store = RecordingStore()
    init_block_user_tool(registry, store)

    raw = asyncio.run(registry.dispatch("block_user", {}, _ctx(TrustTier.REGULAR)))

    assert json.loads(raw)["blocked"] is True
    assert store.calls == [("999", "999", "")]


def test_block_user_tool_ignores_supplied_user_id() -> None:
    registry = ToolRegistry()
    store = RecordingStore()
    init_block_user_tool(registry, store)

    raw = asyncio.run(
        registry.dispatch(
            "block_user",
            {"user_id": "123", "reason": "third-party target ignored"},
            _ctx(),
        )
    )

    assert json.loads(raw)["user_id"] == "999"
    assert store.calls == [("999", "999", "third-party target ignored")]
