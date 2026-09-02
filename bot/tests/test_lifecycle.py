from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from app import lifecycle as lifecycle_module
from app import runtime as app_runtime
from app.lifecycle import ApplicationLifecycle
from storage.auto_retain import AutoRetainStore
from tests.helpers import make_settings, StubProviderManager, replace_lifecycle_resources
from utils.privacy_barrier import PrivacyDeletionPendingError
from workspace import WorkspaceKey


def _build_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
    *,
    database_path: Path | None = None,
) -> tuple[app_runtime.KimiApplication, ApplicationLifecycle]:
    monkeypatch.setattr(
        app_runtime,
        "build_provider_manager",
        lambda settings: StubProviderManager(settings),
    )
    settings = make_settings(
        model_api_key="test-key",
        moderation_enabled=False,
        database_path=str(database_path) if database_path is not None else "data/test.db",
    )
    app = app_runtime.build_app(settings)
    return app, app.lifecycle


@pytest.mark.asyncio
async def test_filesystem_sweepers_install_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _app, lifecycle = _build_lifecycle(monkeypatch)
    started: list[str] = []

    async def sweep_once(*args: Any, **kwargs: Any) -> int:
        del args, kwargs
        return 0

    def create_task(coroutine: Any) -> asyncio.Task[Any]:
        started.append(coroutine.cr_code.co_name)
        coroutine.close()
        return cast(asyncio.Task[Any], object())

    monkeypatch.setattr(lifecycle_module, "sweep_attachment_orphans_once", sweep_once)
    monkeypatch.setattr(lifecycle_module.asyncio, "create_task", create_task)

    await lifecycle.start_filesystem_sweepers()
    await lifecycle.start_filesystem_sweepers()

    assert sorted(started) == ["attachment_orphan_sweeper", "workspace_sweeper"]
    snapshot = lifecycle.snapshot()
    assert snapshot.workspace_sweeper_started is True
    assert snapshot.workspace_sweeper_task is not None
    assert snapshot.attachment_sweeper_task is not None


@pytest.mark.asyncio
async def test_ready_cancellation_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app_runtime, "READY_EVENT_DRAIN_SECONDS", 0.01)
    app, lifecycle = _build_lifecycle(monkeypatch)
    started = asyncio.Event()
    release = asyncio.Event()

    async def stubborn_ready_cohort() -> None:
        async with app.command_sync.ready_cohort():
            started.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue

    ready_task = asyncio.create_task(stubborn_ready_cohort())
    await started.wait()

    await lifecycle.cancel_ready_events(exclude=None)

    assert ready_task.done() is False
    release.set()
    await ready_task


@pytest.mark.asyncio
async def test_close_resources_preserves_dependency_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, lifecycle = _build_lifecycle(monkeypatch)
    events: list[str] = []

    class _Closer:
        def __init__(self, name: str) -> None:
            self.name = name

        async def close(self) -> None:
            events.append(self.name)

    class _CommandSync:
        async def cancel_all(self) -> None:
            events.append("command-sync")

    class _ActiveOperations:
        async def cancel_all(self) -> bool:
            events.append("active-operations")
            return True

    class _ModuleManager(_Closer):
        scheduler = None
        http = None
        events = None

    async def drain_privacy() -> None:
        events.append("privacy")

    async def stop_events() -> None:
        events.append("event-writer")

    tools = SimpleNamespace(
        browser_service=_Closer("browser"),
        video_service=_Closer("video"),
        module_manager=_ModuleManager("modules"),
    )
    monkeypatch.setattr(lifecycle_module, "drain_confirmed_privacy_deletions", drain_privacy)
    monkeypatch.setattr(lifecycle_module, "stop_event_writer", stop_events)
    replace_lifecycle_resources(
        app,
        command_sync=cast(Any, _CommandSync()),
        turn_admission=cast(Any, _Closer("admission")),
        guild_activation=cast(Any, _Closer("guild-activation")),
        active_operations=cast(Any, _ActiveOperations()),
        coding_tasks=cast(Any, _Closer("coding")),
        tools=cast(Any, tools),
        module_manager=cast(Any, tools.module_manager),
        memory_manager=cast(Any, _Closer("memory")),
        provider_manager=cast(Any, _Closer("providers")),
        database=cast(Any, _Closer("database")),
        moderation_service=None,
    )

    await lifecycle.close_resources()

    assert events == [
        "command-sync",
        "admission",
        "guild-activation",
        "active-operations",
        "privacy",
        "event-writer",
        "coding",
        "browser",
        "modules",
        "memory",
        "providers",
        "video",
        "database",
    ]


@pytest.mark.asyncio
async def test_privacy_replay_tombstones_before_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app, lifecycle = _build_lifecycle(monkeypatch, database_path=tmp_path / "bot.db")
    await app.database.connect()
    await app.privacy_deletion_store.request(
        user_id="42",
        scope="all",
        memory_backend_required=False,
    )

    async def fail_replay(**kwargs: Any) -> None:
        del kwargs
        raise RuntimeError("dependency unavailable")

    monkeypatch.setattr(lifecycle_module, "run_privacy_deletion", fail_replay)
    try:
        await lifecycle.resume_pending_privacy_deletions(
            auto_retain_watermarks=AutoRetainStore(app.database)
        )

        with pytest.raises(PrivacyDeletionPendingError):
            async with app.privacy_barrier.activity(WorkspaceKey("42")):
                pass
        assert [request.user_id for request in await app.privacy_deletion_store.list_pending()] == [
            "42"
        ]
    finally:
        await app.database.close()
