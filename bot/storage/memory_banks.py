"""Durable local state for per-user Hindsight bank deletion safety."""

from __future__ import annotations

import time

from storage.db import Database


class UserMemoryBankStateStore:
    """Track whether a user's remote bank may still exist.

    ``may_exist`` is deliberately conservative: callers set it before attempting
    a remote create/retain, and clear it only after a confirmed delete. A failed
    or interrupted remote mutation therefore leaves privacy deletion requiring
    the backend instead of falsely claiming there is nothing to wipe.
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    async def mark_may_exist(self, user_id: str, *, now: float | None = None) -> None:
        timestamp = time.time() if now is None else float(now)
        async with self._db.write_transaction() as conn:
            await conn.execute(
                """
                INSERT INTO user_memory_bank_states (user_id, may_exist, updated_at)
                VALUES (?, 1, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    may_exist = 1,
                    updated_at = excluded.updated_at
                """,
                (str(user_id), timestamp),
            )

    async def mark_absent(self, user_id: str, *, now: float | None = None) -> None:
        timestamp = time.time() if now is None else float(now)
        async with self._db.write_transaction() as conn:
            await conn.execute(
                """
                INSERT INTO user_memory_bank_states (user_id, may_exist, updated_at)
                VALUES (?, 0, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    may_exist = 0,
                    updated_at = excluded.updated_at
                """,
                (str(user_id), timestamp),
            )

    async def may_exist(self, user_id: str) -> bool:
        async with self._db.conn.execute(
            "SELECT may_exist FROM user_memory_bank_states WHERE user_id = ?",
            (str(user_id),),
        ) as cursor:
            row = await cursor.fetchone()
        return bool(row["may_exist"]) if row is not None else False
