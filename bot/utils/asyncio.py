"""Small asyncio lifecycle helpers shared by resource-owning code."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable


async def await_uncancellable[T](awaitable: Awaitable[T]) -> T:
    """Finish ``awaitable`` before propagating cancellation from this task.

    This is for short, mandatory finalizers whose interruption would leak a
    resource. Cancellation of the awaited operation itself still propagates.
    """
    task = asyncio.ensure_future(awaitable)
    cancellation: asyncio.CancelledError | None = None
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError as exc:
            if task.cancelled():
                raise
            cancellation = cancellation or exc
        except BaseException as exc:
            if cancellation is not None:
                raise cancellation from exc
            raise
    if cancellation is not None:
        raise cancellation
    return result
