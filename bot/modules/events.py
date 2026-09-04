"""In-process event bus for modules: namespaced, bounded, isolated.

``publish`` never awaits subscribers. Each subscriber module owns a bounded
queue drained by a fixed pool of workers with a per-handler timeout, so a slow
module cannot back-pressure a gateway listener or a sibling publisher. When a
module's queue is full the oldest pending event is dropped and counted in its
health metrics. Events are process-local and lost on restart; durable work
belongs in the scheduler.
"""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import logging
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from modules.tasks import DEFAULT_CANCEL_GRACE_SECONDS, cancel_with_grace

from kimi_agent_module_api.contracts import (
    CORE_TOPIC_PREFIX,
    Event,
    EventHandler,
    EventTopicError,
    ModulePermissions,
    split_topic,
    validate_publish_topic,
    validate_subscription,
)

log = logging.getLogger(__name__)

DEFAULT_QUEUE_SIZE = 256
DEFAULT_WORKERS = 4
DEFAULT_HANDLER_TIMEOUT = 30.0

type MetricsSink = Callable[[str, Mapping[str, float]], None]
type GuildPredicate = Callable[[int], bool]


@dataclass(slots=True)
class _Subscription:
    module_name: str
    pattern: str
    handler: EventHandler
    bus: EventBusImpl
    is_guild_active: GuildPredicate | None = None
    closed: bool = False

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.bus._remove(self)

    def matches(self, topic: str) -> bool:
        return fnmatch.fnmatchcase(topic, self.pattern)


@dataclass(slots=True)
class _ModuleLane:
    """One subscriber module's queue, workers, and counters."""

    module_name: str
    queue: deque[tuple[_Subscription, Event]]
    capacity: int
    wakeup: asyncio.Event = field(default_factory=asyncio.Event)
    idle: asyncio.Event = field(default_factory=asyncio.Event)
    in_flight: int = 0
    workers: list[asyncio.Task[None]] = field(default_factory=list)
    handled: int = 0
    failed: int = 0
    timed_out: int = 0
    dropped: int = 0

    def metrics(self) -> dict[str, float]:
        return {
            "events_handled": float(self.handled),
            "events_failed": float(self.failed),
            "events_timed_out": float(self.timed_out),
            "events_dropped": float(self.dropped),
            "events_pending": float(len(self.queue)),
        }


class EventBusImpl:
    def __init__(
        self,
        *,
        queue_size: int = DEFAULT_QUEUE_SIZE,
        workers: int = DEFAULT_WORKERS,
        handler_timeout: float = DEFAULT_HANDLER_TIMEOUT,
        metrics_sink: MetricsSink | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._queue_size = queue_size
        self._workers = workers
        self._handler_timeout = handler_timeout
        self._metrics_sink = metrics_sink
        self._clock = clock
        self._subscriptions: list[_Subscription] = []
        self._lanes: dict[str, _ModuleLane] = {}
        self._closed = False

    # ---- publication ----------------------------------------------------

    def publish_core(self, topic: str, payload: object) -> None:
        """Core publishes ``discord.*``; modules go through their view."""
        namespace, _ = split_topic(topic)
        if namespace != CORE_TOPIC_PREFIX:
            raise EventTopicError(f"core may only publish under {CORE_TOPIC_PREFIX!r}.*")
        self._dispatch(
            Event(topic, payload, "core", self._clock()),
            guild_id=_discord_event_guild_id(payload),
        )

    def publish_from(self, module_name: str, topic: str, payload: object) -> None:
        validate_publish_topic(module_name, topic)
        self._dispatch(Event(topic, payload, module_name, self._clock()))

    def _dispatch(self, event: Event, *, guild_id: int | None = None) -> None:
        if self._closed:
            return
        for subscription in list(self._subscriptions):
            if subscription.closed or not subscription.matches(event.topic):
                continue
            if (
                guild_id is not None
                and subscription.is_guild_active is not None
                and not subscription.is_guild_active(guild_id)
            ):
                continue
            lane = self._lanes.get(subscription.module_name)
            if lane is None:
                continue
            if len(lane.queue) >= lane.capacity:
                lane.queue.popleft()
                lane.dropped += 1
                log.warning(
                    "Module %s event queue full; dropped oldest pending event",
                    lane.module_name,
                )
            lane.queue.append((subscription, event))
            lane.idle.clear()
            lane.wakeup.set()

    # ---- subscription ---------------------------------------------------

    def subscribe(
        self,
        module_name: str,
        permissions: ModulePermissions,
        pattern: str,
        handler: EventHandler,
        *,
        is_guild_active: GuildPredicate | None = None,
    ) -> _Subscription:
        validate_subscription(module_name, permissions, pattern)
        lane = self._lanes.get(module_name)
        if lane is None:
            lane = _ModuleLane(module_name, deque(), self._queue_size)
            self._lanes[module_name] = lane
            self._start_workers(lane)
        subscription = _Subscription(
            module_name,
            pattern,
            handler,
            self,
            is_guild_active=is_guild_active,
        )
        self._subscriptions.append(subscription)
        return subscription

    def _remove(self, subscription: _Subscription) -> None:
        with contextlib.suppress(ValueError):
            self._subscriptions.remove(subscription)

    # ---- workers --------------------------------------------------------

    def _start_workers(self, lane: _ModuleLane) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        for index in range(self._workers):
            lane.workers.append(
                loop.create_task(
                    self._worker(lane), name=f"module-events:{lane.module_name}:{index}"
                )
            )

    async def _worker(self, lane: _ModuleLane) -> None:
        while True:
            if not lane.queue:
                if lane.in_flight == 0:
                    lane.idle.set()
                lane.wakeup.clear()
                await lane.wakeup.wait()
                continue
            subscription, event = lane.queue.popleft()
            if subscription.closed:
                continue
            lane.in_flight += 1
            try:
                handled = await asyncio.wait_for(
                    self._invoke_if_active(subscription, event),
                    timeout=self._handler_timeout,
                )
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                lane.timed_out += 1
                log.warning(
                    "Module %s handler for %s timed out after %.0fs",
                    lane.module_name,
                    event.topic,
                    self._handler_timeout,
                )
            except Exception:
                lane.failed += 1
                log.exception("Module %s handler for %s failed", lane.module_name, event.topic)
            else:
                if handled:
                    lane.handled += 1
            finally:
                lane.in_flight -= 1
                self._report(lane)

    async def _invoke_if_active(self, subscription: _Subscription, event: Event) -> bool:
        """Recheck queued core events in the same coroutine that invokes the handler."""

        if event.source_module == "core":
            guild_id = _discord_event_guild_id(event.payload)
            if guild_id is not None and subscription.is_guild_active is not None:
                try:
                    if not subscription.is_guild_active(guild_id):
                        return False
                except Exception:
                    # A broken lifecycle/settings predicate must not kill the
                    # lane worker or let a core Discord event run unchecked.
                    log.exception(
                        "Module %s guild-active predicate failed for %s",
                        subscription.module_name,
                        event.topic,
                    )
                    return False
        await subscription.handler(event)
        return True

    def _report(self, lane: _ModuleLane) -> None:
        if self._metrics_sink is not None:
            try:
                self._metrics_sink(lane.module_name, lane.metrics())
            except Exception:
                log.exception("Event metrics sink failed for %s", lane.module_name)

    # ---- lifecycle ------------------------------------------------------

    async def close_module(self, module_name: str) -> bool:
        """Cancel in-flight handlers, then drop the module's subscriptions."""
        stopped = True
        lane = self._lanes.pop(module_name, None)
        if lane is not None:
            stopped = await cancel_with_grace(
                lane.workers,
                grace=DEFAULT_CANCEL_GRACE_SECONDS,
                what=f"module {module_name} event handler",
            )
            lane.queue.clear()
        self._subscriptions = [s for s in self._subscriptions if s.module_name != module_name]
        return stopped

    async def close(self) -> None:
        self._closed = True
        for name in list(self._lanes):
            await self.close_module(name)


@dataclass(frozen=True, slots=True)
class ModuleEventView:
    """The ``EventBus`` port handed to one module."""

    bus: EventBusImpl
    module_name: str
    permissions: ModulePermissions
    is_guild_active: GuildPredicate | None = None

    def publish(self, topic: str, payload: object) -> None:
        self.bus.publish_from(self.module_name, topic, payload)

    def subscribe(self, pattern: str, handler: EventHandler) -> _Subscription:
        return self.bus.subscribe(
            self.module_name,
            self.permissions,
            pattern,
            handler,
            is_guild_active=self.is_guild_active,
        )


def _discord_event_guild_id(payload: object) -> int | None:
    direct = getattr(payload, "guild_id", None)
    ref = getattr(payload, "ref", None)
    message = getattr(payload, "message", None)
    member = getattr(payload, "member", None)
    invite = getattr(payload, "invite", None)
    candidates = (
        direct,
        getattr(ref, "guild_id", None),
        getattr(getattr(message, "ref", None), "guild_id", None),
        getattr(member, "guild_id", None),
        getattr(invite, "guild_id", None),
    )
    for value in candidates:
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    refs = getattr(payload, "refs", ())
    first = next(iter(refs), None)
    value = getattr(first, "guild_id", None)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


__all__ = ["EventBusImpl", "ModuleEventView"]
