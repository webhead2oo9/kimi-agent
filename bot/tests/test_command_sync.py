from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import cast

import pytest
from discord import app_commands

from app.command_sync import CommandSyncConfig, DiscordCommandSync
from tests.app_state_probes import command_sync_state
from tests.helpers import set_command_sync_retired_tasks


class FakeTree:
    def __init__(self, sync: Callable[[], Awaitable[list[object]]]) -> None:
        self.sync = sync


class FakeGuildSyncPort:
    def __init__(self) -> None:
        self.guild_sync_calls = 0
        self.pause_calls = 0
        self.resume_calls = 0

    async def sync_ready(self, *, is_current: Callable[[], bool]) -> None:
        assert is_current() is True
        self.guild_sync_calls += 1

    async def pause_sync(self) -> None:
        self.pause_calls += 1

    async def resume_sync(self, *, is_current: Callable[[], bool]) -> None:
        assert is_current() is True
        self.resume_calls += 1


class Shutdown:
    closed = False


async def _empty_sync() -> list[object]:
    return []


def _command_sync(
    tree: FakeTree,
    *,
    port: FakeGuildSyncPort | None = None,
    timeout: float = 0.01,
) -> DiscordCommandSync:
    return DiscordCommandSync(
        tree=cast(app_commands.CommandTree, tree),
        get_guild_sync_port=lambda: port,
        config=CommandSyncConfig(drain_timeout_seconds=timeout),
        shutdown=Shutdown(),
    )


@pytest.mark.asyncio
async def test_overlapping_ready_publications_share_one_global_put() -> None:
    concurrent = 0
    peak = 0
    calls = 0

    async def slow_sync() -> list[object]:
        nonlocal calls, concurrent, peak
        calls += 1
        concurrent += 1
        peak = max(peak, concurrent)
        await asyncio.sleep(0)
        concurrent -= 1
        return []

    command_sync = _command_sync(FakeTree(slow_sync))

    await asyncio.gather(command_sync.sync_for_ready(), command_sync.sync_for_ready())

    assert peak == 1
    assert calls == 1


@pytest.mark.asyncio
async def test_fast_sync_stays_cached_until_same_generation_cohort_leaves() -> None:
    calls = 0
    both_entered = asyncio.Event()
    entered = 0

    async def fast_sync() -> list[object]:
        nonlocal calls
        calls += 1
        return []

    command_sync = _command_sync(FakeTree(fast_sync))

    async def cohort_member() -> None:
        nonlocal entered
        async with command_sync.ready_cohort() as generation:
            entered += 1
            if entered == 2:
                both_entered.set()
            await both_entered.wait()
            await command_sync.sync_for_ready(generation)

    await asyncio.gather(cohort_member(), cohort_member())

    assert calls == 1
    assert command_sync_state(command_sync).global_sync_task is None


@pytest.mark.asyncio
async def test_completed_cache_is_not_shared_across_gateway_generations() -> None:
    calls = 0
    old_cached = asyncio.Event()
    release_old_cohort = asyncio.Event()

    async def fast_sync() -> list[object]:
        nonlocal calls
        calls += 1
        return []

    command_sync = _command_sync(FakeTree(fast_sync), port=FakeGuildSyncPort())

    async def old_cohort() -> None:
        async with command_sync.ready_cohort() as generation:
            await command_sync.sync_for_ready(generation)
            old_cached.set()
            await release_old_cohort.wait()

    old_ready = asyncio.create_task(old_cohort())
    await old_cached.wait()
    assert command_sync_state(command_sync).global_sync_task is not None

    await command_sync.disconnect()
    async with command_sync.ready_cohort() as generation:
        await command_sync.sync_for_ready(generation)

    release_old_cohort.set()
    await old_ready
    assert calls == 2


@pytest.mark.asyncio
async def test_disconnect_cancels_only_its_captured_predecessor_set() -> None:
    pause_started = asyncio.Event()
    release_pause = asyncio.Event()
    global_started = asyncio.Event()
    release_global = asyncio.Event()
    global_completed = False

    class BlockingPausePort(FakeGuildSyncPort):
        async def pause_sync(self) -> None:
            self.pause_calls += 1
            pause_started.set()
            await release_pause.wait()

    async def global_sync() -> list[object]:
        nonlocal global_completed
        global_started.set()
        await release_global.wait()
        global_completed = True
        return []

    port = BlockingPausePort()
    command_sync = _command_sync(FakeTree(global_sync), port=port)

    disconnecting = asyncio.create_task(command_sync.disconnect())
    await pause_started.wait()
    newer = asyncio.create_task(command_sync.sync_for_ready())
    await global_started.wait()

    release_pause.set()
    await disconnecting
    assert newer.done() is False

    release_global.set()
    await newer
    assert global_completed is True
    assert port.guild_sync_calls == 1


@pytest.mark.asyncio
async def test_cancellation_preserves_tasks_retired_after_its_snapshot() -> None:
    old_started = asyncio.Event()
    release_old = asyncio.Event()
    release_new = asyncio.Event()

    async def stubborn_old() -> None:
        old_started.set()
        while not release_old.is_set():
            try:
                await release_old.wait()
            except asyncio.CancelledError:
                continue

    async def wait_for_new_release() -> None:
        await release_new.wait()

    command_sync = _command_sync(FakeTree(_empty_sync))
    old = asyncio.create_task(stubborn_old())
    newly_retired = asyncio.create_task(wait_for_new_release())
    await old_started.wait()
    set_command_sync_retired_tasks(command_sync, (old,))

    cancelling = asyncio.create_task(command_sync.cancel_all())
    await asyncio.sleep(0)
    set_command_sync_retired_tasks(command_sync, (old, newly_retired))
    await cancelling

    retired = command_sync_state(command_sync).retired_global_sync_tasks
    assert old in retired
    assert newly_retired in retired

    release_old.set()
    release_new.set()
    await asyncio.gather(old, newly_retired)
    set_command_sync_retired_tasks(command_sync, ())


@pytest.mark.asyncio
async def test_stubborn_old_global_put_blocks_put_but_not_guild_reconciliation() -> None:
    old_started = asyncio.Event()
    release_old = asyncio.Event()
    global_calls = 0

    async def stubborn_global_sync() -> list[object]:
        nonlocal global_calls
        global_calls += 1
        old_started.set()
        while not release_old.is_set():
            try:
                await release_old.wait()
            except asyncio.CancelledError:
                continue
        return []

    port = FakeGuildSyncPort()
    command_sync = _command_sync(FakeTree(stubborn_global_sync), port=port)
    old_publication = asyncio.create_task(command_sync.sync_for_ready())
    await old_started.wait()

    await command_sync.disconnect()
    await command_sync.sync_for_ready()

    assert global_calls == 1
    assert port.guild_sync_calls == 1
    assert command_sync_state(command_sync).retired_global_sync_tasks

    release_old.set()
    await old_publication
    await command_sync.cancel_all()
