from __future__ import annotations

import pytest

from providers.circuit_breaker import CircuitRecord
from providers.failure_policy import CircuitScopeKind
from storage.db import Database
from storage.provider_circuits import ProviderCircuitStore


@pytest.mark.asyncio
async def test_provider_circuit_store_round_trip_and_reset(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    store = ProviderCircuitStore(db)
    record = CircuitRecord(
        scope_key="opaque",
        scope_kind=CircuitScopeKind.ACCOUNT,
        display_label="provider/model",
        reason="quota",
        status_code=429,
        provider_code="limit",
        opened_at=1000,
        retry_at=2000,
        updated_at=1000,
    )
    try:
        await store.upsert(record)
        assert await store.load() == [record]
        await store.reset_all()
        assert await store.load() == []
    finally:
        await db.close()
