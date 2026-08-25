from __future__ import annotations

import asyncio
import json

import pytest

from agent.context import ConversationContext
from agent.core import ConversationRunRequest, run_conversation
from providers.base import LLMProvider
from providers.types import ProviderRequest, ProviderResponse, ToolCall
from tools.registry import MessageContext, ToolRegistry
from trust.tiers import TrustTier


class ScriptedProvider(LLMProvider):
    def __init__(self) -> None:
        self.calls = 0

    async def run_turn(self, request: ProviderRequest) -> ProviderResponse:
        self.calls += 1
        if self.calls == 1:
            return ProviderResponse(
                content="Using the resource.",
                tool_calls=[
                    ToolCall(id="one", name="lease", arguments={}),
                    ToolCall(id="two", name="lease", arguments={}),
                ],
            )
        return ProviderResponse(content="done")


@pytest.mark.asyncio
async def test_turn_finalizer_is_idempotent_and_runs_after_react_loop() -> None:
    events: list[str] = []
    registry = ToolRegistry()

    async def finalize() -> None:
        events.append("finalized")

    async def lease(args: dict, ctx: MessageContext) -> str:
        added = ctx.add_turn_finalizer("shared-lease", finalize)
        events.append(f"tool:{added}")
        return json.dumps({"ok": True})

    registry.register(
        name="lease",
        description="lease",
        parameters={"type": "object", "properties": {}},
        handler=lease,
    )
    result = await run_conversation(
        request=ConversationRunRequest(
            user_message="go",
            context=ConversationContext(key="root", db_conversation_id=1),
            trust_tier=TrustTier.MEMBER,
            user_name="User",
            user_id="1",
            provider=ScriptedProvider(),
            registry=registry,
        )
    )

    assert result.text == "done"
    assert events == ["tool:True", "tool:False", "finalized"]


@pytest.mark.asyncio
async def test_cancellation_waits_for_started_finalizer_drain() -> None:
    events: list[str] = []
    drain_started = asyncio.Event()
    allow_drain = asyncio.Event()
    registry = ToolRegistry()
    registered = False

    async def first_finalizer() -> None:
        events.append("first")

    async def blocking_finalizer() -> None:
        events.append("blocking:start")
        drain_started.set()
        await allow_drain.wait()
        events.append("blocking:end")

    async def lease(args: dict, ctx: MessageContext) -> str:
        nonlocal registered
        if not registered:
            registered = True
            ctx.add_turn_finalizer("first", first_finalizer)
            ctx.add_turn_finalizer("blocking", blocking_finalizer)
        return json.dumps({"ok": True})

    registry.register(
        name="lease",
        description="lease",
        parameters={"type": "object", "properties": {}},
        handler=lease,
    )
    turn = asyncio.create_task(
        run_conversation(
            request=ConversationRunRequest(
                user_message="go",
                context=ConversationContext(key="root", db_conversation_id=1),
                trust_tier=TrustTier.MEMBER,
                user_name="User",
                user_id="1",
                provider=ScriptedProvider(),
                registry=registry,
            )
        )
    )

    await asyncio.wait_for(drain_started.wait(), timeout=1.0)
    try:
        turn.cancel()
        await asyncio.sleep(0)
        assert not turn.done()
        # A shutdown path may cancel an already-cancelling turn again. The
        # independent finalizer drain must still retain its resource leases.
        turn.cancel()
        await asyncio.sleep(0)
        assert not turn.done()
    finally:
        allow_drain.set()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(turn, timeout=1.0)
    assert events == ["blocking:start", "blocking:end", "first"]
