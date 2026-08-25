from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from app.memory import MemoryManager
from app.providers import ProviderManager, build_provider_manager
from app.tools import build_runtime_tools
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
) -> tuple[MemoryManager, ProviderManager]:
    """Offline registry composition: build_runtime_tools + safe stubs (NO ensure_ready)."""
    provider_manager = build_provider_manager(settings)
    memory_manager = memory_manager or MemoryManager(settings=settings, registry=registry)
    # block_user must exist in evals (safety scenarios grade refusal to misuse it),
    # but attempts only ever write into this in-memory stub.
    blocked_user_store = StubBlockedUserStore()
    build_runtime_tools(
        settings,
        gateway,
        provider_manager,
        memory_manager,
        get_blocked_user_store=lambda: blocked_user_store,
        # Thread handoff is ON by default in production, so leaving it unwired here
        # hid a default-on surface from every run. `move_to_thread` never touches the
        # manager (it only sets ctx.thread_request), and the three lifecycle tools
        # fail closed to a tool_error outside a managed thread, so a null manager
        # registers the surface without simulating a live thread.
        get_thread_handoff=lambda: None,
        registry=registry,
    )
    # Chat-side coding controls are registered by app/runtime.py in production and so
    # were unreachable from evals entirely. The stub keeps `start_coding_task` gradeable
    # (does the model delegate, or answer inline?) without ever spawning a job.
    init_coding_control_tools(registry, cast(CodingTaskControls, StubCodingControls()))
    install_safe_stubs(registry)
    return memory_manager, provider_manager


@dataclass
class EvalRegistry:
    registry: InstrumentedRegistry
    memory_manager: MemoryManager
    provider_manager: ProviderManager
    _database: Database
    preference_store: PreferenceStore
    _db_dir: Path | None = None

    async def close(self) -> None:
        await self.memory_manager.close()
        await self.provider_manager.close()
        await self._database.close()
        if self._db_dir is not None:
            shutil.rmtree(self._db_dir, ignore_errors=True)


async def build_eval_registry(settings: Settings, *, gateway: Any) -> EvalRegistry:
    """Full composition incl. live memory tools. Requires a reachable Hindsight backend."""
    registry = InstrumentedRegistry()
    db_dir = Path(tempfile.mkdtemp(prefix="eval-db-"))
    database = Database(path=str(db_dir / "eval.db"))
    await database.connect()
    try:
        conversation_store = ConversationStore(database)
        preference_store = PreferenceStore(database)
        memory_manager, provider_manager = compose_tools(
            settings, registry=registry, gateway=gateway
        )
        await memory_manager.ensure_ready(conversation_store, preference_store)
        # Memory tools register during ensure_ready; re-apply safe stubs in case `teach`
        # was added by ensure_ready.
        install_safe_stubs(registry)
    except BaseException:
        await database.close()
        shutil.rmtree(db_dir, ignore_errors=True)
        raise
    return EvalRegistry(
        registry=registry,
        memory_manager=memory_manager,
        provider_manager=provider_manager,
        _database=database,
        preference_store=preference_store,
        _db_dir=db_dir,
    )
