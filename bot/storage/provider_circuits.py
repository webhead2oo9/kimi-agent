from __future__ import annotations

from providers.circuit_breaker import CircuitRecord
from providers.failure_policy import CircuitScopeKind
from storage.db import Database


class ProviderCircuitStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def load(self) -> list[CircuitRecord]:
        async with self._db.conn.execute(
            "SELECT scope_key, scope_kind, display_label, reason, status_code, "
            "provider_code, opened_at, retry_at, updated_at FROM provider_circuits"
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            CircuitRecord(
                scope_key=str(row["scope_key"]),
                scope_kind=CircuitScopeKind(str(row["scope_kind"])),
                display_label=str(row["display_label"]),
                reason=str(row["reason"]),
                status_code=(int(row["status_code"]) if row["status_code"] is not None else None),
                provider_code=(
                    str(row["provider_code"]) if row["provider_code"] is not None else None
                ),
                opened_at=float(row["opened_at"]),
                retry_at=float(row["retry_at"]),
                updated_at=float(row["updated_at"]),
            )
            for row in rows
        ]

    async def upsert(self, record: CircuitRecord) -> None:
        async with self._db.write_transaction() as conn:
            await conn.execute(
                """INSERT INTO provider_circuits (
                    scope_key, scope_kind, display_label, reason, status_code,
                    provider_code, opened_at, retry_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(scope_key) DO UPDATE SET
                    scope_kind = excluded.scope_kind,
                    display_label = excluded.display_label,
                    reason = excluded.reason,
                    status_code = excluded.status_code,
                    provider_code = excluded.provider_code,
                    opened_at = excluded.opened_at,
                    retry_at = excluded.retry_at,
                    updated_at = excluded.updated_at""",
                (
                    record.scope_key,
                    record.scope_kind.value,
                    record.display_label,
                    record.reason,
                    record.status_code,
                    record.provider_code,
                    record.opened_at,
                    record.retry_at,
                    record.updated_at,
                ),
            )

    async def delete(self, scope_key: str) -> None:
        async with self._db.write_transaction() as conn:
            await conn.execute("DELETE FROM provider_circuits WHERE scope_key = ?", (scope_key,))

    async def reset_all(self) -> None:
        async with self._db.write_transaction() as conn:
            await conn.execute("DELETE FROM provider_circuits")
