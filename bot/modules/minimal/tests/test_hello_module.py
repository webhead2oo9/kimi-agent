"""A module unit test needs the SDK and pytest, without a running bot."""

import pytest

from hello_module import SPEC
from kimi_agent_module_api import ModuleToolContext, TrustTier
from kimi_agent_module_api.testing import load_context


@pytest.mark.asyncio
async def test_greeting_uses_the_actual_caller() -> None:
    ctx, recorded = load_context(None)
    SPEC.create(ctx)
    tool = recorded.registry.tools["hello_member"]
    caller = ModuleToolContext(
        user_id=123,
        user_name="Alice",
        guild_id=456,
        channel_id=789,
        thread_id=None,
        trust_tier=TrustTier.MEMBER,
        tool_configs={},
    )
    assert await tool.handler({"user_name": "Mallory"}, caller) == "Hello, Alice!"
    assert tool.guild_only
