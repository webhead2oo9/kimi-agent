from __future__ import annotations

import json

import pytest

from tools.plan import MAX_PLAN_STEP_CHARS, MAX_PLAN_STEPS, init_plan_tool
from tools.registry import MessageContext, ToolRegistry
from trust.tiers import TrustTier


def _make_ctx(tier: TrustTier = TrustTier.MEMBER) -> MessageContext:
    return MessageContext(
        user_id="user123",
        user_name="test",
        guild_id="g1",
        channel_id="c1",
        thread_id=None,
        trust_tier=tier,
        context_key="g1:c1:main",
    )


def _registry() -> ToolRegistry:
    reg = ToolRegistry()
    init_plan_tool(reg)
    return reg


@pytest.mark.asyncio
async def test_plan_stores_on_ctx_and_echoes() -> None:
    reg = _registry()
    ctx = _make_ctx()
    result = await reg.dispatch(
        "plan",
        {
            "steps": [
                {"content": "search the web", "status": "in_progress"},
                {"content": "write the summary"},
            ]
        },
        ctx,
    )
    body = json.loads(result)
    assert body["count"] == 2
    assert body["plan"] == [
        {"content": "search the web", "status": "in_progress"},
        {"content": "write the summary", "status": "pending"},
    ]
    # Stashed on per-turn context (never persisted to SQLite).
    assert ctx.plan == body["plan"]


@pytest.mark.asyncio
async def test_plan_accepts_plain_string_steps() -> None:
    reg = _registry()
    ctx = _make_ctx()
    result = await reg.dispatch("plan", {"steps": ["one", "two"]}, ctx)
    assert json.loads(result)["plan"] == [
        {"content": "one", "status": "pending"},
        {"content": "two", "status": "pending"},
    ]


@pytest.mark.asyncio
async def test_plan_replaces_previous_plan() -> None:
    reg = _registry()
    ctx = _make_ctx()
    await reg.dispatch("plan", {"steps": ["old"]}, ctx)
    await reg.dispatch("plan", {"steps": ["new a", "new b"]}, ctx)
    assert [step["content"] for step in ctx.plan] == ["new a", "new b"]


@pytest.mark.asyncio
async def test_plan_rejects_empty_steps() -> None:
    reg = _registry()
    ctx = _make_ctx()
    result = await reg.dispatch("plan", {"steps": []}, ctx)
    assert json.loads(result) == {"error": "steps must be a non-empty list"}
    assert ctx.plan == []


@pytest.mark.asyncio
async def test_plan_rejects_too_many_steps() -> None:
    reg = _registry()
    ctx = _make_ctx()
    steps = [f"step {i}" for i in range(MAX_PLAN_STEPS + 1)]
    result = await reg.dispatch("plan", {"steps": steps}, ctx)
    assert "at most" in json.loads(result)["error"]


@pytest.mark.asyncio
async def test_plan_rejects_unknown_status() -> None:
    reg = _registry()
    ctx = _make_ctx()
    result = await reg.dispatch("plan", {"steps": [{"content": "x", "status": "done"}]}, ctx)
    assert "status must be one of" in json.loads(result)["error"]


@pytest.mark.asyncio
async def test_plan_rejects_empty_content() -> None:
    reg = _registry()
    ctx = _make_ctx()
    result = await reg.dispatch("plan", {"steps": [{"content": "  "}]}, ctx)
    assert "content must not be empty" in json.loads(result)["error"]


@pytest.mark.asyncio
async def test_plan_clips_overlong_content() -> None:
    reg = _registry()
    ctx = _make_ctx()
    long = "x" * (MAX_PLAN_STEP_CHARS + 50)
    result = await reg.dispatch("plan", {"steps": [long]}, ctx)
    content = json.loads(result)["plan"][0]["content"]
    assert content == "x" * MAX_PLAN_STEP_CHARS + "…"


def test_plan_description_tells_model_users_see_the_checklist() -> None:
    # The checklist renders live on the activity surface, so the description must
    # steer the model toward short, user-readable steps (not private scratch).
    reg = _registry()
    [schema] = [s for s in reg.get_tool_schemas(TrustTier.MEMBER) if s["name"] == "plan"]
    assert "user sees" in schema["description"].casefold()
