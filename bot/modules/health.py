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
    _module_states: dict[str, ModuleHealth] = field(default_factory=dict)
    _constraints: dict[tuple[str, str], ModuleHealth] = field(default_factory=dict)
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
        health = self._build(state, detail, metrics)
        self._module_states[module_name] = health
        return self._refresh(module_name)

    def set_constraint(
        self,
        module_name: str,
        source: str,
        state: HealthState,
        detail: str = "",
        metrics: Mapping[str, float] | None = None,
    ) -> ModuleHealth:
        """Set one core-owned health constraint without erasing module reports."""
        key = (module_name, source)
        if state == "healthy" and not detail and not metrics:
            self._constraints.pop(key, None)
        else:
            self._constraints[key] = self._build(state, detail, metrics)
        return self._refresh(module_name)

    def _build(
        self,
        state: HealthState,
        detail: str,
        metrics: Mapping[str, float] | None,
    ) -> ModuleHealth:
        return ModuleHealth(
            state=state,
            detail=detail[:HEALTH_DETAIL_MAX_LENGTH],
            metrics=_bounded_metrics(metrics),
            updated_at=self.clock(),
        )

    def _refresh(self, module_name: str) -> ModuleHealth:
        candidates = [
            health for (name, _source), health in self._constraints.items() if name == module_name
        ]
        module_health = self._module_states.get(module_name)
        if module_health is not None:
            candidates.append(module_health)
        if not candidates:
            self._states.pop(module_name, None)
            return ModuleHealth(state="healthy", updated_at=self.clock())

        order: dict[HealthState, int] = {
            "healthy": 0,
            "starting": 1,
            "degraded": 2,
            "failed": 3,
        }
        worst_level = max(order[health.state] for health in candidates)
        worst = [health for health in candidates if order[health.state] == worst_level]
        details = list(dict.fromkeys(health.detail for health in worst if health.detail))
        metrics: dict[str, float] = {}
        for health in candidates:
            metrics.update(health.metrics)
        health = ModuleHealth(
            state=worst[0].state,
            detail="; ".join(details)[:HEALTH_DETAIL_MAX_LENGTH],
            metrics=_bounded_metrics(metrics),
            updated_at=self.clock(),
        )
        previous = self._states.get(module_name)
        self._states[module_name] = health
        if previous is None or previous.state != health.state:
            log.info(
                "Kimi module %s is %s%s",
                module_name,
                health.state,
                f": {health.detail}" if health.detail else "",
            )
        if self.on_change is not None:
            try:
                self.on_change(module_name, health)
            except Exception:
                log.exception("Module health observer failed for %s", module_name)
        return health

    def mark(
        self,
        module_name: str,
        state: HealthState,
        detail: str = "",
        *,
        source: str = "core",
    ) -> None:
        """Set a core constraint for callers that need a ``None`` return."""
        self.set_constraint(module_name, source, state, detail)

    def merge_metrics(self, module_name: str, metrics: Mapping[str, float]) -> None:
        """Update metrics without changing state or detail (e.g. event counters)."""
        current = self._module_states.get(module_name)
        state = current.state if current is not None else "healthy"
        detail = current.detail if current is not None else ""
        merged = dict(current.metrics) if current is not None else {}
        merged.update(metrics)
        self.set(module_name, state, detail, merged)

    def forget(self, module_name: str) -> None:
        self._module_states.pop(module_name, None)
        self._constraints = {
            key: health for key, health in self._constraints.items() if key[0] != module_name
        }
        self._states.pop(module_name, None)

    def module_state(self, module_name: str) -> ModuleHealth | None:
        """Return only the state reported by lifecycle/module code."""
        return self._module_states.get(module_name)

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
