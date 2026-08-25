"""Per-user barrier between ordinary activity and on-demand privacy deletion.

Ordinary turns take a shared activity lease for their whole externally visible
lifetime (preparation, tools, delivery, and persistence).  ``/privacy`` takes an
exclusive deletion lease.  Once a deletion is waiting, no new activity for that
user starts until the deletion finishes; activity that was already running is
allowed to finish and is then included in the wipe.

The live lease coordinator is in-process; durable authorization is stored
separately in SQLite so a confirmed deletion survives a hard restart. A pending
durable request marks this coordinator before destructive work begins. If an
attempt is incomplete, later activity for that user remains blocked so a retry
cannot unexpectedly erase state created after the original request. Leases are
re-entrant within one asyncio task. Child tasks explicitly entering ``activity``
join the inherited open lease group, so mutable worker work can outlive a timed-
out surface without deadlocking a deletion already waiting for that surface.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any


class PrivacyDeletionPendingError(RuntimeError):
    """Ordinary activity is blocked by an incomplete durable deletion request."""


@dataclass
class _UserState:
    active: int = 0
    waiting_activity: int = 0
    waiting_deletions: int = 0
    deleting: bool = False
    pending_deletion: bool = False


@dataclass
class _ActivityGroup:
    """One top-level surface and the descendant tasks it explicitly guards."""

    state: _UserState
    tasks: set[asyncio.Task[Any]]
    open: bool = True


class UserPrivacyBarrier:
    """Coordinate user activity with a complete, on-demand data deletion."""

    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._states: dict[str, _UserState] = {}
        # A child inherits the group object but must explicitly call activity()
        # to join it. The task set distinguishes same-task nesting from a guarded
        # child that keeps the group active after its surface times out.
        self._activity_groups: ContextVar[dict[str, _ActivityGroup] | None] = ContextVar(
            f"privacy_activity_groups_{id(self)}",
            default=None,
        )
        self._deletion_owners: ContextVar[dict[str, asyncio.Task[Any]] | None] = ContextVar(
            f"privacy_deletion_owners_{id(self)}",
            default=None,
        )

    @asynccontextmanager
    async def activity(self, user_id: str) -> AsyncIterator[None]:
        """Hold a shared lease while one user's interaction may write state."""

        key = str(user_id)
        task = asyncio.current_task()
        if task is None:  # pragma: no cover - an async context always has a task
            raise RuntimeError("privacy activity lease requires an asyncio task")

        # A helper called by the exclusive deletion itself is already protected.
        if (self._deletion_owners.get() or {}).get(key) is task:
            yield
            return

        inherited = (self._activity_groups.get() or {}).get(key)
        group, owns_ref = await self._enter_activity(key, task, inherited)
        if not owns_ref:
            # Same-task nesting. The outer context owns this task's only ref.
            yield
            return

        groups = dict(self._activity_groups.get() or {})
        groups[key] = group
        token = self._activity_groups.set(groups)
        try:
            yield
        finally:
            self._activity_groups.reset(token)
            await self._leave_activity(key, task, group)

    @asynccontextmanager
    async def deletion(self, user_id: str) -> AsyncIterator[None]:
        """Hold the exclusive lease used by a complete privacy deletion."""

        key = str(user_id)
        task = asyncio.current_task()
        if task is None:  # pragma: no cover - an async context always has a task
            raise RuntimeError("privacy deletion lease requires an asyncio task")

        if (self._deletion_owners.get() or {}).get(key) is task:
            yield
            return
        activity_group = (self._activity_groups.get() or {}).get(key)
        if activity_group is not None and activity_group.open:
            raise RuntimeError(
                "cannot start privacy deletion from inside the same user's activity lease"
            )

        await self._enter_deletion(key)
        owners = dict(self._deletion_owners.get() or {})
        owners[key] = task
        token = self._deletion_owners.set(owners)
        try:
            yield
        finally:
            self._deletion_owners.reset(token)
            await self._leave_deletion(key)

    async def mark_deletion_pending(self, user_id: str) -> None:
        """Block later ordinary activity until the durable request completes."""

        key = str(user_id)
        async with self._condition:
            self._state(key).pending_deletion = True
            self._condition.notify_all()

    async def clear_deletion_pending(self, user_id: str) -> None:
        """Release activity after the matching durable request is completed."""

        key = str(user_id)
        async with self._condition:
            state = self._states.get(key)
            if state is None:
                return
            state.pending_deletion = False
            self._cleanup(key, state)
            self._condition.notify_all()

    def _state(self, key: str) -> _UserState:
        state = self._states.get(key)
        if state is None:
            state = _UserState()
            self._states[key] = state
        return state

    async def _enter_activity(
        self,
        key: str,
        task: asyncio.Task[Any],
        inherited: _ActivityGroup | None,
    ) -> tuple[_ActivityGroup, bool]:
        async with self._condition:
            state = self._state(key)
            if inherited is not None and inherited.open and inherited.state is state:
                if task in inherited.tasks:
                    return inherited, False
                # Descendant work belongs to activity that began before the
                # queued deletion. Join even though new top-level activity is
                # writer-blocked; deletion already waits for this group and
                # includes all of its writes in the subsequent wipe.
                inherited.tasks.add(task)
                return inherited, True

            if state.pending_deletion:
                raise PrivacyDeletionPendingError(
                    f"Privacy deletion is still pending for user {key}."
                )

            state.waiting_activity += 1
            try:
                # Prefer a queued deletion over later activity.  The first
                # deletion marks ``deleting`` before draining existing readers;
                # waiting_deletions keeps back-to-back deletions contiguous.
                await self._condition.wait_for(
                    lambda: (
                        state.pending_deletion
                        or (not state.deleting and state.waiting_deletions == 0)
                    )
                )
                if state.pending_deletion:
                    raise PrivacyDeletionPendingError(
                        f"Privacy deletion is still pending for user {key}."
                    )
            except BaseException:
                state.waiting_activity -= 1
                self._cleanup(key, state)
                self._condition.notify_all()
                raise
            state.waiting_activity -= 1
            state.active += 1
            return _ActivityGroup(state=state, tasks={task}), True

    async def _leave_activity(
        self,
        key: str,
        task: asyncio.Task[Any],
        group: _ActivityGroup,
    ) -> None:
        async with self._condition:
            state = group.state
            group.tasks.discard(task)
            if not group.tasks:
                group.open = False
                state.active -= 1
                if state.active < 0:  # pragma: no cover - defensive invariant
                    raise RuntimeError("privacy activity lease count underflow")
            self._cleanup(key, state)
            self._condition.notify_all()

    async def _enter_deletion(self, key: str) -> None:
        async with self._condition:
            state = self._state(key)
            state.waiting_deletions += 1
            marked_deleting = False
            try:
                await self._condition.wait_for(lambda: not state.deleting)
                state.deleting = True
                marked_deleting = True
                state.waiting_deletions -= 1
                await self._condition.wait_for(lambda: state.active == 0)
            except BaseException:
                if marked_deleting:
                    state.deleting = False
                else:
                    state.waiting_deletions -= 1
                self._cleanup(key, state)
                self._condition.notify_all()
                raise

    async def _leave_deletion(self, key: str) -> None:
        async with self._condition:
            state = self._states[key]
            state.deleting = False
            self._cleanup(key, state)
            self._condition.notify_all()

    def _cleanup(self, key: str, state: _UserState) -> None:
        if (
            state.active == 0
            and state.waiting_activity == 0
            and state.waiting_deletions == 0
            and not state.deleting
            and not state.pending_deletion
            and self._states.get(key) is state
        ):
            self._states.pop(key, None)
