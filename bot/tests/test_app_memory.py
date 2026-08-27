from __future__ import annotations

from typing import cast

import pytest

from app import memory as memory_runtime
from config.settings import Settings
from storage.conversations import ConversationStore
from storage.preferences import PreferenceStore
from tools.registry import ToolRegistry


def _settings(**kwargs: object) -> Settings:
    values: dict[str, object] = {"hindsight_url": "http://hindsight.local", **kwargs}
    return Settings.model_validate(values)


class FakeMemoryClient:
    def __init__(self) -> None:
        self.close_count = 0

    async def close(self) -> None:
        self.close_count += 1


class FakePreferenceStore:
    async def is_memory_enabled(self, user_id: str) -> bool:
        return True


@pytest.mark.asyncio
async def test_memory_manager_registers_tools_after_successful_bank_setup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ToolRegistry()
    client = FakeMemoryClient()
    manager = memory_runtime.MemoryManager(_settings(), registry, client=client)

    async def fake_ensure_global_banks(memory_client: object) -> bool:
        assert memory_client is client
        return True

    monkeypatch.setattr(
        memory_runtime,
        "ensure_global_banks",
        fake_ensure_global_banks,
    )

    assert manager.active_client() is None

    await manager.ensure_ready(
        cast(ConversationStore, object()),
        cast(PreferenceStore, FakePreferenceStore()),
    )

    assert manager.ready is True
    assert manager.tools_registered is True
    assert manager.active_client() is client
    assert registry.has_tool("recall_community")
    assert registry.has_tool("reflect_community")
    assert registry.has_tool("teach")
    assert registry.has_tool("recall_user")
    assert registry.has_tool("reflect_user")
    assert registry.has_tool("remember_user_memory")
    assert not registry.has_tool("lookup_memory_source")


@pytest.mark.asyncio
async def test_memory_manager_unregisters_tools_after_later_bank_setup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ToolRegistry()
    client = FakeMemoryClient()
    manager = memory_runtime.MemoryManager(_settings(), registry, client=client)
    outcomes = [True, False]

    async def fake_ensure_global_banks(memory_client: object) -> bool:
        return outcomes.pop(0)

    monkeypatch.setattr(
        memory_runtime,
        "ensure_global_banks",
        fake_ensure_global_banks,
    )

    await manager.ensure_ready(
        cast(ConversationStore, object()),
        cast(PreferenceStore, FakePreferenceStore()),
    )
    assert registry.has_tool("recall_user")

    await manager.ensure_ready(
        cast(ConversationStore, object()),
        cast(PreferenceStore, FakePreferenceStore()),
    )

    assert manager.ready is False
    assert manager.tools_registered is False
    for name in memory_runtime.MEMORY_TOOL_NAMES:
        assert not registry.has_tool(name)


@pytest.mark.asyncio
async def test_memory_manager_rebinds_write_tools_on_repeated_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ToolRegistry()
    client = FakeMemoryClient()
    manager = memory_runtime.MemoryManager(_settings(), registry, client=client)
    community_calls = 0
    user_calls = 0
    write_calls: list[tuple[object, object]] = []

    async def fake_ensure_global_banks(memory_client: object) -> bool:
        return True

    def fake_init_community_tools(
        tool_registry: ToolRegistry,
        memory_client: object,
        *,
        on_learn: object | None = None,
    ) -> None:
        nonlocal community_calls
        assert tool_registry is registry
        assert memory_client is client
        community_calls += 1

    def fake_init_user_memory_tools(
        tool_registry: ToolRegistry,
        memory_client: object,
        *,
        recall_types: list[str] | None = None,
    ) -> None:
        nonlocal user_calls
        assert tool_registry is registry
        assert memory_client is client
        assert recall_types == ["world"]
        user_calls += 1

    def fake_init_user_memory_write_tools(
        tool_registry: ToolRegistry,
        memory_client: object,
        conversation_store: object,
        preference_store: object,
        *,
        max_writes_per_turn: int = 3,
    ) -> None:
        assert tool_registry is registry
        assert memory_client is client
        write_calls.append((conversation_store, preference_store))

    monkeypatch.setattr(
        memory_runtime,
        "ensure_global_banks",
        fake_ensure_global_banks,
    )
    monkeypatch.setattr(
        memory_runtime,
        "init_community_tools",
        fake_init_community_tools,
    )
    monkeypatch.setattr(
        memory_runtime,
        "init_user_memory_tools",
        fake_init_user_memory_tools,
    )
    monkeypatch.setattr(
        memory_runtime,
        "init_user_memory_write_tools",
        fake_init_user_memory_write_tools,
    )
    manager.settings.memory_recall_types = "world"
    store_1 = object()
    store_2 = object()
    preferences_1 = object()
    preferences_2 = object()

    await manager.ensure_ready(
        cast(ConversationStore, store_1),
        cast(PreferenceStore, preferences_1),
    )
    await manager.ensure_ready(
        cast(ConversationStore, store_2),
        cast(PreferenceStore, preferences_2),
    )

    assert community_calls == 1
    assert user_calls == 1
    assert write_calls == [(store_1, preferences_1), (store_2, preferences_2)]


@pytest.mark.asyncio
async def test_memory_manager_close_calls_client_close_once() -> None:
    registry = ToolRegistry()
    client = FakeMemoryClient()
    manager = memory_runtime.MemoryManager(_settings(), registry, client=client)
    manager.ready = True
    manager.tools_registered = True

    await manager.close()
    await manager.close()

    assert client.close_count == 1
    assert manager.client is None
    assert manager.ready is False
    assert manager.tools_registered is False


def test_memory_manager_does_not_create_client_without_hindsight_url() -> None:
    manager = memory_runtime.MemoryManager(
        _settings(hindsight_url=""),
        ToolRegistry(),
    )

    assert manager.client is None
    assert manager.active_client() is None


@pytest.mark.asyncio
async def test_memory_manager_close_awaits_sync_close_hook() -> None:
    class SyncClient:
        def __init__(self) -> None:
            self.close_count = 0

        def close(self) -> None:
            self.close_count += 1

    client = SyncClient()
    manager = memory_runtime.MemoryManager(_settings(), ToolRegistry(), client=client)

    await manager.close()

    assert client.close_count == 1
