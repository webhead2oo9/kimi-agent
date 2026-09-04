from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Literal, cast

from providers.image_caption import is_image_caption
from providers.types import ContentPart, ContentPartType, ConversationMessage
from storage.db import Database

log = logging.getLogger(__name__)

MAX_PERSISTED_CONVERSATION_IMAGES = 10

ConversationAccessScope = Literal["channel_shared", "owner_only"]
CHANNEL_SHARED: ConversationAccessScope = "channel_shared"
OWNER_ONLY: ConversationAccessScope = "owner_only"


@dataclass(frozen=True)
class StoredMessage:
    id: int
    role: str
    user_id: str | None
    user_name: str | None
    content: str | None
    message_data: dict
    created_at: float
    source_created_at: float | None = None
    discord_message_id: str | None = None


@dataclass(frozen=True)
class ChannelMessageRecord:
    """A real Discord channel message persisted to the transcript, deduped by id.

    Persistence DTO, defined here (not in agent/) so the store types against it
    without importing agent. Carries its own author and Discord source timestamp
    so source-anchored memory writes can enforce per-user boundaries.
    """

    discord_message_id: str
    role: str  # "user" | "assistant"
    author_id: str | None  # None for the bot's own messages
    author_name: str | None
    content: str  # clean text; chunk-marker stripped; NO "Name:" prefix
    source_created_at: float | None = None
    content_parts: list[ContentPart] | None = None


@dataclass(frozen=True)
class ConversationRecord:
    id: int
    key: str
    channel_name: str
    guild_id: str | None
    channel_id: str | None
    thread_id: str | None
    root_discord_message_id: str | None
    owner_user_id: str | None = None
    access_scope: ConversationAccessScope = CHANNEL_SHARED


@dataclass(frozen=True)
class UserDataDeletion:
    """Outcome of an on-demand per-user transcript deletion."""

    conversations_deleted: int
    messages_scrubbed: int
    coding_tasks_deleted: int = 0


class ConversationStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def get_or_create(
        self,
        key: str,
        channel_name: str = "",
        *,
        guild_id: str | None = None,
        channel_id: str | None = None,
        thread_id: str | None = None,
        root_discord_message_id: str | None = None,
        owner_user_id: str | None = None,
        access_scope: str = CHANNEL_SHARED,
    ) -> int:
        if access_scope not in (CHANNEL_SHARED, OWNER_ONLY):
            raise ValueError(f"Invalid conversation access scope: {access_scope!r}")
        if access_scope == OWNER_ONLY and not owner_user_id:
            raise ValueError("owner_only conversations require owner_user_id")
        now = time.time()
        async with self._db.write_transaction() as conn:
            await conn.execute(
                "INSERT OR IGNORE INTO conversations "
                "(key, channel_name, guild_id, channel_id, thread_id, "
                "root_discord_message_id, owner_user_id, access_scope, "
                "created_at, last_active_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    key,
                    channel_name,
                    guild_id,
                    channel_id,
                    thread_id,
                    root_discord_message_id,
                    owner_user_id,
                    access_scope,
                    now,
                    now,
                ),
            )
            # Authorize against the persisted row before filling any missing
            # metadata. In particular, an owner-only row with no owner
            # is intentionally unusable; the first caller must not be able to
            # claim it merely by supplying an owner id.
            async with conn.execute(
                "SELECT owner_user_id, access_scope FROM conversations WHERE key = ?",
                (key,),
            ) as cur:
                existing_owner_row = await cur.fetchone()
            existing_owner = existing_owner_row["owner_user_id"] if existing_owner_row else None
            existing_scope = existing_owner_row["access_scope"] if existing_owner_row else None
            if existing_scope == OWNER_ONLY and (
                access_scope != OWNER_ONLY or existing_owner != owner_user_id
            ):
                raise PermissionError(
                    "owner_only conversation requires its matching owner and scope"
                )
            # Resolving an existing root is activity too. Refresh it before the
            # turn's preparation awaits so the retention sweeper cannot delete a
            # live conversation between resolution and transcript persistence.
            await conn.execute(
                "UPDATE conversations SET last_active_at = ?, "
                "owner_user_id = COALESCE(owner_user_id, ?), "
                "access_scope = CASE "
                "WHEN ? = 'owner_only' THEN 'owner_only' ELSE access_scope END "
                "WHERE key = ?",
                (now, owner_user_id, access_scope, key),
            )
            async with conn.execute(
                "SELECT owner_user_id, access_scope FROM conversations WHERE key = ?",
                (key,),
            ) as cur:
                owner_row = await cur.fetchone()
            actual_owner = owner_row["owner_user_id"] if owner_row else None
            actual_scope = owner_row["access_scope"] if owner_row else None
            if actual_scope == OWNER_ONLY and (
                access_scope != OWNER_ONLY or actual_owner != owner_user_id
            ):
                raise PermissionError(
                    "owner_only conversation requires its matching owner and scope"
                )

        async with self._db.conn.execute(
            "SELECT id FROM conversations WHERE key = ?", (key,)
        ) as cur:
            row = await cur.fetchone()
            if row:
                return int(row["id"])

        raise RuntimeError(f"Failed to create conversation for key {key!r}")

    async def touch(self, conversation_id: int) -> bool:
        """Refresh a resolved conversation before any long turn preparation awaits."""
        async with self._db.write_transaction() as conn:
            cursor = await conn.execute(
                "UPDATE conversations SET last_active_at = ? WHERE id = ?",
                (time.time(), conversation_id),
            )
        return cursor.rowcount > 0

    async def get_conversation_by_discord_message(
        self,
        discord_message_id: str,
        *,
        channel_id: str,
    ) -> ConversationRecord | None:
        conn = self._db.conn
        async with conn.execute(
            "SELECT c.id, c.key, c.channel_name, c.guild_id, c.channel_id, "
            "c.thread_id, c.root_discord_message_id, c.owner_user_id, "
            "c.access_scope "
            "FROM message_contexts mc "
            "JOIN conversations c ON c.id = mc.conversation_id "
            "WHERE mc.discord_message_id = ? AND mc.channel_id = ?",
            (discord_message_id, channel_id),
        ) as cur:
            row = await cur.fetchone()
        return self._conversation_record_from_row(row)

    async def get_continuation_conversation_for_reply(
        self,
        discord_message_id: str,
        *,
        channel_id: str,
        requester_user_id: str,
    ) -> ConversationRecord | None:
        """Resolve a conversation from a replied-to message, bot-authored only.

        This is reply *routing*, not the respond/ignore decision (the bot must
        already be mentioned; see ``discord_io.should_respond``). It continues
        the existing root only when the pinged reply targets one of the bot's
        own messages. Owner-only conversations additionally require an exact,
        non-empty owner/requester match. A missing owner therefore fails closed.
        A human trigger is persisted as a ``role='user'``
        transcript row, so those are excluded; the bot's replies
        (``role='assistant'``) and its narration/activity messages (mapped
        without a transcript row) both qualify. Replying to a human message
        therefore falls through to a fresh root.
        """
        conn = self._db.conn
        async with conn.execute(
            "SELECT c.id, c.key, c.channel_name, c.guild_id, c.channel_id, "
            "c.thread_id, c.root_discord_message_id, c.owner_user_id, "
            "c.access_scope "
            "FROM message_contexts mc "
            "JOIN conversations c ON c.id = mc.conversation_id "
            "WHERE mc.discord_message_id = ? AND mc.channel_id = ? "
            "AND (c.access_scope = 'channel_shared' OR ("
            "  c.access_scope = 'owner_only' "
            "  AND c.owner_user_id IS NOT NULL "
            "  AND TRIM(c.owner_user_id) != '' "
            "  AND c.owner_user_id = ?"
            ")) "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM messages m "
            "  WHERE m.conversation_id = mc.conversation_id "
            "  AND m.discord_message_id = mc.discord_message_id "
            "  AND m.role = 'user'"
            ")",
            (discord_message_id, channel_id, requester_user_id),
        ) as cur:
            row = await cur.fetchone()
        return self._conversation_record_from_row(row)

    @staticmethod
    def _conversation_record_from_row(row: Any) -> ConversationRecord | None:
        if row is None:
            return None
        return ConversationRecord(
            id=int(row["id"]),
            key=str(row["key"]),
            channel_name=str(row["channel_name"] or ""),
            guild_id=row["guild_id"],
            channel_id=row["channel_id"],
            thread_id=row["thread_id"],
            root_discord_message_id=row["root_discord_message_id"],
            owner_user_id=row["owner_user_id"],
            access_scope=cast(ConversationAccessScope, str(row["access_scope"])),
        )

    async def load_activated_tools(self, conversation_id: int) -> set[str]:
        conn = self._db.conn
        async with conn.execute(
            "SELECT tool_name FROM conversation_activated_tools WHERE conversation_id = ?",
            (conversation_id,),
        ) as cur:
            rows = await cur.fetchall()
        return {str(row["tool_name"]) for row in rows if row["tool_name"]}

    async def add_activated_tools(self, conversation_id: int, names: set[str]) -> None:
        rows = [(conversation_id, name) for name in sorted(names) if name]
        if not rows:
            return
        async with self._db.write_transaction() as conn:
            await conn.executemany(
                "INSERT OR IGNORE INTO conversation_activated_tools "
                "(conversation_id, tool_name) VALUES (?, ?)",
                rows,
            )

    async def load_recent_conversation_messages(
        self,
        conversation_id: int,
        limit: int = 20,
        before_discord_message_id: str | None = None,
    ) -> list[ConversationMessage]:
        messages = await self.load_recent_stored_messages(
            conversation_id,
            limit,
            before_discord_message_id=before_discord_message_id,
        )
        return [
            message
            for stored in messages
            if (message := _stored_to_history_conversation_message(stored)) is not None
        ]

    async def load_recent_stored_messages(
        self,
        conversation_id: int,
        limit: int = 20,
        before_discord_message_id: str | None = None,
    ) -> list[StoredMessage]:
        conn = self._db.conn
        before_row_id: int | None = None
        if before_discord_message_id:
            async with conn.execute(
                "SELECT id FROM messages WHERE conversation_id = ? AND discord_message_id = ?",
                (conversation_id, before_discord_message_id),
            ) as cur:
                row = await cur.fetchone()
            if row is not None:
                before_row_id = int(row["id"])

        where = "WHERE conversation_id = ?"
        params: list[Any] = [conversation_id]
        if before_row_id is not None:
            where += " AND id < ?"
            params.append(before_row_id)
        params.append(limit)

        async with conn.execute(
            "SELECT id, role, user_id, user_name, content, message_data, "
            "created_at, source_created_at, discord_message_id "
            "FROM ("
            "  SELECT id, role, user_id, user_name, content, message_data, "
            "  created_at, source_created_at, discord_message_id "
            "  FROM messages "
            f"  {where} ORDER BY id DESC LIMIT ?"
            ") sub ORDER BY id ASC",
            tuple(params),
        ) as cur:
            rows = await cur.fetchall()
            return _stored_messages_from_rows(rows)

    async def count_user_messages(
        self,
        user_id: str,
        *,
        exclude_discord_message_id: str | None = None,
        limit: int | None = None,
    ) -> int:
        """Count messages authored by ``user_id`` across all conversations, optionally
        excluding one Discord message id (the current trigger may already be persisted).
        Callers use this to detect a user with little or no prior history with the bot.
        ``limit`` caps the count (and the scan) when the caller only needs a threshold
        check. The result is ``min(actual, limit)``, so work stays bounded for heavy users.
        """
        conn = self._db.conn
        where = "user_id = ?"
        params: list[Any] = [user_id]
        if exclude_discord_message_id:
            where += " AND discord_message_id != ?"
            params.append(exclude_discord_message_id)
        if limit is not None and limit > 0:
            sql = f"SELECT COUNT(*) AS n FROM (SELECT 1 FROM messages WHERE {where} LIMIT ?)"
            params.append(limit)
        else:
            sql = f"SELECT COUNT(*) AS n FROM messages WHERE {where}"
        async with conn.execute(sql, tuple(params)) as cur:
            row = await cur.fetchone()
        return int(row["n"]) if row else 0

    async def save_channel_messages(
        self,
        conversation_id: int,
        records: list[ChannelMessageRecord],
        *,
        context_channel_id: str | None = None,
    ) -> int | None:
        """Persist real channel messages, deduped by (conversation_id, discord id).

        Each row carries its own author so per-user memory writes and source
        lookup attribute content to the right user. Returns MAX(message id) for
        the conversation.
        """
        if not records:
            return None
        now = time.time()
        rows = [
            (
                conversation_id,
                record.role,
                record.author_id,
                record.author_name,
                record.content,
                json.dumps(_message_data_for_record(record)),
                record.discord_message_id,
                record.source_created_at,
                now,
            )
            for record in records
        ]
        # Multi-statement unit: a transcript row committed without its
        # message_contexts mapping permanently breaks reply-continuation routing
        # for that message, so the whole unit is scoped atomically.
        async with self._db.write_transaction() as conn:
            await conn.executemany(
                "INSERT OR IGNORE INTO messages "
                "(conversation_id, role, user_id, user_name, content, message_data, "
                "discord_message_id, source_created_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            if context_channel_id is not None:
                await conn.executemany(
                    "INSERT OR IGNORE INTO message_contexts "
                    "(discord_message_id, conversation_id, channel_id, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    [
                        (
                            record.discord_message_id,
                            conversation_id,
                            context_channel_id,
                            now,
                        )
                        for record in records
                    ],
                )
            await conn.execute(
                "UPDATE conversations SET last_active_at = ? WHERE id = ?",
                (now, conversation_id),
            )
            if any(_record_has_image_parts(record) for record in records):
                await _enforce_image_part_limit(
                    conn,
                    conversation_id,
                    max_images=MAX_PERSISTED_CONVERSATION_IMAGES,
                )
        conn = self._db.conn
        async with conn.execute(
            "SELECT MAX(id) FROM messages WHERE conversation_id = ?",
            (conversation_id,),
        ) as cur:
            row = await cur.fetchone()
            return int(row[0]) if row and row[0] is not None else None

    async def map_message_context(
        self,
        discord_message_id: str,
        conversation_id: int,
        channel_id: str,
    ) -> None:
        """Route a Discord message id to a conversation without a transcript row."""
        async with self._db.write_transaction() as conn:
            await conn.execute(
                "INSERT OR IGNORE INTO message_contexts "
                "(discord_message_id, conversation_id, channel_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (discord_message_id, conversation_id, channel_id, time.time()),
            )

    async def map_thread_conversation(
        self,
        thread_id: str,
        conversation_id: int,
        *,
        creator_user_id: str,
        auto_respond: bool = True,
    ) -> None:
        """Enroll a bot-created thread: its messages continue this conversation.

        ``auto_respond`` is always written explicitly because this is an INSERT OR
        REPLACE: re-enrolling a thread would otherwise silently reset its mode
        back to the column default.
        """
        creator_user_id = creator_user_id.strip()
        if not creator_user_id:
            raise ValueError("creator_user_id is required for a managed thread")
        async with self._db.write_transaction() as conn:
            await conn.execute(
                "INSERT OR REPLACE INTO thread_conversations "
                "(thread_id, conversation_id, creator_user_id, created_at, auto_respond) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    thread_id,
                    conversation_id,
                    creator_user_id,
                    time.time(),
                    int(auto_respond),
                ),
            )

    async def get_thread_creator_user_id(self, thread_id: str) -> str | None:
        """The user who requested this managed thread, if recorded.

        A row without a recoverable creator returns None so lifecycle
        authorization fails closed.
        """
        async with self._db.conn.execute(
            "SELECT creator_user_id FROM thread_conversations WHERE thread_id = ?",
            (thread_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        creator = str(row["creator_user_id"] or "").strip()
        return creator or None

    async def set_thread_auto_respond(self, thread_id: str, auto_respond: bool) -> bool:
        """Pause or resume no-mention replies in an already-managed thread.

        False when no row was updated: the mapping can be swept out from under a
        live thread id (retention, privacy deletion), and reporting success there
        would have the bot announce a mode change nothing durable backs.
        """
        async with self._db.write_transaction() as conn:
            cursor = await conn.execute(
                "UPDATE thread_conversations SET auto_respond = ? WHERE thread_id = ?",
                (int(auto_respond), thread_id),
            )
            return cursor.rowcount > 0

    async def get_thread_conversation(self, thread_id: str) -> ConversationRecord | None:
        conn = self._db.conn
        async with conn.execute(
            "SELECT c.id, c.key, c.channel_name, c.guild_id, c.channel_id, "
            "c.thread_id, c.root_discord_message_id, c.owner_user_id, "
            "c.access_scope "
            "FROM thread_conversations tc "
            "JOIN conversations c ON c.id = tc.conversation_id "
            "WHERE tc.thread_id = ?",
            (thread_id,),
        ) as cur:
            row = await cur.fetchone()
        return self._conversation_record_from_row(row)

    async def delete_thread_conversation(self, thread_id: str) -> None:
        async with self._db.write_transaction() as conn:
            await conn.execute(
                "DELETE FROM thread_conversations WHERE thread_id = ?",
                (thread_id,),
            )

    async def delete_owner_conversation(self, key: str, owner_user_id: str) -> bool:
        """Delete one exact owner-only root, including its transcript.

        The owner predicate makes this safe for caller-scoped reset surfaces.
        Related mappings, activated tools, retained-watermark rows, and coding
        records cascade with the conversation; long-term memory and workspace
        data are deliberately outside this operation.
        """

        async with self._db.write_transaction() as conn:
            async with conn.execute(
                "SELECT id FROM conversations "
                "WHERE key = ? AND owner_user_id = ? AND access_scope = ?",
                (key, owner_user_id, OWNER_ONLY),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                return False
            conversation_id = int(row["id"])
            await conn.execute(
                "DELETE FROM messages WHERE conversation_id = ?",
                (conversation_id,),
            )
            await conn.execute(
                "DELETE FROM conversations WHERE id = ?",
                (conversation_id,),
            )
            return True

    async def list_thread_conversations(self) -> list[tuple[str, bool]]:
        """Every managed thread id paired with its auto-respond mode."""
        conn = self._db.conn
        async with conn.execute("SELECT thread_id, auto_respond FROM thread_conversations") as cur:
            rows = await cur.fetchall()
        return [(str(row["thread_id"]), bool(row["auto_respond"])) for row in rows]

    async def delete_conversations_older_than(
        self,
        cutoff: float,
        *,
        limit: int = 500,
    ) -> int:
        """Purge whole conversations whose last activity predates ``cutoff`` (epoch secs).

        Deletes the transcript ``messages`` rows first, then the ``conversations``
        rows: the conversation delete CASCADEs ``message_contexts``,
        ``thread_conversations``, ``conversation_activated_tools``, and
        ``auto_retain_watermarks``. ``messages`` has no ``ON DELETE CASCADE`` and
        ``foreign_keys=ON``, so deleting it after the conversation would fail the
        FK check; it must go first. Both statements share one ``cutoff`` inside a
        single serialized write transaction, so they see a consistent slice.

        This is the raw SQLite transcript only; Hindsight memory banks are a
        separate store and are left untouched (docs/privacy.md). ``limit`` bounds
        one batch so a large backlog drains across several calls instead of one
        long-held write lock; returns the number of conversations removed.
        """
        async with self._db.write_transaction() as conn:
            async with conn.execute(
                "SELECT id FROM conversations WHERE last_active_at < ? "
                "ORDER BY last_active_at ASC LIMIT ?",
                (cutoff, max(1, limit)),
            ) as cur:
                ids = [int(row["id"]) for row in await cur.fetchall()]
            if not ids:
                return 0
            placeholders = ",".join("?" for _ in ids)
            await conn.execute(
                f"DELETE FROM messages WHERE conversation_id IN ({placeholders})",
                ids,
            )
            await conn.execute(
                f"DELETE FROM conversations WHERE id IN ({placeholders})",
                ids,
            )
            return len(ids)

    async def list_user_conversation_keys(self, user_id: str) -> list[str]:
        """Return every root whose transcript a user deletion can mutate.

        Callers use these stable logical keys to drain in-flight turns before
        :meth:`delete_user_data`. Owner-only roots are included even when they no
        longer have a user-authored message, while the ``EXISTS`` arm covers the
        user's messages inside roots owned by someone else.
        """

        conn = self._db.conn
        async with conn.execute(
            """
            SELECT DISTINCT c.key
            FROM conversations c
            WHERE c.owner_user_id = ?
               OR EXISTS (
                   SELECT 1
                   FROM thread_conversations tc
                   WHERE tc.conversation_id = c.id
                     AND tc.creator_user_id = ?
               )
               OR EXISTS (
                   SELECT 1
                   FROM messages m
                   WHERE m.conversation_id = c.id
                     AND m.user_id = ?
               )
            ORDER BY c.key
            """,
            (user_id, user_id, user_id),
        ) as cursor:
            rows = await cursor.fetchall()
        return [str(row["key"]) for row in rows]

    async def rooted_conversation_ids(self, user_id: str) -> list[int]:
        """Conversation ids rooted by this user (deletion/cancellation scope)."""
        async with self._db.conn.execute(
            "SELECT c.id FROM conversations c "
            "WHERE c.owner_user_id = ? OR EXISTS ("
            "SELECT 1 FROM messages m "
            "WHERE m.conversation_id = c.id "
            "AND m.discord_message_id = c.root_discord_message_id "
            "AND m.user_id = ?"
            ")",
            (user_id, user_id),
        ) as cur:
            return [int(row["id"]) for row in await cur.fetchall()]

    async def delete_user_data(self, user_id: str) -> UserDataDeletion:
        """Immediately delete one user's transcript data, on demand (docs/privacy.md).

        Three scopes, in a single serialized write transaction so they see one
        consistent slice:

        1. **Conversations the user rooted**: roots carry an explicit
           ``owner_user_id`` so assistant-only timeout transcripts and scheduled
           firings remain attributable. A root-message join also catches an
           ownerless row whose first persisted message identifies the user.
        2. **The user's own messages elsewhere**: their rows are scrubbed from any
           remaining (shared) conversation, leaving other participants' messages and
           the bot's replies intact.
        3. **Managed threads they initiated elsewhere**: the creator marker is
           cleared without deleting another user's shared conversation. Lifecycle
           actions then fail closed to STAFF or Discord Manage Threads.

        ``messages`` has no ``ON DELETE CASCADE`` and ``foreign_keys=ON``, so its
        rows are deleted before the ``conversations`` rows (which CASCADE
        ``message_contexts``, ``thread_conversations``,
        ``conversation_activated_tools``, ``image_distillations``, and
        ``auto_retain_watermarks``). Cached image distillations for a surviving
        shared conversation are invalidated before one participant's messages
        are scrubbed, because an aggregate description may include their image
        content. This is the raw SQLite transcript only; Hindsight memory is a
        separate store (see ``memory/privacy.py:forget_user_memory``) and usage
        ledgers are excluded by design, same as the retention sweep.
        """
        async with self._db.write_transaction() as conn:
            # Coding tasks carry their own internal journal and job records.
            # Delete them explicitly for tasks the user started in a shared root;
            # rooted tasks would also disappear through the conversation FK.
            async with conn.execute(
                "SELECT COUNT(*) FROM coding_tasks WHERE user_id = ?", (user_id,)
            ) as cursor:
                coding_row = await cursor.fetchone()
            coding_tasks_deleted = int(coding_row[0] or 0) if coding_row is not None else 0
            await conn.execute("DELETE FROM coding_tasks WHERE user_id = ?", (user_id,))
            await conn.execute(
                "DELETE FROM image_distillations WHERE conversation_id IN ("
                "SELECT DISTINCT conversation_id FROM messages WHERE user_id = ?"
                ")",
                (user_id,),
            )
            async with conn.execute(
                "SELECT c.id FROM conversations c "
                "WHERE c.owner_user_id = ? OR EXISTS ("
                "SELECT 1 FROM messages m "
                "WHERE m.conversation_id = c.id "
                "AND m.discord_message_id = c.root_discord_message_id "
                "AND m.user_id = ?"
                ")",
                (user_id, user_id),
            ) as cur:
                rooted_ids = [int(row["id"]) for row in await cur.fetchall()]
            conversations_deleted = 0
            if rooted_ids:
                placeholders = ",".join("?" for _ in rooted_ids)
                await conn.execute(
                    f"DELETE FROM messages WHERE conversation_id IN ({placeholders})",
                    rooted_ids,
                )
                await conn.execute(
                    f"DELETE FROM conversations WHERE id IN ({placeholders})",
                    rooted_ids,
                )
                conversations_deleted = len(rooted_ids)
            # Rooted conversations' rows are already gone, so this only touches the
            # user's messages left in conversations other people rooted.
            await conn.execute(
                "DELETE FROM message_contexts WHERE discord_message_id IN ("
                "SELECT discord_message_id FROM messages "
                "WHERE user_id = ? AND discord_message_id IS NOT NULL"
                ")",
                (user_id,),
            )
            scrub = await conn.execute(
                "DELETE FROM messages WHERE user_id = ?",
                (user_id,),
            )
            await conn.execute(
                "UPDATE thread_conversations SET creator_user_id = NULL WHERE creator_user_id = ?",
                (user_id,),
            )
            messages_scrubbed = scrub.rowcount if scrub.rowcount and scrub.rowcount > 0 else 0
            return UserDataDeletion(
                conversations_deleted=conversations_deleted,
                messages_scrubbed=messages_scrubbed,
                coding_tasks_deleted=coding_tasks_deleted,
            )

    async def get_message_by_discord_id(
        self, conversation_id: int, discord_message_id: str
    ) -> StoredMessage | None:
        conn = self._db.conn
        async with conn.execute(
            "SELECT id, role, user_id, user_name, content, message_data, "
            "created_at, source_created_at, discord_message_id "
            "FROM messages "
            "WHERE conversation_id = ? AND discord_message_id = ?",
            (conversation_id, discord_message_id),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        messages = _stored_messages_from_rows([row])
        return messages[0] if messages else None

    async def load_message_window(
        self,
        conversation_id: int,
        anchor_message_id: int,
        *,
        before: int = 2,
        after: int = 2,
    ) -> list[StoredMessage]:
        before = max(0, before)
        after = max(0, after)
        conn = self._db.conn
        select_cols = (
            "id, role, user_id, user_name, content, message_data, "
            "created_at, source_created_at, discord_message_id "
        )
        async with conn.execute(
            f"SELECT {select_cols} FROM messages "
            "WHERE conversation_id = ? AND id < ? "
            "ORDER BY id DESC LIMIT ?",
            (conversation_id, anchor_message_id, before),
        ) as cur:
            before_rows = await cur.fetchall()
        async with conn.execute(
            f"SELECT {select_cols} FROM messages WHERE conversation_id = ? AND id = ?",
            (conversation_id, anchor_message_id),
        ) as cur:
            anchor_row = await cur.fetchone()
        async with conn.execute(
            f"SELECT {select_cols} FROM messages "
            "WHERE conversation_id = ? AND id > ? "
            "ORDER BY id ASC LIMIT ?",
            (conversation_id, anchor_message_id, after),
        ) as cur:
            after_rows = await cur.fetchall()

        rows = list(reversed(list(before_rows)))
        if anchor_row is not None:
            rows.append(anchor_row)
        rows.extend(after_rows)
        return _stored_messages_from_rows(rows)


def _stored_messages_from_rows(rows: Any) -> list[StoredMessage]:
    return [
        StoredMessage(
            id=r["id"],
            role=r["role"],
            user_id=r["user_id"],
            user_name=r["user_name"],
            content=r["content"],
            message_data=json.loads(r["message_data"]),
            created_at=r["created_at"],
            source_created_at=r["source_created_at"],
            discord_message_id=r["discord_message_id"],
        )
        for r in rows
    ]


def _stored_to_history_conversation_message(
    message: StoredMessage,
) -> ConversationMessage | None:
    if message.role not in {"user", "assistant"}:
        return None
    if message.message_data.get("tool_calls"):
        return None
    raw_provider_data = message.message_data.get("raw_provider_data")
    if isinstance(raw_provider_data, dict) and raw_provider_data.get("tool_calls"):
        return None
    content = _content_parts_from_data(message.message_data.get("content"))
    if not content:
        return None
    if message.role == "user" and message.user_name:
        content = _ensure_user_label(content, message.user_name)
    return ConversationMessage(
        # The persisted role is a free-form column; only the three literals are
        # ever written, and mypy cannot narrow a str read back out of SQLite.
        role=message.role,  # type: ignore[arg-type]
        content=content,
        raw_provider_data=raw_provider_data if isinstance(raw_provider_data, dict) else {},
        source_discord_message_id=message.discord_message_id,
    )


def _message_data_for_record(record: ChannelMessageRecord) -> dict[str, Any]:
    parts = (
        record.content_parts
        if record.content_parts is not None
        else ([ContentPart.from_text(record.content)] if record.content else [])
    )
    return {
        "role": record.role,
        "content": [_content_part_to_data(part) for part in parts],
    }


def _content_part_to_data(part: ContentPart) -> dict[str, Any]:
    if part.type is ContentPartType.IMAGE:
        data: dict[str, Any] = {
            "type": "image",
            "image_url": part.image_url,
            "media_type": part.media_type,
        }
        if part.detail:
            data["detail"] = part.detail
        return data
    return {"type": "text", "text": part.text or ""}


def _record_has_image_parts(record: ChannelMessageRecord) -> bool:
    return any(part.type is ContentPartType.IMAGE for part in (record.content_parts or []))


async def _enforce_image_part_limit(
    conn: Any,
    conversation_id: int,
    *,
    max_images: int,
) -> None:
    if max_images < 0:
        max_images = 0
    async with conn.execute(
        "SELECT id, message_data FROM messages WHERE conversation_id = ? ORDER BY id ASC",
        (conversation_id,),
    ) as cur:
        rows = await cur.fetchall()

    image_refs: list[tuple[int, int]] = []
    parsed: dict[int, dict[str, Any]] = {}
    for row in rows:
        row_id = int(row["id"])
        try:
            data = json.loads(row["message_data"])
        except TypeError, json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        parsed[row_id] = data
        content = data.get("content")
        if not isinstance(content, list):
            continue
        for index, part in enumerate(content):
            if isinstance(part, dict) and part.get("type") == "image":
                image_refs.append((row_id, index))

    excess = len(image_refs) - max_images
    if excess <= 0:
        return

    remove_by_row: dict[int, set[int]] = {}
    for row_id, index in image_refs[:excess]:
        remove_by_row.setdefault(row_id, set()).add(index)

    for row_id, indexes in remove_by_row.items():
        data = parsed[row_id]
        content = data.get("content")
        if not isinstance(content, list):
            continue
        data["content"] = [part for index, part in enumerate(content) if index not in indexes]
        await conn.execute(
            "UPDATE messages SET message_data = ? WHERE id = ?",
            (json.dumps(data), row_id),
        )


def _ensure_user_label(parts: list[ContentPart], user_name: str) -> list[ContentPart]:
    """Prefix the first text part with the username if not already labeled."""
    if not parts:
        return parts
    first = parts[0]
    if first.type is not ContentPartType.TEXT or not first.text:
        return parts
    # Once an image-only message loses its image to eviction, its caption is the
    # first part left. Labeling it would pass off a machine description as the
    # user's own words.
    if is_image_caption(first.text):
        return parts
    if first.text.startswith(f"{user_name}: "):
        return parts
    labeled = ContentPart.from_text(f"{user_name}: {first.text}")
    return [labeled, *parts[1:]]


def _content_parts_from_data(content: Any) -> list[ContentPart]:
    if isinstance(content, list):
        parts: list[ContentPart] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                text = part.get("text")
                if isinstance(text, str) and text:
                    parts.append(ContentPart.from_text(text))
            elif part.get("type") == "image":
                image_url = part.get("image_url")
                media_type = part.get("media_type")
                if isinstance(image_url, str) and isinstance(media_type, str):
                    parts.append(
                        ContentPart.from_image_url(
                            url=image_url,
                            media_type=media_type,
                            detail=part.get("detail") or "auto",
                        )
                    )
        return parts
    return []
