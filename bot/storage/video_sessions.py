from __future__ import annotations

from dataclasses import dataclass
from sqlite3 import Row
import time

from storage.db import Database

_DELETION_RETRY_BASE_SECONDS = 60.0
_DELETION_RETRY_MAX_SECONDS = 21_600.0
_DELETION_RETRY_MAX_EXPONENT = 9


@dataclass(frozen=True, slots=True)
class VideoSession:
    handle: str
    conversation_id: int
    actor_user_id: str
    guild_id: str
    youtube_url: str
    youtube_video_id: str
    model: str
    latest_interaction_id: str
    interaction_count: int
    created_at: float
    last_active_at: float
    expires_at: float


@dataclass(frozen=True, slots=True)
class VideoInteractionDeletion:
    interaction_id: str
    actor_user_id: str
    attempts: int
    retry_at: float


class VideoSessionStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def create_session(
        self,
        *,
        handle: str,
        conversation_id: int,
        actor_user_id: str,
        guild_id: str,
        youtube_url: str,
        youtube_video_id: str,
        model: str,
        interaction_id: str,
        now: float,
        expires_at: float,
    ) -> None:
        async with self._db.write_transaction() as conn:
            await conn.execute(
                """
                INSERT INTO video_sessions (
                    handle, conversation_id, actor_user_id, guild_id,
                    youtube_url, youtube_video_id, model, latest_interaction_id,
                    interaction_count, created_at, last_active_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                """,
                (
                    handle,
                    conversation_id,
                    actor_user_id,
                    guild_id,
                    youtube_url,
                    youtube_video_id,
                    model,
                    interaction_id,
                    now,
                    now,
                    expires_at,
                ),
            )
            await conn.execute(
                """
                INSERT INTO video_interactions (
                    interaction_id, session_handle, actor_user_id, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (interaction_id, handle, actor_user_id, now),
            )

    async def find_sessions(
        self,
        *,
        conversation_id: int,
        actor_user_id: str,
        guild_id: str,
        now: float,
        handle: str | None,
    ) -> tuple[VideoSession, ...]:
        params: list[object] = [conversation_id, actor_user_id, guild_id, now]
        handle_clause = ""
        if handle is not None:
            handle_clause = " AND handle = ?"
            params.append(handle)
        query = (
            "SELECT * FROM video_sessions "
            "WHERE conversation_id = ? AND actor_user_id = ? AND guild_id = ? "
            "AND expires_at > ?"
            f"{handle_clause} ORDER BY last_active_at DESC LIMIT 2"
        )
        async with self._db.conn.execute(query, params) as cur:
            rows = await cur.fetchall()
        return tuple(_session_from_row(row) for row in rows)

    async def advance_session(
        self,
        *,
        handle: str,
        actor_user_id: str,
        expected_interaction_id: str,
        interaction_id: str,
        now: float,
        expires_at: float,
        max_interactions: int,
    ) -> bool:
        async with self._db.write_transaction() as conn:
            cursor = await conn.execute(
                """
                UPDATE video_sessions
                SET latest_interaction_id = ?, interaction_count = interaction_count + 1,
                    last_active_at = ?, expires_at = ?
                WHERE handle = ? AND actor_user_id = ? AND latest_interaction_id = ?
                  AND interaction_count < ? AND expires_at > ?
                """,
                (
                    interaction_id,
                    now,
                    expires_at,
                    handle,
                    actor_user_id,
                    expected_interaction_id,
                    max_interactions,
                    now,
                ),
            )
            if cursor.rowcount != 1:
                return False
            await conn.execute(
                """
                INSERT INTO video_interactions (
                    interaction_id, session_handle, actor_user_id, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (interaction_id, handle, actor_user_id, now),
            )
        return True

    async def delete_user_sessions(self, user_id: str) -> int:
        async with self._db.write_transaction() as conn:
            cursor = await conn.execute(
                "DELETE FROM video_sessions WHERE actor_user_id = ?",
                (user_id,),
            )
            return max(0, int(cursor.rowcount))

    async def enqueue_deletion(
        self,
        *,
        interaction_id: str,
        actor_user_id: str,
        now: float,
    ) -> None:
        async with self._db.write_transaction() as conn:
            await conn.execute(
                """
                INSERT INTO video_interaction_deletions (
                    interaction_id, actor_user_id, queued_at, updated_at,
                    attempts, last_error
                ) VALUES (?, ?, ?, ?, 0, '')
                ON CONFLICT(interaction_id) DO NOTHING
                """,
                (interaction_id, actor_user_id, now, now),
            )

    async def delete_expired(self, now: float, *, limit: int) -> int:
        if limit <= 0:
            return 0
        async with self._db.write_transaction() as conn:
            cursor = await conn.execute(
                """
                DELETE FROM video_sessions
                WHERE handle IN (
                    SELECT handle FROM video_sessions
                    WHERE expires_at <= ?
                    ORDER BY expires_at
                    LIMIT ?
                )
                """,
                (now, limit),
            )
            return max(0, int(cursor.rowcount))

    async def pending_deletions(
        self,
        *,
        user_id: str | None,
        limit: int,
    ) -> tuple[VideoInteractionDeletion, ...]:
        retry_expression = (
            "CASE WHEN attempts <= 0 THEN queued_at "
            "WHEN attempts >= ? THEN updated_at + ? "
            "ELSE updated_at + (? * (1 << (attempts - 1))) END"
        )
        if user_id is None:
            query = (
                "SELECT interaction_id, actor_user_id, attempts, "
                f"{retry_expression} AS retry_at "
                "FROM video_interaction_deletions "
                "ORDER BY retry_at, queued_at LIMIT ?"
            )
            params: tuple[object, ...] = (
                _DELETION_RETRY_MAX_EXPONENT,
                _DELETION_RETRY_MAX_SECONDS,
                _DELETION_RETRY_BASE_SECONDS,
                limit,
            )
        else:
            query = (
                "SELECT interaction_id, actor_user_id, attempts, "
                f"{retry_expression} AS retry_at "
                "FROM video_interaction_deletions WHERE actor_user_id = ? "
                "ORDER BY retry_at, queued_at LIMIT ?"
            )
            params = (
                _DELETION_RETRY_MAX_EXPONENT,
                _DELETION_RETRY_MAX_SECONDS,
                _DELETION_RETRY_BASE_SECONDS,
                user_id,
                limit,
            )
        async with self._db.conn.execute(query, params) as cur:
            rows = await cur.fetchall()
        return tuple(
            VideoInteractionDeletion(
                interaction_id=str(row["interaction_id"]),
                actor_user_id=str(row["actor_user_id"]),
                attempts=int(row["attempts"]),
                retry_at=float(row["retry_at"]),
            )
            for row in rows
        )

    async def complete_deletion(self, interaction_id: str) -> None:
        async with self._db.write_transaction() as conn:
            await conn.execute(
                "DELETE FROM video_interaction_deletions WHERE interaction_id = ?",
                (interaction_id,),
            )

    async def fail_deletion(
        self,
        interaction_id: str,
        error: str,
        *,
        now: float | None = None,
    ) -> None:
        async with self._db.write_transaction() as conn:
            await conn.execute(
                """
                UPDATE video_interaction_deletions
                SET attempts = attempts + 1, updated_at = ?, last_error = ?
                WHERE interaction_id = ?
                """,
                (time.time() if now is None else now, error[:500], interaction_id),
            )


def _session_from_row(row: Row) -> VideoSession:
    return VideoSession(
        handle=str(row["handle"]),
        conversation_id=int(row["conversation_id"]),
        actor_user_id=str(row["actor_user_id"]),
        guild_id=str(row["guild_id"]),
        youtube_url=str(row["youtube_url"]),
        youtube_video_id=str(row["youtube_video_id"]),
        model=str(row["model"]),
        latest_interaction_id=str(row["latest_interaction_id"]),
        interaction_count=int(row["interaction_count"]),
        created_at=float(row["created_at"]),
        last_active_at=float(row["last_active_at"]),
        expires_at=float(row["expires_at"]),
    )
