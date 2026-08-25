import pytest

import agent.core as core
from agent.context import ConversationContext
from agent.core import ConversationRunRequest, run_conversation
from providers.types import ProviderResponse
from tools.registry import ToolRegistry
from trust.tiers import TrustTier


class _CapturingProvider:
    """Minimal provider that records the system prompt it is handed and stops."""

    provider_key = "test"
    model = "test-model"
    capabilities: set = set()

    def __init__(self) -> None:
        self.system_prompt: str | None = None

    async def run_turn(self, request, **kwargs):
        self.system_prompt = request.system_prompt
        return ProviderResponse(content="ok", tool_calls=[])


@pytest.mark.asyncio
async def test_command_template_reaches_system_prompt(monkeypatch):
    seen: dict[str, str | None] = {}

    def fake_build_system_prompt(**kwargs):
        seen["command_template"] = kwargs.get("command_template")
        return "template prompt"

    monkeypatch.setattr(core, "build_system_prompt", fake_build_system_prompt)
    provider = _CapturingProvider()
    ctx = ConversationContext(key="k")
    result = await run_conversation(
        request=ConversationRunRequest(
            user_message="hi",
            context=ctx,
            trust_tier=TrustTier.MEMBER,
            user_name="u",
            user_id="1",
            provider=provider,
            registry=ToolRegistry(),
            max_iterations=1,
            command_template="translate",
        )
    )
    assert result.text == "ok"
    assert seen["command_template"] == "translate"
    assert provider.system_prompt is not None


@pytest.mark.asyncio
async def test_thread_scope_reaches_system_prompt(monkeypatch):
    """The last hop of the thread-instructions chain.

    Everything upstream of ``build_system_prompt`` is plain field copying, so
    without this the whole feature can be disabled by deleting one kwarg and the
    suite still passes: the thread simply falls back to the channel branch,
    which is exactly the bug the scopes exist to fix.
    """
    seen: dict[str, object] = {}

    def fake_build_system_prompt(**kwargs):
        seen.update(kwargs)
        return "template prompt"

    monkeypatch.setattr(core, "build_system_prompt", fake_build_system_prompt)
    result = await run_conversation(
        request=ConversationRunRequest(
            user_message="hi",
            context=ConversationContext(key="k"),
            trust_tier=TrustTier.MEMBER,
            user_name="u",
            user_id="1",
            provider=_CapturingProvider(),
            registry=ToolRegistry(),
            max_iterations=1,
            channel_id="77",
            thread_id="77",
            parent_channel_id="20",
        )
    )
    assert result.text == "ok"
    # channel_id stays the thread's own id (its full-template rung precedes the parent);
    # the parent and thread ids ride alongside it for inherited prompt resolution.
    assert seen["channel_id"] == "77"
    assert seen["thread_id"] == "77"
    assert seen["parent_channel_id"] == "20"


@pytest.mark.asyncio
async def test_non_thread_turn_passes_no_thread_scope(monkeypatch):
    seen: dict[str, object] = {}

    def fake_build_system_prompt(**kwargs):
        seen.update(kwargs)
        return "template prompt"

    monkeypatch.setattr(core, "build_system_prompt", fake_build_system_prompt)
    await run_conversation(
        request=ConversationRunRequest(
            user_message="hi",
            context=ConversationContext(key="k"),
            trust_tier=TrustTier.MEMBER,
            user_name="u",
            user_id="1",
            provider=_CapturingProvider(),
            registry=ToolRegistry(),
            max_iterations=1,
            channel_id="20",
        )
    )
    assert seen["channel_id"] == "20"
    assert seen["thread_id"] == ""
    assert seen["parent_channel_id"] == ""
