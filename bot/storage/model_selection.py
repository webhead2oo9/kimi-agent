from __future__ import annotations

import time

from storage.db import Database


class ModelSelectionStore:
    """Durable singleton holding the operator's global chat-model override."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def get(self) -> str | None:
        async with self._db.conn.execute(
            "SELECT model_name FROM model_selection WHERE singleton = 1"
        ) as cur:
            row = await cur.fetchone()
        if row is None or row["model_name"] is None:
            return None
        return str(row["model_name"])

    async def set(self, model_name: str | None) -> None:
        async with self._db.write_transaction() as conn:
            await conn.execute(
                "INSERT INTO model_selection (singleton, model_name, updated_at) "
                "VALUES (1, ?, ?) "
                "ON CONFLICT(singleton) DO UPDATE SET "
                "model_name = excluded.model_name, updated_at = excluded.updated_at",
                (model_name, time.time()),
            )
