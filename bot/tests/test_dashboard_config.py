from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.turn_entry import TurnDependencyFactory, TurnEntryHooks
from config.fragments.dashboard import load_dashboard_tool_policy
from config.fragments.prompt import build_system_prompt, resolve_template_path
from tests.helpers import make_settings
from trust.tiers import TrustTier


def template(config_dir: Path, name: str, content: str) -> Path:
    path = config_dir / "prompts" / "commands" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_dashboard_prompt_and_frontmatter_share_local_and_guild_precedence(tmp_path):
    shared = template(tmp_path, "dashboard.md", "---\npinned_tools: [shared]\n---\nSHARED")
    local = template(
        tmp_path,
        "dashboard.local.md",
        "---\npinned_tools: [local]\nblocked_tools: [blocked]\n---\nLOCAL <server_instructions>",
    )
    guild = template(tmp_path, "dashboard/2.md", "---\npinned_tools: [guild]\n---\nGUILD")
    (tmp_path / "servers").mkdir()
    (tmp_path / "servers" / "3.md").write_text("SERVER", encoding="utf-8")
    for guild_id, chosen, pin in [("2", guild, "guild"), ("3", local, "local")]:
        assert (
            resolve_template_path(
                tmp_path, channel_id="4", guild_id=guild_id, command_template="dashboard"
            )
            == chosen
        )
        policy = load_dashboard_tool_policy(guild_id, config_dir=tmp_path)
        assert policy.pinned_tools == {pin}
    prompt = build_system_prompt(
        TrustTier.MEMBER,
        "User",
        "1",
        guild_id="3",
        command_template="dashboard",
        config_dir=tmp_path,
    )
    assert "LOCAL" in prompt and "SERVER" in prompt
    assert "pinned_tools" not in prompt and "blocked_tools" not in prompt
    local.unlink()
    assert load_dashboard_tool_policy("3", config_dir=tmp_path).pinned_tools == {"shared"}
    assert shared.exists()


def test_external_config_without_dashboard_uses_shipped_surface_prompt(tmp_path):
    (tmp_path / "prompt.md").write_text("CHANNEL ONLY", encoding="utf-8")
    chosen = resolve_template_path(
        tmp_path, channel_id="3", guild_id="2", command_template="dashboard"
    )
    assert chosen == Path(__file__).resolve().parents[1] / "config/prompts/commands/dashboard.md"
    assert load_dashboard_tool_policy("2", config_dir=tmp_path).blocked_tools == frozenset()


@pytest.mark.parametrize(
    "bad", ["blocked_tools: all", "blocked_tools: [false]", "pinned_tools: tool", "[broken"]
)
def test_invalid_dashboard_policy_fails_closed_then_retains_last_valid_reload(tmp_path, bad):
    path = template(tmp_path, "dashboard.local.md", f"---\n{bad}\n---\nInstructions")
    with pytest.raises(RuntimeError, match="dashboard tool policy"):
        load_dashboard_tool_policy("2", config_dir=tmp_path)
    path.write_text("---\nblocked_tools: [dangerous]\n---\nInstructions", encoding="utf-8")
    assert load_dashboard_tool_policy("2", config_dir=tmp_path).blocked_tools == {"dangerous"}
    path.write_text(f"---\n{bad}\n---\nInstructions", encoding="utf-8")
    assert load_dashboard_tool_policy("2", config_dir=tmp_path).blocked_tools == {"dangerous"}
    path.write_text("---\nblocked_tools: []\n---\nInstructions", encoding="utf-8")
    assert load_dashboard_tool_policy("2", config_dir=tmp_path).blocked_tools == frozenset()


@pytest.mark.asyncio
async def test_dashboard_policy_adds_to_existing_tool_boundaries_only_on_dashboard(tmp_path):
    template(
        tmp_path,
        "dashboard.md",
        "---\npinned_tools: [dashboard_pin, move_to_thread]\nblocked_tools: [dashboard_block]\n---\nBODY",
    )
    services = MagicMock()
    services.settings = make_settings(config_dir=str(tmp_path))
    services.preference_store = None
    source = SimpleNamespace(
        personal_chat=False,
        guild_id="2",
        channel_id="3",
        parent_channel_id="3",
        thread_id=None,
        user_id="1",
        source_message=object(),
        allow_bot_authored_reply_context=False,
        trust_tier=TrustTier.MEMBER,
    )
    hooks = TurnEntryHooks(
        turn_has_image_input=AsyncMock(return_value=False),
        load_channel_pinned_tools=lambda _: frozenset({"channel_pin"}),
        load_guild_pinned_tools=lambda _: frozenset({"guild_pin"}),
        load_global_blocked_tools=lambda: frozenset({"global_block"}),
        load_guild_blocked_tools=lambda _: frozenset({"guild_block"}),
        load_channel_blocked_tools=lambda _: frozenset({"channel_block"}),
        load_channel_thread_handoff=lambda _: True,
        load_guild_thread_handoff=lambda _: True,
        filter_pins_to_searchable=lambda pins, *_: pins,
    )
    factory = TurnDependencyFactory(services)
    for command in ("dashboard", None):
        dependencies = await factory.build(
            source,
            collect_reply_context_func=AsyncMock(),
            strip_mention_func=MagicMock(),
            persist_prepared_user_message=AsyncMock(),
            hooks=hooks,
            command_template=command,
            extra_blocked_tools=frozenset({"move_to_thread"}),
        )
        blocked = dependencies.blocked_tools()
        assert {"global_block", "guild_block", "channel_block", "move_to_thread"} <= blocked
        assert ("dashboard_block" in blocked) == (command == "dashboard")
        pins = dependencies.channel_pinned_tools() - blocked
        assert {"channel_pin", "guild_pin"} <= pins
        assert "move_to_thread" not in pins
        assert ("dashboard_pin" in pins) == (command == "dashboard")
