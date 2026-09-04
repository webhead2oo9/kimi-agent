import json
from pathlib import Path

import pytest

from skills.loader import scan_skills
from skills.registration import register_all_skill_tools
from tests.helpers import make_settings
from tests.skill_runner_helpers import run_script_with_direct_test_command
from tools.browse import init_browse_tools
from tools.registry import MessageContext, ToolRegistry
from trust.tiers import TrustTier


@pytest.fixture
def full_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ToolRegistry:
    from skills import registration

    monkeypatch.setattr(registration, "run_script", run_script_with_direct_test_command)

    reg = ToolRegistry()
    store = Path("tests/fixtures/skills")
    register_all_skill_tools(
        skills_store=store,
        registry=reg,
        secrets={},
        settings=make_settings(),
        workspace_base_dir=tmp_path / "workspaces",
    )
    init_browse_tools(reg)
    return reg


def _make_ctx(activated: set | None = None) -> MessageContext:
    return MessageContext(
        user_id="test_user",
        user_name="Tester",
        guild_id="g1",
        channel_id="c1",
        thread_id=None,
        trust_tier=TrustTier.STAFF,
        activated_tools=activated or set(),
    )


def test_echo_test_skill_scanned() -> None:
    skills = scan_skills(Path("tests/fixtures/skills"))
    assert "echo-test" in skills
    assert len(skills["echo-test"].tools) == 1
    assert skills["echo-test"].tools[0].name == "echo_test"


@pytest.mark.asyncio
async def test_browse_loads_echo_test(full_registry: ToolRegistry) -> None:
    ctx = _make_ctx()
    result = await full_registry.dispatch("browse_tools", {"load": ["echo_test"]}, ctx)
    parsed = json.loads(result)
    assert parsed["loaded"] == ["echo_test"]
    assert "echo_test" in ctx.activated_tools


@pytest.mark.asyncio
async def test_echo_test_executes_after_activation(full_registry: ToolRegistry) -> None:
    ctx = _make_ctx(activated={"echo_test"})
    result = await full_registry.dispatch("echo_test", {"message": "hello world"}, ctx)
    parsed = json.loads(result)
    assert parsed["echoed"] == "hello world"
    assert parsed["workspace_exists"] is True
    assert parsed["has_path"] is True


@pytest.mark.asyncio
async def test_echo_test_rejected_without_activation(full_registry: ToolRegistry) -> None:
    ctx = _make_ctx(activated=set())
    result = await full_registry.dispatch("echo_test", {"message": "hello"}, ctx)
    parsed = json.loads(result)
    assert "error" in parsed
