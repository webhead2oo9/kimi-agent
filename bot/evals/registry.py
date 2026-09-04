from __future__ import annotations

import shutil
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from app.memory import MemoryManager
from app.providers import ProviderManager, build_provider_manager
from app.tools import RuntimeTools, build_runtime_tools
from tools.coding_tasks import CodingTaskControls, init_coding_control_tools
from config.settings import Settings
from evals.capture import InstrumentedRegistry
from evals.stub_gateway import StubBlockedUserStore, StubCodingControls, install_safe_stubs
from storage.conversations import ConversationStore
from storage.db import Database
from storage.preferences import PreferenceStore


def compose_tools(
    settings: Settings,
    *,
    registry: InstrumentedRegistry,
    gateway: Any,
    memory_manager: MemoryManager | None = None,
    provider_manager: ProviderManager | None = None,
) -> tuple[MemoryManager, ProviderManager, RuntimeTools]:
    """Offline registry composition: build_runtime_tools + safe stubs (NO ensure_ready)."""
    provider_manager = provider_manager or build_provider_manager(settings)
    memory_manager = memory_manager or MemoryManager(settings=settings, registry=registry)
    # block_user must exist in evals (safety scenarios grade refusal to misuse it),
    # but attempts only ever write into this in-memory stub.
    blocked_user_store = StubBlockedUserStore()
    runtime_tools = build_runtime_tools(
        settings,
        gateway,
        provider_manager,
        get_blocked_user_store=lambda: blocked_user_store,
        # Thread handoff is on by default in production. `move_to_thread` only
        # sets ctx.outbox.thread_request, and lifecycle tools fail closed outside a
        # managed thread, so a null manager registers the surface without
        # simulating a live thread.
        get_thread_handoff=lambda: None,
        registry=registry,
    )
    # Chat-side coding controls are normally registered by app/runtime.py. The
    # stub keeps `start_coding_task` gradeable (delegate or answer inline)
    # without spawning a job.
    init_coding_control_tools(registry, cast(CodingTaskControls, StubCodingControls()))
    # Modules are loaded, never started, in evals: their tools would otherwise
    # stay masked. Every eval guild counts as active for them.
    runtime_tools.module_manager.bind_tool_availability(lambda _guild_id: True)
    install_safe_stubs(registry)
    return memory_manager, provider_manager, runtime_tools


async def _close_eval_resources(
    *,
    runtime_tools: RuntimeTools | None,
    memory_manager: MemoryManager | None,
    provider_manager: ProviderManager | None,
    database: Database,
    state_dir: Path | None,
) -> None:
    """Close every composed owner before deleting its temporary state.

    Cleanup is best-effort but not silent: every owner and the state directory get
    a cleanup attempt, and every teardown error remains available to the caller.
    """

    closers: list[tuple[str, Callable[[], Awaitable[None]]]] = []
    if runtime_tools is not None:
        closers.extend(
            (
                ("browser service", runtime_tools.browser_service.close),
                ("module manager", runtime_tools.module_manager.close),
            )
        )
    if memory_manager is not None:
        closers.append(("memory manager", memory_manager.close))
    if provider_manager is not None:
        closers.append(("provider manager", provider_manager.close))
    closers.append(("database", database.close))

    errors: list[BaseException] = []
    for owner, close in closers:
        try:
            await close()
        except BaseException as exc:
            exc.add_note(f"while closing eval {owner}")
            errors.append(exc)
    if state_dir is not None:
        try:
            shutil.rmtree(state_dir)
        except FileNotFoundError:
            pass
        except BaseException as exc:
            exc.add_note(f"while removing eval state directory {state_dir}")
            errors.append(exc)

    if len(errors) == 1:
        raise errors[0]
    if errors:
        raise BaseExceptionGroup("Eval resource cleanup failed", errors)


@dataclass
class EvalRegistry:
    registry: InstrumentedRegistry
    memory_manager: MemoryManager
    provider_manager: ProviderManager
    runtime_tools: RuntimeTools
    _database: Database
    preference_store: PreferenceStore
    _db_dir: Path | None = None
    _closed: bool = False

    async def close(self) -> None:
        if self._closed:
            return
        await _close_eval_resources(
            runtime_tools=self.runtime_tools,
            memory_manager=self.memory_manager,
            provider_manager=self.provider_manager,
            database=self._database,
            state_dir=self._db_dir,
        )
        self._closed = True


async def build_eval_registry(settings: Settings, *, gateway: Any) -> EvalRegistry:
    """Compose production tools over temporary eval-owned writable state."""
    registry = InstrumentedRegistry()
    db_dir = Path(tempfile.mkdtemp(prefix="eval-state-"))
    eval_settings = settings.model_copy(
        update={
            "workspace_dir": str(db_dir / "workspaces"),
            "attachment_store_dir": str(db_dir / "attachments"),
            "personal_skills_dir": str(db_dir / "personal-skills"),
            "browser_profiles_dir": str(db_dir / "browser-profiles"),
            # Eval events already live in summary/transcript artifacts; never append
            # synthetic users or model output to the deployment's event stream.
            "tool_event_log_enabled": False,
        }
    )
    database = Database(path=str(db_dir / "eval.db"))
    memory_manager: MemoryManager | None = None
    provider_manager: ProviderManager | None = None
    runtime_tools: RuntimeTools | None = None
    try:
        await database.connect()
        conversation_store = ConversationStore(database)
        preference_store = PreferenceStore(database)
        provider_manager = build_provider_manager(eval_settings)
        memory_manager = MemoryManager(settings=eval_settings, registry=registry)
        memory_manager, provider_manager, runtime_tools = compose_tools(
            eval_settings,
            registry=registry,
            gateway=gateway,
            memory_manager=memory_manager,
            provider_manager=provider_manager,
        )
        await memory_manager.ensure_ready(conversation_store, preference_store)
        # Memory tools register during ensure_ready; re-apply safe stubs in case `teach`
        # was added by ensure_ready.
        install_safe_stubs(registry)
    except BaseException as initialization_error:
        try:
            await _close_eval_resources(
                runtime_tools=runtime_tools,
                memory_manager=memory_manager,
                provider_manager=provider_manager,
                database=database,
                state_dir=db_dir,
            )
        except BaseException as cleanup_error:
            raise BaseExceptionGroup(
                "Eval registry initialization and cleanup failed",
                [initialization_error, cleanup_error],
            ) from None
        raise
    assert memory_manager is not None
    assert provider_manager is not None
    assert runtime_tools is not None
    return EvalRegistry(
        registry=registry,
        memory_manager=memory_manager,
        provider_manager=provider_manager,
        runtime_tools=runtime_tools,
        _database=database,
        preference_store=preference_store,
        _db_dir=db_dir,
    )
