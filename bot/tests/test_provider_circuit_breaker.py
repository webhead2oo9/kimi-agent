from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from providers.circuit_breaker import CircuitRecord, CircuitTarget, ProviderCircuitBreaker
from providers.failure_policy import CircuitScopeKind, FailureCategory, ProviderFailure


@dataclass
class _Store:
    rows: dict[str, CircuitRecord] = field(default_factory=dict)

    async def load(self) -> list[CircuitRecord]:
        return list(self.rows.values())

    async def upsert(self, record: CircuitRecord) -> None:
        self.rows[record.scope_key] = record

    async def delete(self, scope_key: str) -> None:
        self.rows.pop(scope_key, None)

    async def reset_all(self) -> None:
        self.rows.clear()


@pytest.mark.asyncio
async def test_open_skip_half_open_and_close() -> None:
    now = [1000.0]
    target = CircuitTarget.create(model_identity="p/m", account_identity="p/a", label="p/m")
    store = _Store()
    breaker = ProviderCircuitBreaker(clock=lambda: now[0])
    await breaker.initialize(store, {target.model_scope_key, target.account_scope_key})

    permit = await breaker.allow(target)
    assert permit is not None
    failure = ProviderFailure(
        "retry",
        FailureCategory.OUTAGE,
        CircuitScopeKind.MODEL,
        retry_at=1030,
    )
    await breaker.record_failure(target, failure, permit)
    assert await breaker.allow(target) is None

    now[0] = 1030
    probe = await breaker.allow(target)
    assert probe is not None
    assert await breaker.allow(target) is None
    await breaker.record_success(probe)

    assert await breaker.snapshots() == ()
    assert await breaker.allow(target) is not None


@pytest.mark.asyncio
async def test_reset_invalidates_an_old_permit() -> None:
    target = CircuitTarget.create(model_identity="p/m", account_identity="p/a", label="p/m")
    store = _Store()
    breaker = ProviderCircuitBreaker(clock=lambda: 1000)
    await breaker.initialize(store, {target.model_scope_key, target.account_scope_key})
    permit = await breaker.allow(target)
    assert permit is not None

    await breaker.reset_all()
    await breaker.record_failure(
        target,
        ProviderFailure(
            "retry",
            FailureCategory.OUTAGE,
            CircuitScopeKind.MODEL,
            retry_at=2000,
        ),
        permit,
    )

    assert await breaker.snapshots() == ()
    assert store.rows == {}


@pytest.mark.asyncio
async def test_failure_keeps_unrelated_expired_scope_open() -> None:
    target = CircuitTarget.create(model_identity="p/m", account_identity="p/a", label="p/m")
    account = CircuitRecord(
        target.account_scope_key,
        CircuitScopeKind.ACCOUNT,
        "p/m",
        "quota",
        429,
        None,
        100,
        900,
        100,
    )
    model = CircuitRecord(
        target.model_scope_key,
        CircuitScopeKind.MODEL,
        "p/m",
        "outage",
        503,
        None,
        100,
        900,
        100,
    )
    store = _Store({account.scope_key: account, model.scope_key: model})
    breaker = ProviderCircuitBreaker(clock=lambda: 1000)
    await breaker.initialize(store, {target.model_scope_key, target.account_scope_key})

    permit = await breaker.allow(target)
    assert permit is not None
    await breaker.record_failure(
        target,
        ProviderFailure(
            "failover",
            FailureCategory.MODEL_UNAVAILABLE,
            CircuitScopeKind.MODEL,
            retry_at=2000,
        ),
        permit,
    )

    records = {record.scope_key: record for record in await breaker.snapshots()}
    assert records[target.account_scope_key] == account
    assert records[target.model_scope_key].retry_at == 2000
