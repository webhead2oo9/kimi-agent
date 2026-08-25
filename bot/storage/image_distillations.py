from __future__ import annotations

import time

from storage.db import Database


class ImageDistillationStore:
    """Conversation-scoped cache of model-produced visual descriptions."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def get(self, conversation_id: int, cache_key: str) -> tuple[str, str] | None:
        async with self._db.conn.execute(
            "SELECT description, model_name FROM image_distillations "
            "WHERE conversation_id = ? AND cache_key = ?",
            (conversation_id, cache_key),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return str(row["description"]), str(row["model_name"])

    async def set(
        self,
        conversation_id: int,
        cache_key: str,
        *,
        model_name: str,
        prompt_version: int,
        description: str,
    ) -> None:
        async with self._db.write_transaction() as conn:
            await conn.execute(
                "INSERT INTO image_distillations "
                "(conversation_id, cache_key, model_name, prompt_version, "
                "description, created_at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(conversation_id, cache_key) DO UPDATE SET "
                "model_name = excluded.model_name, "
                "prompt_version = excluded.prompt_version, "
                "description = excluded.description, "
                "created_at = excluded.created_at",
                (
                    conversation_id,
                    cache_key,
                    model_name,
                    prompt_version,
                    description,
                    time.time(),
                ),
            )
