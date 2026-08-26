"""Versioned services one module provides and another consumes.

The registry is shared across modules; each module sees it through a
``ModuleServiceView`` that enforces the module's ``provides`` and ``consumes``
declarations. Consumers receive a proxy, so a provider that closed makes every
later call raise ``ServiceUnavailable`` instead of touching a dead object.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from kimi_agent_module_api.contracts import (
    ModuleContractError,
    ServiceDeclaration,
    ServiceRequirement,
    ServiceUnavailable,
)

log = logging.getLogger(__name__)


@dataclass(slots=True)
class _Provided:
    provider: str
    implementation: object
    alive: bool = True


class ServiceProxy:
    """Attribute access forwards to the implementation while it is alive."""

    __slots__ = ("_name", "_provided", "_version")

    def __init__(self, provided: _Provided, name: str, version: int) -> None:
        self._provided = provided
        self._name = name
        self._version = version

    def __getattr__(self, attribute: str) -> Any:
        if not self._provided.alive:
            raise ServiceUnavailable(
                f"service {self._name}@{self._version} closed with module "
                f"{self._provided.provider!r}"
            )
        return getattr(self._provided.implementation, attribute)

    def __repr__(self) -> str:
        state = "alive" if self._provided.alive else "closed"
        return f"<ServiceProxy {self._name}@{self._version} from {self._provided.provider} {state}>"


@dataclass(slots=True)
class _Registration:
    registry: ServiceRegistryImpl
    key: tuple[str, int]
    closed: bool = False

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.registry._retire(self.key)


@dataclass(slots=True)
class ServiceRegistryImpl:
    _provided: dict[tuple[str, int], _Provided] = field(default_factory=dict)

    def provide(
        self, provider: str, name: str, version: int, implementation: object
    ) -> _Registration:
        key = (name, version)
        existing = self._provided.get(key)
        if existing is not None and existing.alive:
            raise ModuleContractError(
                f"service {name}@{version} is already provided by {existing.provider!r}"
            )
        self._provided[key] = _Provided(provider, implementation)
        log.info("Kimi module %s provides service %s@%d", provider, name, version)
        return _Registration(self, key)

    def get(self, name: str, version: int) -> ServiceProxy:
        provided = self._provided.get((name, version))
        if provided is None or not provided.alive:
            raise ServiceUnavailable(f"service {name}@{version} is not provided")
        return ServiceProxy(provided, name, version)

    def _retire(self, key: tuple[str, int]) -> None:
        provided = self._provided.get(key)
        if provided is not None:
            provided.alive = False

    def retire_module(self, provider: str) -> None:
        for provided in self._provided.values():
            if provided.provider == provider:
                provided.alive = False

    def provided_by(self, provider: str) -> tuple[tuple[str, int], ...]:
        return tuple(
            key
            for key, provided in self._provided.items()
            if provided.provider == provider and provided.alive
        )


@dataclass(frozen=True, slots=True)
class ModuleServiceView:
    """The ``ServiceRegistry`` port handed to one module."""

    registry: ServiceRegistryImpl
    module_name: str
    provides: tuple[ServiceDeclaration, ...]
    consumes: tuple[ServiceRequirement, ...]

    def provide(self, name: str, version: int, implementation: object) -> _Registration:
        if not any(d.name == name and d.version == version for d in self.provides):
            raise ModuleContractError(
                f"module {self.module_name!r} did not declare that it provides {name}@{version}"
            )
        return self.registry.provide(self.module_name, name, version, implementation)

    def get(self, name: str, version: int) -> object:
        if not any(r.name == name and r.version == version for r in self.consumes):
            raise ModuleContractError(
                f"module {self.module_name!r} did not declare that it consumes {name}@{version}"
            )
        return self.registry.get(name, version)


def undeclared_provisions(
    module_name: str,
    provides: Iterable[ServiceDeclaration],
    actually: Callable[[str], tuple[tuple[str, int], ...]],
) -> tuple[str, ...]:
    """Declared services a module never provided during ``start``."""
    live = set(actually(module_name))
    return tuple(f"{d.name}@{d.version}" for d in provides if (d.name, d.version) not in live)


__all__ = ["ModuleServiceView", "ServiceProxy", "ServiceRegistryImpl", "undeclared_provisions"]
