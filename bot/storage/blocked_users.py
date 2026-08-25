from __future__ import annotations

import time
from dataclasses import dataclass

from storage.db import Database


@dataclass(frozen=True)
class BlockedUserRecord:
    user_id: str
    blocked_by: str
    reason: str
    created_at: float
    updated_at: float


class BlockedUserStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def is_blocked(self, user_id: str) -> bool:
        return await self.get_block(user_id) is not None

    async def get_block(self, user_id: str) -> BlockedUserRecord | None:
        async with self._db.conn.execute(
            "SELECT user_id, blocked_by, reason, created_at, updated_at "
            "FROM blocked_users WHERE user_id = ?",
            (user_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return BlockedUserRecord(
            user_id=row["user_id"],
            blocked_by=row["blocked_by"],
            reason=row["reason"] or "",
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )

    async def block_user(self, user_id: str, *, blocked_by: str, reason: str = "") -> bool:
        """Insert or refresh a block. Returns True when the block was newly created.

        The insert statement decides creation via SQLite's conflict handling, not
        timestamp equality. If the row already exists, the serialized transaction
        refreshes mutable fields and reports ``False``.
        """
        now = time.time()
        async with self._db.write_transaction() as conn:
            async with conn.execute(
                "INSERT INTO blocked_users (user_id, blocked_by, reason, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(user_id) DO NOTHING "
                "RETURNING 1",
                (user_id, blocked_by, reason.strip(), now, now),
            ) as cur:
                row = await cur.fetchone()
            if row is not None:
                return True

            await conn.execute(
                "UPDATE blocked_users "
                "SET blocked_by = ?, reason = ?, updated_at = ? "
                "WHERE user_id = ?",
                (blocked_by, reason.strip(), now, user_id),
            )
            return False

    async def unblock_user(self, user_id: str) -> bool:
        async with self._db.write_transaction() as conn:
            cur = await conn.execute(
                "DELETE FROM blocked_users WHERE user_id = ?",
                (user_id,),
            )
        return cur.rowcount > 0
