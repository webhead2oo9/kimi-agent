"""Persistence for private dashboard chats; model history stays in conversations."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from typing import Any
from uuid import uuid4

from storage.conversations import OWNER_ONLY, ConversationStore
from storage.db import Database


@dataclass(frozen=True, slots=True)
class DashboardConversation:
    id: str
    conversation_id: int
    key: str
    user_id: str
    guild_id: str
    channel_id: str
    parent_channel_id: str
    channel_name: str
    title: str
    created_at: float
    updated_at: float
    parent_id: str | None = None
    parent_event_id: int | None = None
    parent_title: str | None = None

    def public(self) -> dict[str, Any]:
        return {
            k: v for k, v in asdict(self).items() if k not in {"key", "conversation_id", "user_id"}
        }


@dataclass(frozen=True, slots=True)
class DashboardEvent:
    id: int
    kind: str
    payload: dict[str, Any]
    created_at: float


@dataclass(frozen=True, slots=True)
class DashboardFile:
    id: str
    dashboard_id: str
    owner_user_id: str
    guild_id: str
    path: str
    filename: str
    media_type: str
    size: int
    kind: str
    created_at: float

    def public(self) -> dict[str, Any]:
        return {
            k: v
            for k, v in asdict(self).items()
            if k not in {"path", "dashboard_id", "owner_user_id", "guild_id"}
        }


class DashboardBusyError(ValueError):
    pass


_CHAT_SELECT = """
SELECT d.id, d.conversation_id, c.key, c.owner_user_id AS user_id,
    c.guild_id, c.channel_id, d.parent_channel_id, c.channel_name,
    d.title, d.created_at, d.updated_at,
    b.parent_id, b.parent_event_id, coalesce(p.title,b.parent_title) AS parent_title
FROM dashboard_conversations d JOIN conversations c ON c.id=d.conversation_id
LEFT JOIN dashboard_branches b ON b.dashboard_id=d.id
LEFT JOIN dashboard_conversations p ON p.id=b.parent_id
WHERE c.access_scope='owner_only'
"""


class DashboardStore:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.conversations = ConversationStore(db)

    async def create(
        self,
        *,
        user_id: str,
        guild_id: str,
        channel_id: str,
        parent_channel_id: str,
        channel_name: str,
    ) -> DashboardConversation:
        chat_id, now = uuid4().hex, time.time()
        root = f"dashboard:{guild_id}:{user_id}:{chat_id}"
        conversation_id = await self.conversations.get_or_create(
            root,
            channel_name,
            guild_id=guild_id,
            channel_id=channel_id,
            thread_id=channel_id if parent_channel_id != channel_id else None,
            owner_user_id=user_id,
            access_scope=OWNER_ONLY,
        )
        async with self.db.write_transaction() as conn:
            await conn.execute(
                "INSERT INTO dashboard_conversations "
                "(id,conversation_id,parent_channel_id,created_at,updated_at) VALUES(?,?,?,?,?)",
                (chat_id, conversation_id, parent_channel_id, now, now),
            )
        chat = await self.get(chat_id, user_id=user_id, guild_id=guild_id)
        assert chat is not None
        return chat

    async def get(
        self, chat_id: str, *, user_id: str, guild_id: str
    ) -> DashboardConversation | None:
        async with self.db.conn.execute(
            _CHAT_SELECT + " AND d.id=? AND c.owner_user_id=? AND c.guild_id=?",
            (chat_id, user_id, guild_id),
        ) as cur:
            row = await cur.fetchone()
        return DashboardConversation(**dict(row)) if row else None

    async def for_root(self, root: str) -> DashboardConversation | None:
        """Internal routing only. HTTP callers must use the owner-scoped get()."""
        async with self.db.conn.execute(_CHAT_SELECT + " AND c.key=?", (root,)) as cur:
            row = await cur.fetchone()
        return DashboardConversation(**dict(row)) if row else None

    async def list_chats(
        self, *, user_id: str, guild_id: str, before: float | None = None, before_id: str = ""
    ) -> list[DashboardConversation]:
        async with self.db.conn.execute(
            _CHAT_SELECT + " AND c.owner_user_id=? AND c.guild_id=? "
            "AND (d.updated_at<? OR (d.updated_at=? AND d.id>? AND ?<>'')) "
            "ORDER BY d.updated_at DESC,d.id LIMIT 100",
            (
                user_id,
                guild_id,
                before if before is not None else float("inf"),
                before,
                before_id,
                before_id,
            ),
        ) as cur:
            return [DashboardConversation(**dict(row)) for row in await cur.fetchall()]

    async def rename(self, chat: DashboardConversation, title: str) -> None:
        title = " ".join(title.split())[:120]
        if not title:
            raise ValueError("Give the conversation a name")
        async with self.db.write_transaction() as conn:
            await conn.execute(
                "UPDATE dashboard_conversations SET title=?,title_edited=1,updated_at=? WHERE id=?",
                (title, time.time(), chat.id),
            )

    async def delete(self, chat: DashboardConversation) -> None:
        # Callers first cancel/drain active work and hold the root lock. The
        # existing transcript and task FK lifecycle also owns dashboard records.
        async with self.db.write_transaction() as conn:
            await conn.execute(
                "DELETE FROM messages WHERE conversation_id=?", (chat.conversation_id,)
            )
            await conn.execute(
                "DELETE FROM conversations WHERE id=? AND owner_user_id=? AND access_scope='owner_only'",
                (chat.conversation_id, chat.user_id),
            )

    async def accepted(self, chat_id: str, request_id: str) -> str | None:
        async with self.db.conn.execute(
            "SELECT id FROM dashboard_turns WHERE dashboard_id=? AND request_id=?",
            (chat_id, request_id),
        ) as cur:
            row = await cur.fetchone()
        return str(row[0]) if row else None

    async def accept(
        self,
        chat: DashboardConversation,
        *,
        request_id: str,
        text: str,
        files: list[dict[str, Any]],
    ) -> tuple[str, bool]:
        now, turn_id = time.time(), uuid4().hex
        async with self.db.write_transaction() as conn:
            async with conn.execute(
                "SELECT id FROM dashboard_turns WHERE dashboard_id=? AND request_id=?",
                (chat.id, request_id),
            ) as cur:
                existing = await cur.fetchone()
            if existing:
                return str(existing[0]), False
            async with conn.execute(
                "SELECT 1 FROM dashboard_turns WHERE dashboard_id=? AND status IN ('accepted','running')",
                (chat.id,),
            ) as cur:
                if await cur.fetchone():
                    raise DashboardBusyError("Kimi is still responding in this conversation")
            await conn.execute(
                "INSERT INTO dashboard_turns VALUES(?,?,?,'accepted',?,?)",
                (turn_id, chat.id, request_id, now, now),
            )
            await conn.execute(
                "INSERT INTO dashboard_events(dashboard_id,kind,payload_json,dedup_key,created_at) "
                "VALUES(?,'user_message',?,?,?)",
                (
                    chat.id,
                    json.dumps({"turn_id": turn_id, "text": text, "files": files}),
                    f"{turn_id}:user",
                    now,
                ),
            )
            title = " ".join(text.split())[:80] or (
                str(files[0]["filename"])[:80] if files else "New chat"
            )
            await conn.execute(
                "UPDATE dashboard_conversations SET updated_at=?, "
                "title=CASE WHEN title='New chat' AND title_edited=0 THEN ? ELSE title END WHERE id=?",
                (now, title, chat.id),
            )
            await conn.execute(
                "UPDATE conversations SET last_active_at=? WHERE id=?", (now, chat.conversation_id)
            )
        return turn_id, True

    async def start_turn(self, turn_id: str) -> None:
        async with self.db.write_transaction() as conn:
            await conn.execute(
                "UPDATE dashboard_turns SET status='running',updated_at=? WHERE id=? AND status='accepted'",
                (time.time(), turn_id),
            )

    async def finish_turn(
        self, chat_id: str, turn_id: str, status: str, payload: dict[str, Any]
    ) -> None:
        if status not in {"completed", "failed", "cancelled", "interrupted"}:
            raise ValueError("Invalid dashboard turn outcome")
        now = time.time()
        async with self.db.write_transaction() as conn:
            updated = await conn.execute(
                "UPDATE dashboard_turns SET status=?,updated_at=? "
                "WHERE id=? AND dashboard_id=? AND status IN ('accepted','running')",
                (status, now, turn_id, chat_id),
            )
            if not updated.rowcount:
                return
            await conn.execute(
                "INSERT INTO dashboard_events(dashboard_id,kind,payload_json,dedup_key,created_at) "
                "VALUES(?,'turn_finished',?,?,?)",
                (
                    chat_id,
                    json.dumps({**payload, "turn_id": turn_id, "status": status}),
                    f"{turn_id}:final",
                    now,
                ),
            )
            await conn.execute(
                "UPDATE dashboard_conversations SET updated_at=? WHERE id=?", (now, chat_id)
            )

    async def event(
        self, chat_id: str, kind: str, payload: dict[str, Any], *, key: str | None = None
    ) -> None:
        async with self.db.write_transaction() as conn:
            # A deleted root must never be recreated by a late worker event.
            await conn.execute(
                "INSERT OR IGNORE INTO dashboard_events(dashboard_id,kind,payload_json,dedup_key,created_at) "
                "SELECT id,?,?,?,? FROM dashboard_conversations WHERE id=?",
                (kind, json.dumps(payload), key, time.time(), chat_id),
            )

    async def events(
        self,
        chat_id: str,
        *,
        after: int | None = None,
        before: int | None = None,
        limit: int = 200,
    ) -> list[DashboardEvent]:
        where = "dashboard_id=?"
        values: list[str | int] = [chat_id]
        if after is not None:
            where += " AND id>?"
            values.append(after)
        if before is not None:
            where += " AND id<?"
            values.append(before)
        order = "ASC" if after is not None else "DESC"
        async with self.db.conn.execute(
            f"SELECT id,kind,payload_json,created_at FROM dashboard_events WHERE {where} ORDER BY id {order} LIMIT ?",
            (*values, min(500, max(1, limit))),
        ) as cur:
            rows = await cur.fetchall()
        result = [
            DashboardEvent(
                row["id"], row["kind"], json.loads(row["payload_json"]), row["created_at"]
            )
            for row in rows
        ]
        return result if after is not None else list(reversed(result))

    async def interrupt_unfinished(self) -> None:
        async with self.db.conn.execute(
            "SELECT id,dashboard_id FROM dashboard_turns WHERE status IN ('accepted','running')"
        ) as cur:
            rows = await cur.fetchall()
        for row in rows:
            await self.finish_turn(
                row["dashboard_id"],
                row["id"],
                "interrupted",
                {
                    "text": "The bot restarted before this response finished. Send a new message to continue.",
                },
            )
        async with self.db.conn.execute(
            "SELECT id,dashboard_id FROM dashboard_actions WHERE status='running'"
        ) as cursor:
            actions = await cursor.fetchall()
        for row in actions:
            await self.finish_action(
                row["dashboard_id"],
                row["id"],
                "interrupted",
                {
                    "text": "The bot restarted during this action. Check the task's current state before trying again."
                },
            )

    async def work_events(self, chat_id: str) -> list[DashboardEvent]:
        """Restore task cards independently of the visible transcript page."""
        async with self.db.conn.execute(
            """SELECT id,kind,payload_json,created_at FROM dashboard_events
            WHERE dashboard_id=? AND id IN (
                SELECT max(id) FROM dashboard_events WHERE dashboard_id=? AND (
                    kind='coding_task' OR json_extract(payload_json,'$.task_preview.id') IS NOT NULL
                ) GROUP BY CASE WHEN kind='coding_task'
                    THEN 'coding:' || json_extract(payload_json,'$.id')
                    ELSE 'scheduled:' || json_extract(payload_json,'$.task_preview.id') END
                UNION
                SELECT e.id FROM dashboard_events e JOIN dashboard_actions a
                    ON json_extract(e.payload_json,'$.action_id')=a.id
                WHERE e.dashboard_id=? AND e.kind='task_action' AND a.status='running'
            ) ORDER BY id DESC LIMIT 200""",
            (chat_id, chat_id, chat_id),
        ) as cursor:
            rows = list(await cursor.fetchall())
        return [
            DashboardEvent(row[0], row[1], json.loads(row[2]), row[3]) for row in reversed(rows)
        ]

    async def link_task(self, chat: DashboardConversation, task_id: str, revision: int) -> None:
        async with self.db.write_transaction() as conn:
            await conn.execute(
                "INSERT OR IGNORE INTO dashboard_task_previews VALUES(?,?,?)",
                (chat.id, task_id, revision),
            )

    async def has_task(self, chat: DashboardConversation, task_id: str, revision: int) -> bool:
        async with self.db.conn.execute(
            "SELECT 1 FROM dashboard_task_previews WHERE dashboard_id=? AND task_id=? AND revision=?",
            (chat.id, task_id, revision),
        ) as cursor:
            return await cursor.fetchone() is not None

    async def linked_tasks(self, chat: DashboardConversation) -> list[tuple[str, int]]:
        async with self.db.conn.execute(
            "SELECT task_id,max(revision) FROM dashboard_task_previews WHERE dashboard_id=? GROUP BY task_id LIMIT 100",
            (chat.id,),
        ) as cursor:
            return [(str(row[0]), int(row[1])) for row in await cursor.fetchall()]

    async def accept_action(
        self, chat: DashboardConversation, request_id: str, task_id: str, action: str
    ) -> tuple[str, bool]:
        async with self.db.write_transaction() as conn:
            async with conn.execute(
                "SELECT id FROM dashboard_actions WHERE dashboard_id=? AND request_id=?",
                (chat.id, request_id),
            ) as cursor:
                row = await cursor.fetchone()
            if row:
                return str(row[0]), False
            action_id, now = uuid4().hex, time.time()
            await conn.execute(
                "INSERT INTO dashboard_actions VALUES(?,?,?,?,?,'running',?)",
                (action_id, chat.id, request_id, task_id, action, now),
            )
            await conn.execute(
                "INSERT INTO dashboard_events(dashboard_id,kind,payload_json,dedup_key,created_at) VALUES(?,'task_action',?,?,?)",
                (
                    chat.id,
                    json.dumps({"action_id": action_id, "task_id": task_id, "action": action}),
                    f"action:{action_id}",
                    now,
                ),
            )
            return action_id, True

    async def finish_action(
        self, chat_id: str, action_id: str, status: str, payload: dict[str, Any]
    ) -> None:
        async with self.db.write_transaction() as conn:
            cursor = await conn.execute(
                "UPDATE dashboard_actions SET status=? WHERE id=? AND dashboard_id=? AND status='running'",
                (status, action_id, chat_id),
            )
            if not cursor.rowcount:
                return
            await conn.execute(
                "INSERT INTO dashboard_events(dashboard_id,kind,payload_json,dedup_key,created_at) VALUES(?,'task_action_result',?,?,?)",
                (
                    chat_id,
                    json.dumps({**payload, "action_id": action_id, "status": status}),
                    f"action-result:{action_id}",
                    time.time(),
                ),
            )

    async def event_by_key(self, chat_id: str, key: str) -> DashboardEvent | None:
        async with self.db.conn.execute(
            "SELECT id,kind,payload_json,created_at FROM dashboard_events WHERE dashboard_id=? AND dedup_key=?",
            (chat_id, key),
        ) as cursor:
            row = await cursor.fetchone()
        return DashboardEvent(row[0], row[1], json.loads(row[2]), row[3]) if row else None

    async def add_file(
        self,
        chat: DashboardConversation,
        *,
        path: str,
        filename: str,
        media_type: str,
        size: int,
        kind: str,
    ) -> DashboardFile:
        record = DashboardFile(
            uuid4().hex,
            chat.id,
            chat.user_id,
            chat.guild_id,
            path,
            filename,
            media_type,
            size,
            kind,
            time.time(),
        )
        async with self.db.write_transaction() as conn:
            await conn.execute(
                "INSERT INTO dashboard_files VALUES(?,?,?,?,?,?,?,?,?,?)",
                tuple(asdict(record).values()),
            )
        return record

    async def file(self, file_id: str, *, user_id: str, guild_id: str) -> DashboardFile | None:
        async with self.db.conn.execute(
            "SELECT * FROM dashboard_files WHERE id=? AND owner_user_id=? AND guild_id=?",
            (file_id, user_id, guild_id),
        ) as cur:
            row = await cur.fetchone()
        return DashboardFile(**dict(row)) if row else None

    async def files(self, chat: DashboardConversation) -> list[DashboardFile]:
        async with self.db.conn.execute(
            "SELECT * FROM dashboard_files WHERE dashboard_id=? ORDER BY created_at DESC LIMIT 200",
            (chat.id,),
        ) as cur:
            return [DashboardFile(**dict(row)) for row in await cur.fetchall()]
