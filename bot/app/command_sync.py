from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Protocol

from discord import app_commands

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from app.lifecycle import ShutdownSignal


class GuildCommandSyncPort(Protocol):
    async def sync_ready(self, *, is_current: Callable[[], bool]) -> None: ...

    async def pause_sync(self) -> None: ...

    async def resume_sync(self, *, is_current: Callable[[], bool]) -> None: ...


@dataclass(frozen=True, slots=True)
class CommandSyncConfig:
    drain_timeout_seconds: float


@dataclass(frozen=True, slots=True)
class CommandSyncSnapshot:
    gateway_generation: int
    global_sync_task: asyncio.Task[None] | None
    global_sync_generation: int | None
    retired_global_sync_tasks: tuple[asyncio.Task[None], ...]
    ready_event_tasks: tuple[asyncio.Task[Any], ...]
    ready_event_generations: Mapping[asyncio.Task[Any], int]


class DiscordCommandSync:
    def __init__(
        self,
        *,
        tree: app_commands.CommandTree,
        get_guild_sync_port: Callable[[], GuildCommandSyncPort | None],
        config: CommandSyncConfig,
        shutdown: ShutdownSignal,
    ) -> None:
        self._tree = tree
        self._get_guild_sync_port = get_guild_sync_port
        self._config = config
        self._shutdown = shutdown
        self._gateway_generation = 0
        self._global_sync_task: asyncio.Task[None] | None = None
        self._global_sync_generation: int | None = None
        self._retired_global_sync_tasks: set[asyncio.Task[None]] = set()
        self._ready_event_tasks: set[asyncio.Task[Any]] = set()
        self._ready_event_generations: dict[asyncio.Task[Any], int] = {}

    @property
    def current_generation(self) -> int:
        return self._gateway_generation

    def snapshot(self) -> CommandSyncSnapshot:
        return CommandSyncSnapshot(
            gateway_generation=self._gateway_generation,
            global_sync_task=self._global_sync_task,
            global_sync_generation=self._global_sync_generation,
            retired_global_sync_tasks=tuple(self._retired_global_sync_tasks),
            ready_event_tasks=tuple(self._ready_event_tasks),
            ready_event_generations=MappingProxyType(dict(self._ready_event_generations)),
        )

    @asynccontextmanager
    async def ready_cohort(self) -> AsyncIterator[int]:
        """Register one READY caller in the current gateway-generation cohort."""

        ready_task = asyncio.current_task()
        gateway_generation = self._gateway_generation
        if ready_task is not None:
            self._ready_event_tasks.add(ready_task)
            self._ready_event_generations[ready_task] = gateway_generation
        try:
            yield gateway_generation
        finally:
            if ready_task is not None:
                self._ready_event_tasks.discard(ready_task)
                self._ready_event_generations.pop(ready_task, None)
            self._release_completed_command_sync()

    async def sync_for_ready(self, generation: int | None = None) -> None:
        """Join one same-generation publication for overlapping READY callers."""

        generation = self._gateway_generation if generation is None else generation
        while not self._shutdown.closed and generation == self._gateway_generation:
            task = self._global_sync_task
            if task is not None and self._global_sync_generation != generation:
                # Different gateway generations never share a cached result.
                # Retire an unfinished predecessor; the new generation's cached
                # coordinator gives it a bounded chance to finish before deciding
                # whether a new global PUT is safe.
                if not task.done():
                    task.cancel()
                    self._retired_global_sync_tasks.add(task)
                if self._global_sync_task is task:
                    self._global_sync_task = None
                    self._global_sync_generation = None
                continue
            if task is None:
                task = asyncio.get_running_loop().create_task(
                    self._publish_global_commands(generation),
                    name=f"discord-command-sync-{generation}",
                )
                self._global_sync_task = task
                self._global_sync_generation = generation
                task.add_done_callback(self._global_command_sync_done)
            # A cancelled READY waiter must not cancel work another READY waiter owns.
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
                if generation != self._gateway_generation or task.cancelled():
                    return
                raise
            return

    async def disconnect(self) -> None:
        self._gateway_generation += 1
        # Capture only work that predates this disconnect.  ``pause_sync`` may
        # yield while stopping a cancellation-resistant retry, and a subsequent
        # READY is allowed to start the new generation during that window.  The
        # older disconnect must not cancel that newer publication when it resumes.
        global_sync_tasks = set(self._retired_global_sync_tasks)
        if self._global_sync_task is not None:
            global_sync_tasks.add(self._global_sync_task)
        port = self._get_guild_sync_port()
        if port is not None:
            await port.pause_sync()
        await self._cancel_global_command_sync(tasks=global_sync_tasks)

    async def resume(self) -> None:
        gateway_generation = self._gateway_generation
        port = self._get_guild_sync_port()
        if port is not None:
            try:
                await port.resume_sync(
                    is_current=lambda: (
                        not self._shutdown.closed and gateway_generation == self._gateway_generation
                    )
                )
            except Exception:
                log.warning("Failed to resume guild slash command sync", exc_info=True)

    async def cancel_ready_events(
        self,
        *,
        exclude: asyncio.Task[Any] | None,
    ) -> None:
        """Bound shutdown on READY initialization and reconnect maintenance."""

        tasks = {task for task in self._ready_event_tasks if task is not exclude}
        for task in tasks:
            task.cancel()
        if not tasks:
            return
        done, pending = await asyncio.wait(
            tasks,
            timeout=self._config.drain_timeout_seconds,
        )
        for task in done:
            with suppress(BaseException):
                task.result()
        if pending:
            log.warning(
                "Timed out waiting for %d READY event task(s) during shutdown",
                len(pending),
            )

    async def cancel_all(self) -> None:
        await self._cancel_global_command_sync()

    async def _publish_global_commands(self, generation: int) -> None:
        current = asyncio.current_task()
        predecessors = {
            task
            for task in self._retired_global_sync_tasks
            if task is not current and not task.done()
        }
        pending_predecessors: set[asyncio.Task[None]] = set()
        if predecessors:
            done, pending_predecessors = await asyncio.wait(
                predecessors,
                timeout=self._config.drain_timeout_seconds,
            )
            for completed in done:
                with suppress(BaseException):
                    completed.result()
                self._retired_global_sync_tasks.discard(completed)

        if pending_predecessors:
            # Never overlap Discord's global bulk-replace endpoint. Guild scopes
            # are independent and still reconcile below so READY can complete.
            log.warning(
                "Skipping global command sync for gateway generation %s; "
                "%d prior sync task(s) are still stopping",
                generation,
                len(pending_predecessors),
            )
        elif not self._shutdown.closed and generation == self._gateway_generation:
            try:
                synced = await self._tree.sync()
                log.info("Synced %d slash command(s)", len(synced))
            except Exception:
                # Command propagation is retried on the next READY, but a transient
                # transport failure must not prevent local sweepers from starting.
                log.warning("Failed to sync global slash commands", exc_info=True)

        await self._sync_guild_commands_for_generation(generation)

    async def _sync_guild_commands_for_generation(self, generation: int) -> None:
        port = self._get_guild_sync_port()
        if (
            port is not None
            and not self._shutdown.closed
            and generation == self._gateway_generation
        ):
            try:
                await port.sync_ready(
                    is_current=lambda: (
                        not self._shutdown.closed and generation == self._gateway_generation
                    )
                )
            except Exception:
                log.warning("Failed to prepare guild slash command sync", exc_info=True)

    def _global_command_sync_done(self, task: asyncio.Task[None]) -> None:
        self._retired_global_sync_tasks.discard(task)
        with suppress(asyncio.CancelledError):
            error = task.exception()
            if error is not None:
                log.error(
                    "Discord command sync task failed",
                    exc_info=(type(error), error, error.__traceback__),
                )
        if self._global_sync_task is task:
            self._release_completed_command_sync()

    def _release_completed_command_sync(self) -> None:
        task = self._global_sync_task
        # Keep a fast completed publication cached until every overlapping READY
        # event leaves its cohort. A later member then joins the same result
        # instead of issuing a duplicate PUT merely because the first PUT was fast.
        generation = self._global_sync_generation
        active_generation_tasks = (
            any(
                self._ready_event_generations.get(ready_task, generation) == generation
                for ready_task in self._ready_event_tasks
            )
            if generation is not None
            else bool(self._ready_event_tasks)
        )
        if task is not None and task.done() and not active_generation_tasks:
            self._global_sync_task = None
            self._global_sync_generation = None

    async def _cancel_global_command_sync(
        self,
        *,
        tasks: set[asyncio.Task[None]] | None = None,
    ) -> None:
        """Bound cancellation to a stable task snapshot.

        ``tasks`` lets a disconnect cancel only publications that existed when
        that disconnect began.  Shutdown omits it and snapshots all known work.
        """

        current = asyncio.current_task()
        active = self._global_sync_task
        targets = (
            set(self._retired_global_sync_tasks) | ({active} if active is not None else set())
            if tasks is None
            else set(tasks)
        )
        running = {task for task in targets if task is not current and not task.done()}
        for task in running:
            task.cancel()
        done: set[asyncio.Task[None]] = set()
        pending: set[asyncio.Task[None]] = set()
        if running:
            done, pending = await asyncio.wait(
                running,
                timeout=self._config.drain_timeout_seconds,
            )
        for task in done:
            with suppress(BaseException):
                task.result()
        completed_after_wait = {task for task in pending if task.done()}
        for task in completed_after_wait:
            with suppress(BaseException):
                task.result()
        pending.difference_update(completed_after_wait)
        if pending:
            log.warning("Timed out cancelling %d Discord command sync task(s)", len(pending))
        # Preserve tasks retired by another lifecycle callback while this one
        # was awaiting its bounded cancellation window.
        self._retired_global_sync_tasks.difference_update(targets - pending)
        self._retired_global_sync_tasks.update(pending)
        if active is not None and active in targets and self._global_sync_task is active:
            self._global_sync_task = None
            self._global_sync_generation = None
