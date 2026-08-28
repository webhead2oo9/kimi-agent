"""End-to-end proof that an independently packaged module attaches by entry point."""

from __future__ import annotations

from pathlib import Path

import pytest

from modules.testing import build_test_runtime
from tools.registry import MessageContext
from trust.tiers import TrustTier


def _tool_context() -> MessageContext:
    return MessageContext(
        user_id="user-1",
        user_name="Ada",
        guild_id=None,
        channel_id="channel-1",
        thread_id=None,
        trust_tier=TrustTier.MEMBER,
    )


@pytest.mark.asyncio
async def test_installed_reference_module_migrates_invokes_and_persists(tmp_path: Path) -> None:
    settings_dir = tmp_path / "config" / "modules"
    settings_dir.mkdir(parents=True)
    (settings_dir / "reference_greeter.md").write_text(
        "---\ngreeting: Welcome\n---\n", encoding="utf-8"
    )

    runtime = await build_test_runtime(tmp_path, ["reference_greeter"])
    try:
        assert runtime.manager.load_state.loaded == ("reference_greeter",)
        assert runtime.registry.is_registered("reference_greet")
        first = await runtime.registry.dispatch("reference_greet", {"name": "Ada"}, _tool_context())
        assert first == "Welcome, Ada! I have greeted someone 1 time(s)."
    finally:
        await runtime.close()

    restarted = await build_test_runtime(tmp_path, ["reference_greeter"])
    try:
        second = await restarted.registry.dispatch(
            "reference_greet", {"name": "Grace"}, _tool_context()
        )
        assert second == "Welcome, Grace! I have greeted someone 2 time(s)."
    finally:
        await restarted.close()
