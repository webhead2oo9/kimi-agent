import asyncio
import json
import shutil
from pathlib import Path

import pytest

from app.tool_surfaces import surface_tools
from config.settings import Settings, settings
from evals.capture import InstrumentedRegistry
from evals.registry import compose_tools
from evals.stub_gateway import SAFE_STUB_TOOLS, StubGateway
from tests.helpers import PROJECT_ROOT
from tools.registry import MessageContext
from trust.tiers import TrustTier


@pytest.fixture
def eval_settings(tmp_path: Path) -> Settings:
    """Settings routed at the committed template instead of operator instance state.

    `config/models.yaml` is untracked private state (bot/.gitignore) and is absent
    from a clean checkout, so reading it made these tests depend on a file the repo
    does not carry; they failed here and would fail in CI. `models.example.yaml`
    is tracked and deliberately kept resolvable for exactly this purpose (see
    config/model_config.py and tests/test_model_config.py), so copy it in under the
    name the loader expects and point config_dir at that.
    """

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    shutil.copy(PROJECT_ROOT / "config" / "models.example.yaml", config_dir / "models.yaml")
    return settings.model_copy(update={"config_dir": str(config_dir)})


def test_compose_tools_registers_core_tools_and_installs_safe_stubs(eval_settings):
    # Offline: build_runtime_tools constructs clients lazily (no network), and we skip
    # ensure_ready (which needs live Hindsight).
    registry = InstrumentedRegistry()
    compose_tools(eval_settings, registry=registry, gateway=StubGateway())

    # A core, always-present tool proves build_runtime_tools ran into our registry.
    assert registry.has_tool("browse_tools")

    # Core's own stub list plus anything a loaded plugin declared for the eval_stub
    # surface must dispatch the safe stub ack rather than its real
    # production-writing handler. Iterating only the plugin surface made this loop
    # a no-op in a plain checkout (and in CI), so the assertion never ran; the core
    # names keep it honest wherever the tools are registered.
    for name in sorted({*SAFE_STUB_TOOLS, *surface_tools("eval_stub")}):
        if not registry.has_tool(name):
            continue
        ctx = MessageContext(
            user_id="u",
            user_name="n",
            guild_id="g",
            channel_id="c",
            thread_id=None,
            trust_tier=TrustTier.STAFF,
            activated_tools={name},
        )
        result = asyncio.run(registry.dispatch(name, {}, ctx))
        assert json.loads(result)["status"] == "stubbed"


def test_compose_tools_registers_block_user_against_in_memory_stub(eval_settings):
    # Safety scenarios grade refusal to misuse block_user, so the tool must exist in
    # eval runs (production registers it unconditionally), but a dispatched call may
    # only land in the in-memory stub store, never a real block list.
    registry = InstrumentedRegistry()
    compose_tools(eval_settings, registry=registry, gateway=StubGateway())

    assert registry.has_tool("block_user")
    ctx = MessageContext(
        user_id="eval-user",
        user_name="n",
        guild_id="g",
        channel_id="c",
        thread_id=None,
        trust_tier=TrustTier.MEMBER,
    )
    result = asyncio.run(registry.dispatch("block_user", {"reason": "abuse"}, ctx))
    payload = json.loads(result)
    assert payload["blocked"] is True
    assert payload["user_id"] == "eval-user"


def test_compose_tools_returns_a_provider_manager(eval_settings):
    from app.providers import ProviderManager

    registry = InstrumentedRegistry()
    _memory_manager, provider_manager = compose_tools(
        eval_settings, registry=registry, gateway=StubGateway()
    )
    assert isinstance(provider_manager, ProviderManager)


def test_compose_tools_registers_default_on_thread_and_coding_surfaces(eval_settings):
    """Both surfaces were unreachable from evals and shipped ungraded.

    thread_handoff_enabled defaults to True, but app/tools.py also requires a
    get_thread_handoff callable, and the chat-side coding controls are installed
    by app/runtime.py rather than build_runtime_tools. Neither gap was visible as
    a failure: the scenarios simply could not be written.
    """

    registry = InstrumentedRegistry()
    compose_tools(eval_settings, registry=registry, gateway=StubGateway())
    for name in (
        "move_to_thread",
        "leave_thread",
        "pause_thread_replies",
        "resume_thread_replies",
        "start_coding_task",
        "coding_task_status",
        "coding_task_message",
        "coding_task_cancel",
    ):
        assert registry.has_tool(name), name


def test_coding_start_dispatches_into_the_stub_not_a_real_scheduler(eval_settings):
    registry = InstrumentedRegistry()
    compose_tools(eval_settings, registry=registry, gateway=StubGateway())
    ctx = MessageContext(
        user_id="u",
        user_name="n",
        guild_id="g",
        channel_id="c",
        thread_id=None,
        trust_tier=TrustTier.MEMBER,
        activated_tools={"start_coding_task"},
    )
    result = json.loads(
        asyncio.run(registry.dispatch("start_coding_task", {"task": "port it"}, ctx))
    )
    assert result["task_id"] == "eval-task-1"
    assert result["status"] == "queued"
    assert ctx.terminal_handoff is not None


def test_move_to_thread_works_without_a_live_handoff_manager(eval_settings):
    """The null manager must not turn tool SELECTION grading into a tool error."""

    registry = InstrumentedRegistry()
    compose_tools(eval_settings, registry=registry, gateway=StubGateway())
    ctx = MessageContext(
        user_id="u",
        user_name="n",
        guild_id="g",
        channel_id="c",
        thread_id=None,
        trust_tier=TrustTier.MEMBER,
        activated_tools={"move_to_thread"},
    )
    asyncio.run(registry.dispatch("move_to_thread", {"name": "launcher rollback"}, ctx))
    assert ctx.thread_request is not None
