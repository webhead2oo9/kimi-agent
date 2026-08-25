"""Fail-closed process-wide lease for the shared VPN network namespace."""

from __future__ import annotations

import asyncio
from types import TracebackType


class NetnsLeaseSafetyError(RuntimeError):
    """An operation could not prove that its netns process surface stopped."""


class NetnsLeasePoisonedError(RuntimeError):
    """The shared namespace cannot be handed off safely before restart."""


class NetnsLease:
    """A one-slot async lease that permanently fails closed when poisoned.

    Unlike ``asyncio.Lock``, poisoning wakes queued callers and makes future
    acquisitions fail instead of hanging behind a holder whose process teardown
    could not be confirmed. Safety errors raised inside ``async with`` poison and
    release atomically, so no waiter can acquire in between those state changes.
    """

    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._held = False
        self._poisoned = False

    @property
    def poisoned(self) -> bool:
        return self._poisoned

    def locked(self) -> bool:
        return self._held

    async def acquire(self) -> bool:
        async with self._condition:
            while self._held and not self._poisoned:
                await self._condition.wait()
            if self._poisoned:
                raise NetnsLeasePoisonedError(
                    "The VPN network namespace is unavailable until restart."
                )
            self._held = True
            return True

    async def release(self) -> None:
        async with self._condition:
            if not self._held:
                raise RuntimeError("VPN network namespace lease is not held")
            self._held = False
            self._condition.notify_all()

    async def poison(self) -> None:
        async with self._condition:
            self._poisoned = True
            self._condition.notify_all()

    async def __aenter__(self) -> NetnsLease:
        await self.acquire()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        del exc_type, traceback
        async with self._condition:
            if isinstance(exc_value, NetnsLeaseSafetyError):
                self._poisoned = True
            if not self._held:
                raise RuntimeError("VPN network namespace lease is not held")
            self._held = False
            self._condition.notify_all()
        return False
