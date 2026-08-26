"""Per-module health state, bounded and owner-visible."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from kimi_agent_module_api.contracts import (
    HEALTH_DETAIL_MAX_LENGTH,
    HEALTH_METRICS_MAX_KEYS,
    HealthState,
    ModuleHealth,
)

log = logging.getLogger(__name__)

_SECRET_KEY_MARKERS = ("token", "secret", "key", "password", "credential")


def _bounded_metrics(metrics: Mapping[str, float] | None) -> dict[str, float]:
    bounded: dict[str, float] = {}
    for name, value in (metrics or {}).items():
        if len(bounded) >= HEALTH_METRICS_MAX_KEYS:
            break
        lowered = name.lower()
        if any(marker in lowered for marker in _SECRET_KEY_MARKERS):
            continue
        try:
            bounded[name[:64]] = float(value)
        except TypeError, ValueError:
            continue
    return bounded


@dataclass(slots=True)
class HealthRegistry:
    """Owns the health of every active module and publishes transitions."""

    clock: Callable[[], float] = time.time
    on_change: Callable[[str, ModuleHealth], None] | None = None
    _states: dict[str, ModuleHealth] = field(default_factory=dict)

    def snapshot(self) -> Mapping[str, ModuleHealth]:
        return dict(self._states)

    def get(self, module_name: str) -> ModuleHealth | None:
        return self._states.get(module_name)

    def set(
        self,
        module_name: str,
        state: HealthState,
        detail: str = "",
        metrics: Mapping[str, float] | None = None,
    ) -> ModuleHealth:
        health = ModuleHealth(
            state=state,
            detail=detail[:HEALTH_DETAIL_MAX_LENGTH],
            metrics=_bounded_metrics(metrics),
            updated_at=self.clock(),
        )
        previous = self._states.get(module_name)
        self._states[module_name] = health
        if previous is None or previous.state != state:
            log.info("Kimi module %s is %s%s", module_name, state, f": {detail}" if detail else "")
        if self.on_change is not None:
            try:
                self.on_change(module_name, health)
            except Exception:
                log.exception("Module health observer failed for %s", module_name)
        return health

    def forget(self, module_name: str) -> None:
        self._states.pop(module_name, None)

    def reporter_for(self, module_name: str) -> ModuleHealthReporter:
        return ModuleHealthReporter(self, module_name)

    @property
    def worst(self) -> HealthState:
        order: dict[HealthState, int] = {
            "healthy": 0,
            "starting": 1,
            "degraded": 2,
            "failed": 3,
        }
        worst: HealthState = "healthy"
        for health in self._states.values():
            if order[health.state] > order[worst]:
                worst = health.state
        return worst


@dataclass(frozen=True, slots=True)
class ModuleHealthReporter:
    """The ``HealthReporter`` port handed to one module."""

    registry: HealthRegistry
    module_name: str

    def report(
        self,
        state: HealthState,
        detail: str = "",
        metrics: Mapping[str, float] | None = None,
    ) -> None:
        self.registry.set(self.module_name, state, detail, metrics)


__all__ = ["HealthRegistry", "ModuleHealthReporter"]
