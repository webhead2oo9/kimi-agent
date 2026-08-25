import asyncio
import json
import shutil
from pathlib import Path

import pytest

import evals.registry as registry_module
from app.tool_surfaces import surface_tools
from config.settings import Settings, settings
from evals.capture import InstrumentedRegistry
from evals.identity import EvalIdentity
from evals.registry import build_eval_registry, compose_tools
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
    _memory_manager, provider_manager, runtime_tools = compose_tools(
        eval_settings, registry=registry, gateway=StubGateway()
    )
    assert isinstance(provider_manager, ProviderManager)
    assert runtime_tools.registry is registry


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


def test_eval_registry_isolates_hashed_users_and_removes_writable_state(eval_settings):
    async def exercise() -> Path:
        eval_registry = await build_eval_registry(eval_settings, gateway=StubGateway())
        state_dir = eval_registry._db_dir
        assert state_dir is not None
        try:
            first = EvalIdentity("run-a", "candidate", "edit-note", 0)
            second = EvalIdentity("run-a", "candidate", "edit-note", 1)
            first_ctx = MessageContext(
                user_id=first.user_id,
                user_name="webhead",
                guild_id=None,
                channel_id="channel",
                thread_id=None,
                trust_tier=TrustTier.MEMBER,
            )
            second_ctx = MessageContext(
                user_id=second.user_id,
                user_name="webhead",
                guild_id=None,
                channel_id="channel",
                thread_id=None,
                trust_tier=TrustTier.MEMBER,
            )

            written = json.loads(
                await eval_registry.registry.dispatch(
                    "write_file",
                    {"path": "marker.txt", "content": "first repetition", "attach": False},
                    first_ctx,
                )
            )
            assert written["path"] == "marker.txt"
            first_read = await eval_registry.registry.dispatch(
                "read_file", {"path": "marker.txt"}, first_ctx
            )
            second_read = await eval_registry.registry.dispatch(
                "read_file", {"path": "marker.txt"}, second_ctx
            )
            assert "first repetition" in first_read
            assert json.loads(second_read)["error"] == "Workspace file not found: marker.txt"
            assert str(state_dir) != str(eval_settings.workspace_dir)
            return state_dir
        finally:
            await eval_registry.close()

    state_dir = asyncio.run(exercise())
    assert not state_dir.exists()


def test_eval_registry_closes_browser_before_removing_temporary_state(eval_settings):
    async def exercise() -> tuple[Path, list[str]]:
        eval_registry = await build_eval_registry(eval_settings, gateway=StubGateway())
        state_dir = eval_registry._db_dir
        assert state_dir is not None
        calls: list[str] = []
        real_browser_close = eval_registry.runtime_tools.browser_service.close
        real_modules_close = eval_registry.runtime_tools.module_manager.close
        real_memory_close = eval_registry.memory_manager.close
        real_provider_close = eval_registry.provider_manager.close
        real_database_close = eval_registry._database.close

        async def close_browser() -> None:
            assert state_dir.exists()
            calls.append("browser")
            await real_browser_close()

        async def close_modules() -> None:
            assert state_dir.exists()
            calls.append("modules")
            await real_modules_close()

        async def close_memory() -> None:
            assert state_dir.exists()
            calls.append("memory")
            await real_memory_close()

        async def close_provider() -> None:
            assert state_dir.exists()
            calls.append("provider")
            await real_provider_close()

        async def close_database() -> None:
            assert state_dir.exists()
            calls.append("database")
            await real_database_close()

        eval_registry.runtime_tools.browser_service.close = close_browser
        eval_registry.runtime_tools.module_manager.close = close_modules
        eval_registry.memory_manager.close = close_memory
        eval_registry.provider_manager.close = close_provider
        eval_registry._database.close = close_database
        await eval_registry.close()
        await eval_registry.close()  # Idempotent: owners are closed only once.
        return state_dir, calls

    state_dir, calls = asyncio.run(exercise())
    assert calls == ["browser", "modules", "memory", "provider", "database"]
    assert not state_dir.exists()


def test_eval_registry_closes_browser_when_initialization_fails(eval_settings, monkeypatch):
    async def exercise() -> tuple[Path, list[str]]:
        real_compose_tools = registry_module.compose_tools
        calls: list[str] = []
        state_dir: Path | None = None

        def compose_with_failing_memory(*args, **kwargs):
            nonlocal state_dir
            memory_manager, provider_manager, runtime_tools = real_compose_tools(*args, **kwargs)
            state_dir = Path(args[0].browser_profiles_dir).parent

            async def close_browser() -> None:
                assert state_dir is not None and state_dir.exists()
                calls.append("browser")

            async def close_modules() -> None:
                assert state_dir is not None and state_dir.exists()
                calls.append("modules")

            async def fail_ready(*_args, **_kwargs) -> None:
                raise RuntimeError("memory initialization failed")

            runtime_tools.browser_service.close = close_browser
            runtime_tools.module_manager.close = close_modules
            memory_manager.ensure_ready = fail_ready
            return memory_manager, provider_manager, runtime_tools

        monkeypatch.setattr(registry_module, "compose_tools", compose_with_failing_memory)
        with pytest.raises(RuntimeError, match="memory initialization failed"):
            await build_eval_registry(eval_settings, gateway=StubGateway())
        assert state_dir is not None
        return state_dir, calls

    state_dir, calls = asyncio.run(exercise())
    assert calls == ["browser", "modules"]
    assert not state_dir.exists()


def test_eval_registry_preserves_initialization_and_cleanup_errors(eval_settings, monkeypatch):
    async def exercise() -> Path:
        real_compose_tools = registry_module.compose_tools
        state_dir: Path | None = None

        def compose_with_failures(*args, **kwargs):
            nonlocal state_dir
            memory_manager, provider_manager, runtime_tools = real_compose_tools(*args, **kwargs)
            state_dir = Path(args[0].browser_profiles_dir).parent

            async def fail_browser_close() -> None:
                raise RuntimeError("browser cleanup failed")

            async def fail_ready(*_args, **_kwargs) -> None:
                raise RuntimeError("memory initialization failed")

            runtime_tools.browser_service.close = fail_browser_close
            memory_manager.ensure_ready = fail_ready
            return memory_manager, provider_manager, runtime_tools

        monkeypatch.setattr(registry_module, "compose_tools", compose_with_failures)
        with pytest.raises(BaseExceptionGroup) as raised:
            await build_eval_registry(eval_settings, gateway=StubGateway())
        messages = {str(error) for error in raised.value.exceptions}
        assert "memory initialization failed" in messages
        assert "browser cleanup failed" in messages
        assert state_dir is not None
        return state_dir

    state_dir = asyncio.run(exercise())
    assert not state_dir.exists()


def test_eval_registry_retries_failed_state_directory_removal(eval_settings, monkeypatch):
    async def exercise() -> tuple[Path, int]:
        eval_registry = await build_eval_registry(eval_settings, gateway=StubGateway())
        state_dir = eval_registry._db_dir
        assert state_dir is not None
        real_rmtree = registry_module.shutil.rmtree
        attempts = 0

        def fail_once(path: Path) -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError("state directory is busy")
            real_rmtree(path)

        monkeypatch.setattr(registry_module.shutil, "rmtree", fail_once)
        with pytest.raises(OSError, match="state directory is busy"):
            await eval_registry.close()
        assert state_dir.exists()
        await eval_registry.close()
        await eval_registry.close()
        return state_dir, attempts

    state_dir, attempts = asyncio.run(exercise())
    assert attempts == 2
    assert not state_dir.exists()


def test_eval_registry_continues_cleanup_after_browser_close_fails(eval_settings):
    async def exercise() -> tuple[Path, list[str]]:
        eval_registry = await build_eval_registry(eval_settings, gateway=StubGateway())
        state_dir = eval_registry._db_dir
        assert state_dir is not None
        calls: list[str] = []
        browser_attempts = 0
        real_browser_close = eval_registry.runtime_tools.browser_service.close
        real_modules_close = eval_registry.runtime_tools.module_manager.close
        real_memory_close = eval_registry.memory_manager.close
        real_provider_close = eval_registry.provider_manager.close
        real_database_close = eval_registry._database.close

        async def close_browser() -> None:
            nonlocal browser_attempts
            calls.append("browser")
            browser_attempts += 1
            if browser_attempts == 1:
                raise RuntimeError("browser cleanup failed")
            await real_browser_close()

        async def close_modules() -> None:
            calls.append("modules")
            await real_modules_close()

        async def close_memory() -> None:
            calls.append("memory")
            await real_memory_close()

        async def close_provider() -> None:
            calls.append("provider")
            await real_provider_close()

        async def close_database() -> None:
            calls.append("database")
            await real_database_close()

        eval_registry.runtime_tools.browser_service.close = close_browser
        eval_registry.runtime_tools.module_manager.close = close_modules
        eval_registry.memory_manager.close = close_memory
        eval_registry.provider_manager.close = close_provider
        eval_registry._database.close = close_database

        with pytest.raises(RuntimeError, match="browser cleanup failed"):
            await eval_registry.close()
        assert not state_dir.exists()
        await eval_registry.close()
        await eval_registry.close()
        return state_dir, calls

    state_dir, calls = asyncio.run(exercise())
    expected_order = ["browser", "modules", "memory", "provider", "database"]
    assert calls == [*expected_order, *expected_order]
    assert not state_dir.exists()
