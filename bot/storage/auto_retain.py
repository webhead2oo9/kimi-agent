from __future__ import annotations

import time
from dataclasses import dataclass

from storage.db import Database


@dataclass(frozen=True)
class PendingSlice:
    """A (conversation, user) pair with user messages beyond the watermark."""

    conversation_id: int
    user_id: str
    last_active_at: float
    watermark: int | None


@dataclass(frozen=True)
class SliceRow:
    id: int
    role: str
    user_id: str | None
    user_name: str | None
    content: str
    source_created_at: float | None


@dataclass(frozen=True)
class ConversationMeta:
    guild_id: str | None
    channel_id: str | None
    channel_name: str
    key: str = ""


class AutoRetainStore:
    """SQL surface for auto-retain: pending work, transcript slices, watermarks.

    Only real Discord messages (``discord_message_id IS NOT NULL``) count, which
    structurally excludes synthetic/non-Discord rows.
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    async def pending(self, idle_cutoff: float) -> list[PendingSlice]:
        """(conversation, user) pairs in idle conversations with unflushed user rows.

        Only numeric (real Discord snowflake) message ids qualify, so synthetic
        rows can never be flushed as user memories.
        """
        conn = self._db.conn
        async with conn.execute(
            """
            SELECT m.conversation_id AS conversation_id,
                   m.user_id AS user_id,
                   c.last_active_at AS last_active_at,
                   w.last_retained_message_id AS watermark
            FROM messages m
            JOIN conversations c ON c.id = m.conversation_id
            LEFT JOIN auto_retain_watermarks w
                ON w.conversation_id = m.conversation_id AND w.user_id = m.user_id
            WHERE m.role = 'user'
              AND m.user_id IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1
                  FROM privacy_deletion_requests p
                  WHERE p.user_id = m.user_id
              )
              AND m.discord_message_id IS NOT NULL
              AND m.discord_message_id NOT GLOB '*[^0-9]*'
              AND c.last_active_at <= ?
              AND m.id > COALESCE(w.last_retained_message_id, 0)
            GROUP BY m.conversation_id, m.user_id
            ORDER BY c.last_active_at ASC
            """,
            (idle_cutoff,),
        ) as cur:
            rows = await cur.fetchall()
        return [
            PendingSlice(
                conversation_id=int(row["conversation_id"]),
                user_id=str(row["user_id"]),
                last_active_at=float(row["last_active_at"]),
                watermark=(int(row["watermark"]) if row["watermark"] is not None else None),
            )
            for row in rows
        ]

    async def has_pending_privacy_deletion(self, user_id: str) -> bool:
        """Whether durable deletion currently forbids a new memory write."""

        async with self._db.conn.execute(
            "SELECT 1 FROM privacy_deletion_requests WHERE user_id = ?",
            (user_id,),
        ) as cur:
            return await cur.fetchone() is not None

    async def conversation_max_message_id(self, conversation_id: int) -> int:
        conn = self._db.conn
        async with conn.execute(
            "SELECT MAX(id) FROM messages WHERE conversation_id = ?",
            (conversation_id,),
        ) as cur:
            row = await cur.fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    async def slice_rows(
        self,
        conversation_id: int,
        after_id: int,
        end_id: int,
    ) -> list[SliceRow]:
        """All real transcript rows in the id range, every participant included.

        Per-user filtering and assistant-reply attribution happen in the flush
        engine, which needs the full interleaving to attribute bot replies to
        the user turn they answered.
        """
        conn = self._db.conn
        async with conn.execute(
            """
            SELECT id, role, user_id, user_name, content, source_created_at
            FROM messages
            WHERE conversation_id = ?
              AND id > ? AND id <= ?
              AND discord_message_id IS NOT NULL
              AND discord_message_id NOT GLOB '*[^0-9]*'
              AND role IN ('user', 'assistant')
            ORDER BY id ASC
            """,
            (conversation_id, after_id, end_id),
        ) as cur:
            rows = await cur.fetchall()
        return [
            SliceRow(
                id=int(row["id"]),
                role=str(row["role"]),
                user_id=row["user_id"],
                user_name=row["user_name"],
                content=row["content"] or "",
                source_created_at=(
                    float(row["source_created_at"])
                    if row["source_created_at"] is not None
                    else None
                ),
            )
            for row in rows
        ]

    async def conversation_meta(self, conversation_id: int) -> ConversationMeta:
        conn = self._db.conn
        async with conn.execute(
            "SELECT key, guild_id, channel_id, channel_name FROM conversations WHERE id = ?",
            (conversation_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return ConversationMeta(guild_id=None, channel_id=None, channel_name="")
        return ConversationMeta(
            guild_id=row["guild_id"],
            channel_id=row["channel_id"],
            channel_name=row["channel_name"] or "",
            key=row["key"] or "",
        )

    async def set_watermark(self, conversation_id: int, user_id: str, message_id: int) -> None:
        now = time.time()
        async with self._db.write_transaction() as conn:
            await conn.execute(
                """
                INSERT INTO auto_retain_watermarks
                    (conversation_id, user_id, last_retained_message_id, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(conversation_id, user_id) DO UPDATE SET
                    last_retained_message_id = excluded.last_retained_message_id,
                    updated_at = excluded.updated_at
                """,
                (conversation_id, user_id, message_id, now),
            )

    async def get_watermark(self, conversation_id: int, user_id: str) -> int | None:
        """Read the current committed watermark for one participant slice."""
        conn = self._db.conn
        async with conn.execute(
            "SELECT last_retained_message_id FROM auto_retain_watermarks "
            "WHERE conversation_id = ? AND user_id = ?",
            (conversation_id, user_id),
        ) as cur:
            row = await cur.fetchone()
        return int(row[0]) if row is not None else None

    async def fast_forward_user(self, user_id: str) -> int:
        """Mark every conversation the user spoke in as fully retained.

        Used on forget-me so historical transcript content is never re-ingested
        into a freshly recreated bank. Returns an approximate count of
        conversations touched.
        """
        now = time.time()
        async with self._db.write_transaction() as conn:
            cur = await conn.execute(
                """
                INSERT INTO auto_retain_watermarks
                    (conversation_id, user_id, last_retained_message_id, updated_at)
                SELECT conversation_id, ?, MAX(id), ?
                FROM messages
                WHERE conversation_id IN (
                    SELECT DISTINCT conversation_id FROM messages
                    WHERE role = 'user' AND user_id = ?
                )
                GROUP BY conversation_id
                ON CONFLICT(conversation_id, user_id) DO UPDATE SET
                    last_retained_message_id = excluded.last_retained_message_id,
                    updated_at = excluded.updated_at
                """,
                (user_id, now, user_id),
            )
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
