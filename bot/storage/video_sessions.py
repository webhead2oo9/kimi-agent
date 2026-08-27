from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
import re
from sqlite3 import Row

from aiosqlite import Connection
import time

from storage.db import Database

_DELETION_RETRY_BASE_SECONDS = 60.0
_DELETION_RETRY_MAX_SECONDS = 21_600.0
_DELETION_RETRY_MAX_EXPONENT = 9
_FILE_NAME_RE = re.compile(r"^files/[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?$")
_SOURCE_KINDS = frozenset({"attachment", "workspace"})


@dataclass(frozen=True, slots=True)
class VideoSession:
    handle: str
    conversation_id: int
    actor_user_id: str
    guild_id: str
    source_kind: str
    source_display_name: str
    source_locator: str
    source_byte_size: int | None
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
    session_handle: str | None
    attempts: int
    retry_at: float


@dataclass(frozen=True, slots=True)
class VideoFileDeletion:
    file_name: str
    actor_user_id: str
    session_handle: str | None
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
            await _insert_session(
                conn,
                handle=handle,
                conversation_id=conversation_id,
                actor_user_id=actor_user_id,
                guild_id=guild_id,
                source_kind="youtube",
                source_display_name="YouTube video",
                source_locator=youtube_url,
                source_byte_size=None,
                youtube_url=youtube_url,
                youtube_video_id=youtube_video_id,
                model=model,
                interaction_id=interaction_id,
                now=now,
                expires_at=expires_at,
            )

    async def reserve_provider_file(
        self,
        *,
        file_name: str,
        conversation_id: int,
        actor_user_id: str,
        guild_id: str,
        mime_type: str,
        byte_size: int,
        now: float,
    ) -> None:
        _validate_file_name(file_name)
        if byte_size <= 0:
            raise ValueError("video file size must be positive")
        async with self._db.write_transaction() as conn:
            await conn.execute(
                """
                INSERT INTO video_provider_files (
                    file_name, conversation_id, actor_user_id, guild_id,
                    mime_type, byte_size, session_handle, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?)
                """,
                (
                    file_name,
                    conversation_id,
                    actor_user_id,
                    guild_id,
                    mime_type,
                    byte_size,
                    now,
                ),
            )

    async def create_uploaded_session(
        self,
        *,
        handle: str,
        conversation_id: int,
        actor_user_id: str,
        guild_id: str,
        source_kind: str,
        source_display_name: str,
        source_locator: str,
        source_byte_size: int,
        model: str,
        interaction_id: str,
        file_name: str,
        now: float,
        expires_at: float,
    ) -> None:
        display_name, locator = _validate_uploaded_source(
            source_kind,
            source_display_name,
            source_locator,
        )
        _validate_file_name(file_name)
        async with self._db.write_transaction() as conn:
            await _insert_session(
                conn,
                handle=handle,
                conversation_id=conversation_id,
                actor_user_id=actor_user_id,
                guild_id=guild_id,
                source_kind=source_kind,
                source_display_name=display_name,
                source_locator=locator,
                source_byte_size=source_byte_size,
                youtube_url="",
                youtube_video_id="",
                model=model,
                interaction_id=interaction_id,
                now=now,
                expires_at=expires_at,
            )
            cursor = await conn.execute(
                """
                UPDATE video_provider_files
                SET session_handle = ?
                WHERE file_name = ? AND conversation_id = ?
                  AND actor_user_id = ? AND guild_id = ?
                  AND session_handle IS NULL
                """,
                (
                    handle,
                    file_name,
                    conversation_id,
                    actor_user_id,
                    guild_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("uploaded video reservation is missing or already claimed")

    async def release_provider_file(self, file_name: str, actor_user_id: str) -> bool:
        async with self._db.write_transaction() as conn:
            cursor = await conn.execute(
                """
                DELETE FROM video_provider_files
                WHERE file_name = ? AND actor_user_id = ? AND session_handle IS NULL
                """,
                (file_name, actor_user_id),
            )
            return cursor.rowcount == 1

    async def delete_stale_provider_files(self, cutoff: float, *, limit: int) -> int:
        if limit <= 0:
            return 0
        async with self._db.write_transaction() as conn:
            cursor = await conn.execute(
                """
                DELETE FROM video_provider_files
                WHERE file_name IN (
                    SELECT file_name FROM video_provider_files
                    WHERE session_handle IS NULL AND created_at <= ?
                    ORDER BY created_at LIMIT ?
                )
                """,
                (cutoff, limit),
            )
            return max(0, int(cursor.rowcount))

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
            await conn.execute(
                "DELETE FROM video_provider_files WHERE actor_user_id = ?",
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
                    interaction_id, actor_user_id, session_handle, queued_at,
                    updated_at, attempts, last_error
                ) VALUES (?, ?, NULL, ?, ?, 0, '')
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
                    ORDER BY expires_at LIMIT ?
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
        query, params = _pending_query(
            table="video_interaction_deletions",
            id_column="interaction_id",
            user_id=user_id,
            limit=limit,
        )
        async with self._db.conn.execute(query, params) as cur:
            rows = await cur.fetchall()
        return tuple(
            VideoInteractionDeletion(
                interaction_id=str(row["object_id"]),
                actor_user_id=str(row["actor_user_id"]),
                session_handle=(
                    str(row["session_handle"]) if row["session_handle"] is not None else None
                ),
                attempts=int(row["attempts"]),
                retry_at=float(row["retry_at"]),
            )
            for row in rows
        )

    async def pending_file_deletions(
        self,
        *,
        user_id: str | None,
        limit: int,
    ) -> tuple[VideoFileDeletion, ...]:
        retry_expression = _retry_expression()
        user_clause = "" if user_id is None else " AND f.actor_user_id = ?"
        query = (
            "SELECT f.file_name AS object_id, f.actor_user_id, f.session_handle, f.attempts, "
            f"{retry_expression} AS retry_at "
            "FROM video_provider_file_deletions AS f "
            "WHERE NOT EXISTS ("
            "SELECT 1 FROM video_interaction_deletions AS i "
            "WHERE i.session_handle = f.session_handle AND f.session_handle IS NOT NULL"
            ")"
            f"{user_clause} ORDER BY retry_at, f.queued_at LIMIT ?"
        )
        base_params: list[object] = [
            _DELETION_RETRY_MAX_EXPONENT,
            _DELETION_RETRY_MAX_SECONDS,
            _DELETION_RETRY_BASE_SECONDS,
        ]
        if user_id is not None:
            base_params.append(user_id)
        base_params.append(limit)
        async with self._db.conn.execute(query, tuple(base_params)) as cur:
            rows = await cur.fetchall()
        return tuple(
            VideoFileDeletion(
                file_name=str(row["object_id"]),
                actor_user_id=str(row["actor_user_id"]),
                session_handle=(
                    str(row["session_handle"]) if row["session_handle"] is not None else None
                ),
                attempts=int(row["attempts"]),
                retry_at=float(row["retry_at"]),
            )
            for row in rows
        )

    async def complete_deletion(self, interaction_id: str) -> None:
        await self._complete("video_interaction_deletions", "interaction_id", interaction_id)

    async def complete_file_deletion(self, file_name: str) -> None:
        await self._complete("video_provider_file_deletions", "file_name", file_name)

    async def fail_deletion(
        self,
        interaction_id: str,
        error: str,
        *,
        now: float | None = None,
    ) -> None:
        await self._fail(
            "video_interaction_deletions",
            "interaction_id",
            interaction_id,
            error,
            now,
        )

    async def fail_file_deletion(
        self,
        file_name: str,
        error: str,
        *,
        now: float | None = None,
    ) -> None:
        await self._fail(
            "video_provider_file_deletions",
            "file_name",
            file_name,
            error,
            now,
        )

    async def _complete(self, table: str, column: str, value: str) -> None:
        async with self._db.write_transaction() as conn:
            await conn.execute(f"DELETE FROM {table} WHERE {column} = ?", (value,))

    async def _fail(
        self,
        table: str,
        column: str,
        value: str,
        error: str,
        now: float | None,
    ) -> None:
        async with self._db.write_transaction() as conn:
            await conn.execute(
                f"""
                UPDATE {table}
                SET attempts = attempts + 1, updated_at = ?, last_error = ?
                WHERE {column} = ?
                """,
                (time.time() if now is None else now, error[:500], value),
            )


def _validate_file_name(file_name: str) -> None:
    if not _FILE_NAME_RE.fullmatch(file_name):
        raise ValueError("invalid Gemini file resource name")


def _validate_uploaded_source(kind: str, display_name: str, locator: str) -> tuple[str, str]:
    if kind not in _SOURCE_KINDS:
        raise ValueError("invalid uploaded video source kind")
    display = display_name.strip()[:512]
    value = locator.strip()
    if not display or not value or "://" in value or "\\" in value:
        raise ValueError("uploaded video source metadata is unsafe")
    if kind == "workspace":
        path = PurePosixPath(value)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise ValueError("workspace video locator is unsafe")
        value = path.as_posix()
    elif "/" in value:
        raise ValueError("attachment video locator must be a filename")
    return display, value[:1_024]


async def _insert_session(
    conn: Connection,
    *,
    handle: str,
    conversation_id: int,
    actor_user_id: str,
    guild_id: str,
    source_kind: str,
    source_display_name: str,
    source_locator: str,
    source_byte_size: int | None,
    youtube_url: str,
    youtube_video_id: str,
    model: str,
    interaction_id: str,
    now: float,
    expires_at: float,
) -> None:
    await conn.execute(
        """
        INSERT INTO video_sessions (
            handle, conversation_id, actor_user_id, guild_id,
            source_kind, source_display_name, source_locator, source_byte_size,
            youtube_url, youtube_video_id, model, latest_interaction_id,
            interaction_count, created_at, last_active_at, expires_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
        """,
        (
            handle,
            conversation_id,
            actor_user_id,
            guild_id,
            source_kind,
            source_display_name,
            source_locator,
            source_byte_size,
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


def _retry_expression() -> str:
    return (
        "CASE WHEN attempts <= 0 THEN queued_at "
        "WHEN attempts >= ? THEN updated_at + ? "
        "ELSE updated_at + (? * (1 << (attempts - 1))) END"
    )


def _pending_query(
    *,
    table: str,
    id_column: str,
    user_id: str | None,
    limit: int,
) -> tuple[str, tuple[object, ...]]:
    retry_expression = _retry_expression()
    user_clause = "" if user_id is None else " WHERE actor_user_id = ?"
    query = (
        f"SELECT {id_column} AS object_id, actor_user_id, session_handle, attempts, "
        f"{retry_expression} AS retry_at FROM {table}"
        f"{user_clause} ORDER BY retry_at, queued_at LIMIT ?"
    )
    params: list[object] = [
        _DELETION_RETRY_MAX_EXPONENT,
        _DELETION_RETRY_MAX_SECONDS,
        _DELETION_RETRY_BASE_SECONDS,
    ]
    if user_id is not None:
        params.append(user_id)
    params.append(limit)
    return query, tuple(params)


def _session_from_row(row: Row) -> VideoSession:
    return VideoSession(
        handle=str(row["handle"]),
        conversation_id=int(row["conversation_id"]),
        actor_user_id=str(row["actor_user_id"]),
        guild_id=str(row["guild_id"]),
        source_kind=str(row["source_kind"]),
        source_display_name=str(row["source_display_name"]),
        source_locator=str(row["source_locator"]),
        source_byte_size=(
            int(row["source_byte_size"]) if row["source_byte_size"] is not None else None
        ),
        youtube_url=str(row["youtube_url"]),
        youtube_video_id=str(row["youtube_video_id"]),
        model=str(row["model"]),
        latest_interaction_id=str(row["latest_interaction_id"]),
        interaction_count=int(row["interaction_count"]),
        created_at=float(row["created_at"]),
        last_active_at=float(row["last_active_at"]),
        expires_at=float(row["expires_at"]),
    )
