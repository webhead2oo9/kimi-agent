"""Bounded cancellation for module-owned coroutines.

``asyncio.wait_for`` cancels at the deadline but then waits, unbounded, for the
cancelled coroutine to finish. Module code is trusted but not infallible: a
handler that swallows ``CancelledError`` or blocks in cleanup would hold
startup or shutdown forever. These helpers give cancellation a grace period
and then abandon the task, logging it, so the host's ceilings are real.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Coroutine, Iterable
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_CANCEL_GRACE_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class BoundedOutcome:
    """How a bounded run ended.

    Exactly one of these holds: it finished (``error`` is None), it raised
    (``error`` set), it was cancelled from elsewhere (``cancelled``), or it hit
    the deadline (``timed_out``; ``abandoned`` says whether it also ignored
    cancellation and was left running).
    """

    timed_out: bool = False
    abandoned: bool = False
    cancelled: bool = False
    error: BaseException | None = None

    @property
    def completed(self) -> bool:
        return not (self.timed_out or self.cancelled or self.error is not None)


async def run_bounded(
    coro: Coroutine[Any, Any, Any],
    *,
    timeout: float,
    grace: float = DEFAULT_CANCEL_GRACE_SECONDS,
    what: str,
) -> BoundedOutcome:
    """Run ``coro`` with a deadline; cancel past it and abandon after ``grace``.

    An exception raised by the coroutine itself, including a ``TimeoutError``
    of its own, is reported in ``error`` and never mistaken for the deadline.
    """
    task = asyncio.ensure_future(coro)
    try:
        done, _pending = await asyncio.wait({task}, timeout=timeout)
    except BaseException:
        # The caller was cancelled (shutdown, Ctrl-C): the coroutine must not
        # keep running against a torn-down application.
        await cancel_with_grace((task,), grace=grace, what=what)
        raise
    if not done:
        abandoned = not await cancel_with_grace((task,), grace=grace, what=what)
        return BoundedOutcome(timed_out=True, abandoned=abandoned)
    if task.cancelled():
        return BoundedOutcome(cancelled=True)
    return BoundedOutcome(error=task.exception())


async def cancel_with_grace(tasks: Iterable[asyncio.Task[Any]], *, grace: float, what: str) -> bool:
    """Cancel ``tasks`` and wait up to ``grace`` for them; False if any were abandoned."""
    pending = {task for task in tasks if not task.done()}
    for task in pending:
        task.cancel()
    if not pending:
        return True
    done, still_pending = await asyncio.wait(pending, timeout=grace)
    for task in done:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            task.result()
    for task in still_pending:
        # The coroutine ignored cancellation. Leave it to the loop and move on;
        # a stuck cleanup must not hold every other module hostage.
        log.error("%s did not stop within %gs after cancellation; abandoning", what, grace)
        task.add_done_callback(_consume)
    return not still_pending


def _consume(task: asyncio.Task[Any]) -> None:
    with contextlib.suppress(asyncio.CancelledError, Exception):
        task.result()


__all__ = ["DEFAULT_CANCEL_GRACE_SECONDS", "BoundedOutcome", "cancel_with_grace", "run_bounded"]
