from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, replace
from uuid import uuid4


@dataclass(frozen=True, slots=True)
class ActiveOperation:
    id: str
    user_id: str
    root_key: str
    channel_id: str
    task: asyncio.Task[object]
    cancel_on_stop: bool
    stop_event: asyncio.Event
    provisional: bool = False


class ActiveOperationRegistry:
    """Out-of-band cancellation index for ordinary foreground turns."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._operations: dict[str, ActiveOperation] = {}

    @contextmanager
    def register_provisional(
        self,
        *,
        user_id: str,
        channel_id: str,
    ) -> Iterator[None]:
        """Synchronously cover an admitted turn until its rooted registration."""
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("active operation registration requires an asyncio task")
        operation = ActiveOperation(
            id=uuid4().hex,
            user_id=user_id,
            root_key="",
            channel_id=channel_id,
            task=task,
            cancel_on_stop=True,
            stop_event=asyncio.Event(),
            provisional=True,
        )
        # All registry mutation occurs on the event-loop thread. Keeping this
        # insertion synchronous closes the post-admission race before the turn's
        # next await; cancel() never awaits while inspecting the dictionary.
        self._operations[operation.id] = operation
        try:
            yield
        finally:
            self._operations.pop(operation.id, None)

    def bind_current_provisional(self, root_key: str) -> None:
        """Narrow the current turn's provisional STOP scope to its resolved root."""

        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("active operation binding requires an asyncio task")
        # Like provisional insertion/removal, binding runs synchronously on the
        # event-loop thread. Once resolution returns there is no await at which
        # a root-scoped STOP can still mistake this turn for an unknown root.
        for operation_id, operation in tuple(self._operations.items()):
            if operation.task is task and operation.provisional:
                self._operations[operation_id] = replace(
                    operation,
                    root_key=root_key,
                    provisional=False,
                )

    @asynccontextmanager
    async def register(
        self,
        *,
        user_id: str,
        root_key: str,
        channel_id: str,
        cancel_on_stop: bool = True,
        stop_event: asyncio.Event | None = None,
    ) -> AsyncIterator[None]:
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("active operation registration requires an asyncio task")
        operation = ActiveOperation(
            id=uuid4().hex,
            user_id=user_id,
            root_key=root_key,
            channel_id=channel_id,
            task=task,
            cancel_on_stop=cancel_on_stop,
            stop_event=stop_event or asyncio.Event(),
        )
        async with self._lock:
            self._operations[operation.id] = operation
        try:
            yield
        finally:
            async with self._lock:
                self._operations.pop(operation.id, None)

    async def cancel(
        self,
        *,
        user_id: str,
        root_key: str | None,
        channel_id: str,
        all_operations: bool,
        wait_seconds: float,
    ) -> tuple[int, bool]:
        current = asyncio.current_task()
        deadline = asyncio.get_running_loop().time() + max(0.0, wait_seconds)
        matched_scopes: set[tuple[str, str]] = set()
        while True:
            async with self._lock:
                owned = [
                    operation
                    for operation in self._operations.values()
                    if operation.user_id == user_id and operation.task is not current
                ]
            if not all_operations:
                if root_key:
                    owned = [
                        operation
                        for operation in owned
                        if operation.root_key == root_key
                        or (operation.provisional and operation.channel_id == channel_id)
                    ]
                else:
                    owned = [operation for operation in owned if operation.channel_id == channel_id]
            if not owned:
                return len(matched_scopes), True

            # A response root and its detached mutable children share one scope.
            # Count it once in the user-facing STOP result, but cancel and drain
            # every task. Rescanning after the roots exit closes the race where a
            # child registers immediately after the first cancellation snapshot.
            rooted_tasks = {
                operation.task: operation.root_key
                for operation in owned
                if not operation.provisional
            }
            matched_scopes.update(
                (
                    rooted_tasks.get(operation.task)
                    or operation.root_key
                    or root_key
                    or f"channel:{operation.channel_id}",
                    operation.channel_id,
                )
                for operation in owned
            )
            for operation in owned:
                operation.stop_event.set()
            tasks = {operation.task for operation in owned}
            cancellable_tasks = {operation.task for operation in owned if operation.cancel_on_stop}
            for task in cancellable_tasks:
                task.cancel()
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return len(matched_scopes), False
            done, pending = await asyncio.wait(tasks, timeout=remaining)
            for task in done:
                with contextlib.suppress(BaseException):
                    task.result()
            if pending:
                return len(matched_scopes), False

    async def cancel_all(self) -> None:
        """Cancel roots and drain every active operation, including late children."""
        current = asyncio.current_task()
        while True:
            async with self._lock:
                operations = [
                    operation
                    for operation in self._operations.values()
                    if operation.task is not current
                ]
            if not operations:
                return
            for operation in operations:
                operation.stop_event.set()
            tasks = {operation.task for operation in operations}
            for operation in operations:
                if operation.cancel_on_stop:
                    operation.task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
