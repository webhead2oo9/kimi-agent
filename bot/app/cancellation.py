from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
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


class ActiveOperationRegistry:
    """Out-of-band cancellation index for ordinary foreground turns."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._operations: dict[str, ActiveOperation] = {}

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
                    owned = [operation for operation in owned if operation.root_key == root_key]
                else:
                    owned = [operation for operation in owned if operation.channel_id == channel_id]
            if not owned:
                return len(matched_scopes), True

            # A response root and its detached mutable children share one scope.
            # Count it once in the user-facing STOP result, but cancel and drain
            # every task. Rescanning after the roots exit closes the race where a
            # child registers immediately after the first cancellation snapshot.
            matched_scopes.update((operation.root_key, operation.channel_id) for operation in owned)
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
