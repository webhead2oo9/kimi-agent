"""Synchronization helpers for white-box module event-bus tests."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

from modules.events import EventBusImpl


async def drain_event_bus(bus: EventBusImpl, module_name: str | None = None) -> None:
    """Wait for queued handlers without adding a test method to the runtime API."""

    lanes = [bus._lanes[module_name]] if module_name else list(bus._lanes.values())
    for lane in lanes:
        if lane.queue or lane.in_flight:
            await asyncio.wait_for(lane.idle.wait(), timeout=5.0)


def event_bus_metrics(bus: EventBusImpl, module_name: str) -> Mapping[str, float]:
    """Inspect one private lane's counters without widening the runtime API."""

    lane = bus._lanes.get(module_name)
    return lane.metrics() if lane is not None else {}


def event_bus_subscriptions(bus: EventBusImpl, module_name: str) -> tuple[str, ...]:
    """Inspect live subscriptions without adding a production query surface."""

    return tuple(
        subscription.pattern
        for subscription in bus._subscriptions
        if subscription.module_name == module_name and not subscription.closed
    )
