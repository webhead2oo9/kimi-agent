"""Immediate, bounded admission for user-triggered model turns.

The provider semaphore limits only the time spent inside an LLM call.  A turn
does substantial work before and between those calls, so it needs a separate
boundary that cannot accumulate an unbounded queue of waiting tasks.  This
controller therefore never waits for capacity: callers either receive a lease
immediately or reject the surface with a short retry message.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum
from types import TracebackType


TURN_ADMISSION_BUSY_MESSAGE = (
    "I'm handling as many requests as I can right now. Please try again in a moment."
)


class AdmissionRejection(StrEnum):
    """Why a turn was not admitted."""

    USER_LIMIT = "user_limit"
    GLOBAL_LIMIT = "global_limit"


@dataclass(frozen=True, slots=True)
class AdmissionDecision:
    """The result of one non-waiting admission attempt."""

    lease: TurnAdmissionLease | None
    rejection: AdmissionRejection | None = None

    @property
    def admitted(self) -> bool:
        return self.lease is not None


@dataclass(frozen=True, slots=True)
class AdmissionSnapshot:
    """A consistent view used by diagnostics and focused tests."""

    active_total: int
    active_by_user: dict[str, int]


class TurnAdmissionLease:
    """One admitted turn; releasing it is safe to repeat."""

    __slots__ = ("_controller", "_released", "user_id")

    def __init__(self, controller: TurnAdmissionController, user_id: str) -> None:
        self._controller = controller
        self.user_id = user_id
        self._released = False

    async def __aenter__(self) -> TurnAdmissionLease:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.release()

    async def release(self) -> None:
        release_task = asyncio.create_task(self._controller._release(self))
        cancellation_seen = False
        while not release_task.done():
            try:
                await asyncio.shield(release_task)
            except asyncio.CancelledError:
                # Capacity is a process-wide availability invariant. Complete the
                # tiny counter update even if this turn is cancelled repeatedly.
                cancellation_seen = True
            except BaseException:
                break
        if cancellation_seen:
            # Retrieve any child exception before preserving caller cancellation.
            if not release_task.cancelled():
                release_task.exception()
            raise asyncio.CancelledError
        await release_task


class TurnAdmissionController:
    """Bound active turns per user and across the process without queueing."""

    def __init__(self, *, max_active: int, max_active_per_user: int) -> None:
        if max_active <= 0:
            raise ValueError("max_active must be a positive integer")
        if max_active_per_user <= 0:
            raise ValueError("max_active_per_user must be a positive integer")
        self.max_active = max_active
        self.max_active_per_user = max_active_per_user
        self._lock = asyncio.Lock()
        self._active_total = 0
        self._active_by_user: dict[str, int] = {}

    async def try_acquire(self, user_id: str) -> AdmissionDecision:
        """Acquire immediately or reject; this method never queues for a slot."""

        key = str(user_id)
        async with self._lock:
            user_active = self._active_by_user.get(key, 0)
            if user_active >= self.max_active_per_user:
                return AdmissionDecision(
                    lease=None,
                    rejection=AdmissionRejection.USER_LIMIT,
                )
            if self._active_total >= self.max_active:
                return AdmissionDecision(
                    lease=None,
                    rejection=AdmissionRejection.GLOBAL_LIMIT,
                )
            self._active_total += 1
            self._active_by_user[key] = user_active + 1
            return AdmissionDecision(lease=TurnAdmissionLease(self, key))

    async def snapshot(self) -> AdmissionSnapshot:
        async with self._lock:
            return AdmissionSnapshot(
                active_total=self._active_total,
                active_by_user=dict(self._active_by_user),
            )

    async def _release(self, lease: TurnAdmissionLease) -> None:
        async with self._lock:
            if lease._released:
                return
            lease._released = True
            user_active = self._active_by_user.get(lease.user_id, 0)
            if user_active <= 0 or self._active_total <= 0:  # pragma: no cover
                raise RuntimeError("turn admission lease count underflow")
            if user_active == 1:
                del self._active_by_user[lease.user_id]
            else:
                self._active_by_user[lease.user_id] = user_active - 1
            self._active_total -= 1
