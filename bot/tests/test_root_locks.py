from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast

import pytest

from app.root_locks import RootLockPool
from storage.conversations import ConversationStore


@pytest.mark.asyncio
async def test_root_lock_evicts_entry_after_last_holder_releases() -> None:
    locks = RootLockPool()

    async with locks.hold("root:1"):
        snapshot = locks.snapshot()
        assert snapshot.keys == ("root:1",)
        assert snapshot.refcounts == {"root:1": 1}

    snapshot = locks.snapshot()
    assert snapshot.keys == ()
    assert snapshot.refcounts == {}


@pytest.mark.asyncio
async def test_concurrent_same_root_acquirers_share_one_lock() -> None:
    locks = RootLockPool()
    started = asyncio.Event()
    release = asyncio.Event()

    async def hold() -> None:
        async with locks.hold("root:1"):
            started.set()
            await release.wait()

    async def waiter() -> None:
        async with locks.hold("root:1"):
            pass

    first = asyncio.create_task(hold())
    await started.wait()
    second = asyncio.create_task(waiter())
    await asyncio.sleep(0)

    snapshot = locks.snapshot()
    assert snapshot.keys == ("root:1",)
    assert snapshot.refcounts == {"root:1": 2}

    release.set()
    await asyncio.gather(first, second)
    snapshot = locks.snapshot()
    assert snapshot.keys == ()
    assert snapshot.refcounts == {}


@pytest.mark.asyncio
async def test_root_lock_evicts_entry_when_holder_raises() -> None:
    locks = RootLockPool()

    with pytest.raises(RuntimeError, match="boom"):
        async with locks.hold("root:1"):
            raise RuntimeError("boom")

    snapshot = locks.snapshot()
    assert snapshot.keys == ()
    assert snapshot.refcounts == {}


@pytest.mark.asyncio
async def test_user_conversation_roots_are_acquired_in_sorted_order() -> None:
    acquired: list[str] = []

    class RecordingPool(RootLockPool):
        @asynccontextmanager
        async def hold(self, key: str) -> AsyncIterator[None]:
            acquired.append(key)
            async with super().hold(key):
                yield

    class AffectedRoots:
        async def list_user_conversation_keys(self, user_id: str) -> list[str]:
            assert user_id == "alice"
            return ["root:z", "root:a", "root:m", "root:a"]

    locks = RecordingPool()
    store = cast(ConversationStore, AffectedRoots())

    async with locks.hold_user_conversations("alice", store):
        assert acquired == ["root:a", "root:m", "root:z"]
        assert locks.snapshot().refcounts == {
            "root:a": 1,
            "root:m": 1,
            "root:z": 1,
        }

    assert locks.snapshot().keys == ()


@pytest.mark.asyncio
async def test_user_conversation_lock_drains_an_active_shared_root() -> None:
    locks = RootLockPool()
    turn_started = asyncio.Event()
    release_turn = asyncio.Event()
    deletion_entered = asyncio.Event()

    class AffectedRoots:
        async def list_user_conversation_keys(self, user_id: str) -> list[str]:
            assert user_id == "alice"
            return ["shared-root"]

    async def active_other_user_turn() -> None:
        async with locks.hold("shared-root"):
            turn_started.set()
            await release_turn.wait()

    async def delete_after_drain() -> None:
        async with locks.hold_user_conversations(
            "alice",
            cast(ConversationStore, AffectedRoots()),
        ):
            deletion_entered.set()

    active = asyncio.create_task(active_other_user_turn())
    await turn_started.wait()
    deletion = asyncio.create_task(delete_after_drain())
    await asyncio.sleep(0)
    assert not deletion_entered.is_set()

    release_turn.set()
    await asyncio.gather(active, deletion)
    assert deletion_entered.is_set()
