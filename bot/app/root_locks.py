from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from storage.conversations import ConversationStore


@dataclass(frozen=True, slots=True)
class RootLockSnapshot:
    keys: tuple[str, ...]
    refcounts: Mapping[str, int]


class RootLockPool:
    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._refcounts: dict[str, int] = {}

    def snapshot(self) -> RootLockSnapshot:
        return RootLockSnapshot(
            keys=tuple(sorted(self._locks)),
            refcounts=MappingProxyType(dict(self._refcounts)),
        )

    @asynccontextmanager
    async def hold(self, key: str) -> AsyncIterator[None]:
        # Per-root serialization lock with refcounted eviction so context_locks
        # does not grow unbounded across fresh-mention roots (each root key
        # embeds a unique trigger snowflake). The get-or-create + refcount bump
        # run synchronously before the first await (the lock acquire), so a
        # concurrent acquirer for the same root always sees the same Lock object;
        # the entry is evicted only once the last holder/waiter releases.
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        self._refcounts[key] = self._refcounts.get(key, 0) + 1
        try:
            async with lock:
                yield
        finally:
            count = self._refcounts[key] - 1
            if count <= 0:
                self._refcounts.pop(key, None)
                self._locks.pop(key, None)
            else:
                self._refcounts[key] = count

    @asynccontextmanager
    async def hold_user_conversations(
        self,
        user_id: str,
        conversation_store: ConversationStore | None,
    ) -> AsyncIterator[None]:
        """Drain every active root whose transcript a deletion will mutate."""

        if conversation_store is None:
            # Before the database is initialized there are no transcripts to
            # drain; a deletion request at that point has nothing to wait for.
            yield
            return
        keys = await conversation_store.list_user_conversation_keys(user_id)
        async with AsyncExitStack() as stack:
            # Stable ordering prevents two simultaneous user deletions whose
            # shared roots overlap from deadlocking while they drain turns.
            for key in sorted(set(keys)):
                await stack.enter_async_context(self.hold(key))
            yield
