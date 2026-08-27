from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
import logging
import time
from typing import Protocol

from providers.failure_policy import CircuitScopeKind, ProviderFailure

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CircuitTarget:
    model_scope_key: str
    account_scope_key: str
    display_label: str

    @classmethod
    def create(cls, *, model_identity: str, account_identity: str, label: str) -> CircuitTarget:
        return cls(
            model_scope_key=_scope_key("model", model_identity),
            account_scope_key=_scope_key("account", account_identity),
            display_label=label,
        )

    def key_for(self, kind: CircuitScopeKind) -> str:
        return self.model_scope_key if kind is CircuitScopeKind.MODEL else self.account_scope_key


@dataclass(frozen=True, slots=True)
class CircuitRecord:
    scope_key: str
    scope_kind: CircuitScopeKind
    display_label: str
    reason: str
    status_code: int | None
    provider_code: str | None
    opened_at: float
    retry_at: float
    updated_at: float


@dataclass(frozen=True, slots=True)
class CircuitPermit:
    generation: int
    probe_keys: tuple[str, ...] = ()


class CircuitStore(Protocol):
    async def load(self) -> list[CircuitRecord]: ...

    async def upsert(self, record: CircuitRecord) -> None: ...

    async def delete(self, scope_key: str) -> None: ...

    async def reset_all(self) -> None: ...


def _scope_key(kind: str, identity: str) -> str:
    return sha256(f"{kind}\0{identity}".encode()).hexdigest()


class ProviderCircuitBreaker:
    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._records: dict[str, CircuitRecord] = {}
        self._probes: set[str] = set()
        self._generation = 0
        self._store: CircuitStore | None = None
        self._lock = asyncio.Lock()
        self._persistence_lock = asyncio.Lock()

    def now(self) -> float:
        return self._clock()

    async def initialize(self, store: CircuitStore, valid_scope_keys: set[str]) -> None:
        records = await store.load()
        stale = [record.scope_key for record in records if record.scope_key not in valid_scope_keys]
        for scope_key in stale:
            await store.delete(scope_key)
        async with self._lock:
            self._store = store
            self._records = {
                record.scope_key: record
                for record in records
                if record.scope_key in valid_scope_keys
            }
            self._probes.clear()

    async def allow(self, target: CircuitTarget) -> CircuitPermit | None:
        async with self._lock:
            now = self._clock()
            probe_keys: list[str] = []
            for key in (target.account_scope_key, target.model_scope_key):
                record = self._records.get(key)
                if record is None:
                    continue
                if record.retry_at > now or key in self._probes:
                    return None
                probe_keys.append(key)
            self._probes.update(probe_keys)
            return CircuitPermit(self._generation, tuple(probe_keys))

    async def record_success(self, permit: CircuitPermit) -> None:
        async with self._lock:
            if permit.generation != self._generation:
                return
            keys = permit.probe_keys
            for key in keys:
                self._probes.discard(key)
                self._records.pop(key, None)
        for key in keys:
            await self._delete_safely(key, generation=permit.generation)
            log.info("provider circuit closed: scope=%s", key[:12])

    async def record_failure(
        self,
        target: CircuitTarget,
        failure: ProviderFailure,
        permit: CircuitPermit,
    ) -> None:
        if failure.scope is None or failure.retry_at is None:
            await self.release(permit)
            return
        key = target.key_for(failure.scope)
        now = self._clock()
        record = CircuitRecord(
            scope_key=key,
            scope_kind=failure.scope,
            display_label=target.display_label,
            reason=failure.category.value,
            status_code=failure.status_code,
            provider_code=failure.provider_code,
            opened_at=now,
            retry_at=failure.retry_at,
            updated_at=now,
        )
        async with self._lock:
            if permit.generation != self._generation:
                return
            for probe_key in permit.probe_keys:
                self._probes.discard(probe_key)
            self._records[key] = record
        await self._upsert_safely(record, generation=permit.generation)
        log.warning(
            "provider circuit opened: provider=%s reason=%s retry_at=%s",
            target.display_label,
            record.reason,
            record.retry_at,
        )

    async def release(self, permit: CircuitPermit) -> None:
        async with self._lock:
            if permit.generation == self._generation:
                self._probes.difference_update(permit.probe_keys)

    async def snapshots(self) -> tuple[CircuitRecord, ...]:
        async with self._lock:
            return tuple(sorted(self._records.values(), key=lambda record: record.retry_at))

    async def reset_all(self) -> None:
        async with self._persistence_lock:
            store = self._store
            if store is not None:
                await store.reset_all()
            async with self._lock:
                self._generation += 1
                self._records.clear()
                self._probes.clear()
        log.info("all provider circuits reset")

    async def _upsert_safely(self, record: CircuitRecord, *, generation: int) -> None:
        if self._store is None:
            return
        async with self._persistence_lock:
            async with self._lock:
                if generation != self._generation:
                    return
            try:
                await self._store.upsert(record)
            except Exception:
                log.exception("could not persist provider circuit state")

    async def _delete_safely(self, scope_key: str, *, generation: int) -> None:
        if self._store is None:
            return
        async with self._persistence_lock:
            async with self._lock:
                if generation != self._generation:
                    return
            try:
                await self._store.delete(scope_key)
            except Exception:
                log.exception("could not delete provider circuit state")
