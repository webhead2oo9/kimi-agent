"""Durable authorization queue for user-requested privacy deletion."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Literal, cast

from storage.db import Database

PrivacyDeletionScope = Literal["memory", "all"]


@dataclass(frozen=True)
class PrivacyDeletionRequest:
    user_id: str
    scope: PrivacyDeletionScope
    generation: int
    request_token: str
    memory_backend_required: bool
    requested_at: float
    updated_at: float


class PrivacyDeletionRequestStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def request(
        self,
        *,
        user_id: str,
        scope: PrivacyDeletionScope,
        memory_backend_required: bool,
        now: float | None = None,
    ) -> PrivacyDeletionRequest:
        """Persist authorization and return the effective coalesced request.

        Repeated requests increment ``generation``. Full deletion dominates a
        memory-only request, and a request that was authorized while Hindsight
        was configured keeps requiring a confirmed backend delete on retries.
        """

        user_id = str(user_id).strip()
        if not user_id:
            raise ValueError("Privacy deletion user_id is required.")
        if scope not in {"memory", "all"}:
            raise ValueError("Privacy deletion scope must be memory or all.")
        timestamp = time.time() if now is None else float(now)
        request_token = uuid.uuid4().hex

        async with self._db.write_transaction() as conn:
            await conn.execute(
                """
                INSERT INTO privacy_deletion_requests (
                    user_id, scope, generation, request_token,
                    memory_backend_required,
                    requested_at, updated_at
                ) VALUES (?, ?, 1, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    scope = CASE
                        WHEN privacy_deletion_requests.scope = 'all'
                          OR excluded.scope = 'all'
                        THEN 'all'
                        ELSE 'memory'
                    END,
                    generation = privacy_deletion_requests.generation + 1,
                    request_token = excluded.request_token,
                    memory_backend_required = CASE
                        WHEN privacy_deletion_requests.memory_backend_required != 0
                          OR excluded.memory_backend_required != 0
                        THEN 1
                        ELSE 0
                    END,
                    updated_at = excluded.updated_at
                """,
                (
                    user_id,
                    scope,
                    request_token,
                    int(memory_backend_required),
                    timestamp,
                    timestamp,
                ),
            )
            async with conn.execute(
                """
                SELECT user_id, scope, generation, request_token,
                       memory_backend_required,
                       requested_at, updated_at
                FROM privacy_deletion_requests
                WHERE user_id = ?
                """,
                (user_id,),
            ) as cursor:
                row = await cursor.fetchone()
        if row is None:  # pragma: no cover - same-transaction invariant
            raise RuntimeError("Privacy deletion request was not persisted.")
        return _request_from_row(row)

    async def list_pending(self) -> list[PrivacyDeletionRequest]:
        async with self._db.conn.execute(
            """
            SELECT user_id, scope, generation, request_token,
                   memory_backend_required,
                   requested_at, updated_at
            FROM privacy_deletion_requests
            ORDER BY requested_at, user_id
            """
        ) as cursor:
            rows = await cursor.fetchall()
        return [_request_from_row(row) for row in rows]

    async def complete(self, request: PrivacyDeletionRequest) -> bool:
        """Remove exactly the uniquely authorized request this worker processed."""

        async with self._db.write_transaction() as conn:
            cursor = await conn.execute(
                """
                DELETE FROM privacy_deletion_requests
                WHERE user_id = ? AND request_token = ?
                """,
                (request.user_id, request.request_token),
            )
            return cursor.rowcount > 0


def _request_from_row(row: object) -> PrivacyDeletionRequest:
    # Typed `object` so this module does not depend on the aiosqlite Row type;
    # the driver's row still supports dict() at runtime.
    data = dict(row)  # type: ignore[call-overload]
    return PrivacyDeletionRequest(
        user_id=str(data["user_id"]),
        scope=cast(PrivacyDeletionScope, data["scope"]),
        generation=int(data["generation"]),
        request_token=str(data["request_token"]),
        memory_backend_required=bool(data["memory_backend_required"]),
        requested_at=float(data["requested_at"]),
        updated_at=float(data["updated_at"]),
    )
