from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field


@dataclass
class _LockEntry:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


# The application owns one asyncio event loop. Entries are reference-counted and
# removed when the last holder/waiter leaves so locks do not accumulate for every
# Discord user the process has ever seen (or become bound to stale test loops).
_user_locks: dict[str, _LockEntry] = {}


@asynccontextmanager
async def user_memory_mutation(user_id: str) -> AsyncIterator[None]:
    """Serialize bank and preference mutations for one Discord user.

    This is intentionally narrower than a global memory lock: different users can
    retain or delete concurrently, while a forget operation cannot be overtaken by
    an already-running bank ensure or retain for the same user.
    """
    entry = _user_locks.get(user_id)
    if entry is None:
        entry = _LockEntry()
        _user_locks[user_id] = entry
    entry.users += 1

    acquired = False
    try:
        await entry.lock.acquire()
        acquired = True
        yield
    finally:
        if acquired:
            entry.lock.release()
        entry.users -= 1
        if entry.users == 0 and _user_locks.get(user_id) is entry:
            del _user_locks[user_id]
