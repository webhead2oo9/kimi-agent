"""End-to-end proof that the packaged reference module attaches by entry point.

The module's own suite (``modules/example/tests``) covers behavior on the API
fakes. This test is the host's half: entry-point discovery, settings overlay
from ``config/modules``, real migrations on a real SQLite file, dispatch through
the real ``ToolRegistry`` privilege boundary, and persistence across a restart.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from modules.testing import build_test_runtime
from tools.registry import UNTRUSTED_CONTEXT_NOTE, MessageContext
from trust.tiers import TrustTier

MODULE = "reference_kudos"
GUILD = 4242


def _tool_context(user_id: str, guild_id: str | None = str(GUILD)) -> MessageContext:
    return MessageContext(
        user_id=user_id,
        user_name="Ada",
        guild_id=guild_id,
        channel_id="5150",  # module tools receive ids as ints, so they must be numeric
        thread_id=None,
        trust_tier=TrustTier.MEMBER,
        # kudos_leaderboard is searchable: hidden until browse_tools activates it.
        activated_tools={"kudos_leaderboard"},
    )


@pytest.mark.asyncio
async def test_installed_reference_module_migrates_dispatches_and_persists(
    tmp_path: Path,
) -> None:
    settings_dir = tmp_path / "config" / "modules"
    settings_dir.mkdir(parents=True)
    # Operator override of an exposed field, read through config/modules.
    (settings_dir / f"{MODULE}.md").write_text("---\ndaily_limit: 1\n---\n", encoding="utf-8")

    runtime = await build_test_runtime(tmp_path, [MODULE], guild_config={GUILD: {}})
    try:
        assert runtime.manager.load_state.loaded == (MODULE,)
        assert runtime.registry.is_registered("give_kudos")
        assert runtime.registry.is_registered("kudos_leaderboard")
        ports = runtime.ports[MODULE]
        assert "digest" in ports.scheduler.jobs
        assert set(ports.interactions.commands) == {"kudos.give", "kudos.top", "kudos.setup"}

        first = await runtime.registry.dispatch(
            "give_kudos", {"user": "<@7>", "reason": "shipped it"}, _tool_context("1")
        )
        first_payload = json.loads(first)
        assert first_payload == {
            "result": "Kudos to <@7>: shipped it",
            "context_is_untrusted": True,
            "note": UNTRUSTED_CONTEXT_NOTE,
        }
        # The operator's daily_limit override of 1 is in force.
        second = await runtime.registry.dispatch(
            "give_kudos", {"user": "<@8>", "reason": "again"}, _tool_context("1")
        )
        assert "last 24 hours" in json.loads(second)["result"]
        # Guild-only tools are masked, not refused, for a guild-less caller (DM or
        # personal chat): the registry answers as if the tool did not exist.
        masked = await runtime.registry.dispatch(
            "give_kudos", {"user": "<@7>", "reason": "x"}, _tool_context("1", guild_id=None)
        )
        assert "Unknown tool" in masked
    finally:
        await runtime.close()

    restarted = await build_test_runtime(tmp_path, [MODULE], guild_config={GUILD: {}})
    try:
        board = await restarted.registry.dispatch(
            "kudos_leaderboard", {"days": 1}, _tool_context("2")
        )
        board_payload = json.loads(board)
        assert board_payload["context_is_untrusted"] is True
        assert board_payload["result"].splitlines()[1:] == ["1. <@7> — 1"]
    finally:
        await restarted.close()
