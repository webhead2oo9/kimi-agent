from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.browser import BrowserToolConfig, init_browser_tool
from tools.registry import UNTRUSTED_CONTEXT_NOTE, MessageContext, ToolRegistry
from tools.workspace.common import UserLocks
from trust.tiers import TrustTier
from workspace import WorkspaceManager


class _BrowserService:
    def __init__(self, profile_root: Path) -> None:
        self._profile_root = profile_root

    def uses_netns(self) -> bool:
        return False

    async def acquire_turn(self, owner_id: str, turn_id: str) -> bool:
        return True

    async def release_turn(self, owner_id: str, turn_id: str) -> None:
        return None

    async def run(self, **kwargs: object) -> dict[str, object]:
        return {"ok": True, "value": "é" * 1_000}

    def profile_home(self, owner_id: str) -> Path:
        return self._profile_root / owner_id


@pytest.mark.asyncio
async def test_dispatch_bounds_browser_result_after_untrusted_framing(tmp_path: Path) -> None:
    registry = ToolRegistry()
    init_browser_tool(
        registry,
        _BrowserService(tmp_path / "profiles"),  # type: ignore[arg-type]
        WorkspaceManager(tmp_path / "workspace"),
        BrowserToolConfig(),
        UserLocks(),
    )
    ctx = MessageContext(
        user_id="user-1",
        user_name="Tester",
        guild_id="guild-1",
        channel_id="channel-1",
        thread_id=None,
        trust_tier=TrustTier.MEMBER,
        tool_configs={"browser": {"max_output_chars": 128}},
    )

    raw = await registry.dispatch("browser", {"code": "return document.title"}, ctx)
    result = json.loads(raw)

    assert len(raw) <= 128
    assert result == {
        "truncated": True,
        "context_is_untrusted": True,
        "note": UNTRUSTED_CONTEXT_NOTE,
    }
