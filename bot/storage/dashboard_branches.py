"""Atomic snapshots of private chat context and explicit returns to a parent."""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from typing import Any
from uuid import uuid4

import aiosqlite

from storage.dashboard import (
    DashboardBusyError,
    DashboardConversation,
    DashboardFile,
    DashboardStore,
)

MAX_BRANCH_MESSAGES = 1000
FileCopier = Callable[[str, str], Awaitable[DashboardFile | None]]


def _source_id(kind: str, payload: dict[str, Any]) -> str | None:
    if kind == "user_message" and payload.get("turn_id"):
        return f"dashboard:{payload['turn_id']}:user"
    if kind == "turn_finished" and payload.get("turn_id"):
        return f"dashboard:{payload['turn_id']}:assistant"
    if kind == "coding_task" and payload.get("status") in {
        "completed",
        "failed",
        "cancelled",
        "timed_out",
    }:
        return f"coding:{payload.get('id')}:final"
    if kind in {"history_message", "branch_result"}:
        return payload.get("context_source_id")
    return None


class DashboardBranches:
    def __init__(self, store: DashboardStore, *, copy_file: FileCopier | None = None) -> None:
        self.store = store
        self.copy_file = copy_file

    async def _copy_files(
        self,
        conn: aiosqlite.Connection,
        destination: str,
        files: list[dict[str, Any]],
        copied: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        result = []
        for file in files:
            file_id = file.get("id")
            if not file_id:
                result.append(file)
                continue
            if file_id not in copied:
                if self.copy_file is None:
                    raise RuntimeError("Branch attachments require a snapshot copier")
                record = await self.copy_file(file_id, destination)
                if record is None:
                    copied[file_id] = {"filename": file["filename"], "expired": True}
                else:
                    await conn.execute(
                        "INSERT INTO dashboard_files VALUES(?,?,?,?,?,?,?,?,?,?)",
                        tuple(asdict(record).values()),
                    )
                    copied[file_id] = record.public()
            result.append(copied[file_id])
        return result

    async def _message(
        self, conn: aiosqlite.Connection, chat: DashboardConversation, event_id: int
    ) -> tuple[aiosqlite.Row, dict[str, Any]]:
        async with conn.execute(
            "SELECT kind,payload_json FROM dashboard_events WHERE dashboard_id=? AND id=?",
            (chat.id, event_id),
        ) as cursor:
            event = await cursor.fetchone()
        if event is None:
            raise LookupError("Message not found")
        payload = json.loads(event["payload_json"])
        source_id = _source_id(event["kind"], payload)
        async with conn.execute(
            "SELECT id,role,content,source_id FROM messages "
            "WHERE conversation_id=? AND source_id=? AND role IN ('user','assistant')",
            (chat.conversation_id, source_id),
        ) as cursor:
            message = await cursor.fetchone()
        if message is None:
            raise DashboardBusyError("This message is not available as saved conversation context")
        return message, payload

    async def fork(
        self, parent: DashboardConversation, *, event_id: int, request_id: str
    ) -> DashboardConversation:
        # One transaction covers provenance, model context, and visible history.
        # No task state, approvals, tool activation, or new model turn is copied.
        async with self.store.db.immediate_write_transaction() as conn:
            current = await self.store.get(
                parent.id, user_id=parent.user_id, guild_id=parent.guild_id
            )
            if current is None:
                raise LookupError("Conversation no longer exists")
            parent = current
            async with conn.execute(
                "SELECT dashboard_id,parent_event_id FROM dashboard_branches WHERE parent_id=? AND request_id=?",
                (parent.id, request_id),
            ) as cursor:
                duplicate = await cursor.fetchone()
            if duplicate:
                if duplicate["parent_event_id"] != event_id:
                    raise DashboardBusyError(
                        "This branch request was already used for another message"
                    )
                branch_id = duplicate["dashboard_id"]
            else:
                selected, _ = await self._message(conn, parent, event_id)
                async with conn.execute(
                    "SELECT id,role,content,source_id,created_at FROM messages "
                    "WHERE conversation_id=? AND id<=? AND role IN ('user','assistant') "
                    "ORDER BY id LIMIT ?",
                    (parent.conversation_id, selected["id"], MAX_BRANCH_MESSAGES + 1),
                ) as cursor:
                    messages = list(await cursor.fetchall())
                if len(messages) > MAX_BRANCH_MESSAGES:
                    raise DashboardBusyError(
                        "This history is too large to branch; choose an earlier message"
                    )
                async with conn.execute(
                    "SELECT e.kind,e.payload_json FROM dashboard_events e JOIN messages m ON "
                    "m.conversation_id=? AND m.source_id=CASE "
                    "WHEN e.kind='user_message' THEN 'dashboard:'||json_extract(e.payload_json,'$.turn_id')||':user' "
                    "WHEN e.kind='turn_finished' THEN 'dashboard:'||json_extract(e.payload_json,'$.turn_id')||':assistant' "
                    "WHEN e.kind='coding_task' AND json_extract(e.payload_json,'$.status') IN "
                    "('completed','failed','cancelled','timed_out') THEN 'coding:'||json_extract(e.payload_json,'$.id')||':final' "
                    "WHEN e.kind IN ('history_message','branch_result') THEN json_extract(e.payload_json,'$.context_source_id') "
                    "END WHERE e.dashboard_id=? AND e.id<=? AND m.id<=? ORDER BY e.id",
                    (parent.conversation_id, parent.id, event_id, selected["id"]),
                ) as cursor:
                    originals = list(await cursor.fetchall())
                presentations = {}
                for original in originals:
                    payload = json.loads(original["payload_json"])
                    if original["kind"] == "branch_result":
                        payload["render_markdown"] = True
                    presentations[_source_id(original["kind"], payload)] = payload
                branch_id, now = uuid4().hex, time.time()
                root = f"dashboard:{parent.guild_id}:{parent.user_id}:{branch_id}"
                title = f"Branch · {parent.title}"[:120]
                inserted = await conn.execute(
                    "INSERT INTO conversations(key,channel_name,guild_id,channel_id,thread_id,"
                    "owner_user_id,access_scope,created_at,last_active_at) "
                    "VALUES(?,?,?,?,?,?,'owner_only',?,?)",
                    (
                        root,
                        parent.channel_name,
                        parent.guild_id,
                        parent.channel_id,
                        parent.channel_id
                        if parent.channel_id != parent.parent_channel_id
                        else None,
                        parent.user_id,
                        now,
                        now,
                    ),
                )
                conversation_id = inserted.lastrowid
                await conn.execute(
                    "INSERT INTO dashboard_conversations "
                    "(id,conversation_id,title,title_edited,parent_channel_id,created_at,updated_at) "
                    "VALUES(?,?,?,1,?,?,?)",
                    (branch_id, conversation_id, title, parent.parent_channel_id, now, now),
                )
                await conn.execute(
                    "INSERT INTO dashboard_branches VALUES(?,?,?,?,?)",
                    (branch_id, parent.id, event_id, parent.title, request_id),
                )
                await conn.execute(
                    "INSERT INTO messages(conversation_id,role,user_id,user_name,content,message_data,"
                    "source_id,source_created_at,created_at) "
                    "SELECT ?,role,user_id,user_name,content,message_data,source_id,source_created_at,created_at "
                    "FROM messages WHERE conversation_id=? AND id<=? AND role IN ('user','assistant') ORDER BY id",
                    (conversation_id, parent.conversation_id, selected["id"]),
                )
                copied: dict[str, dict[str, Any]] = {}
                history = []
                for message in messages:
                    original = presentations.get(message["source_id"], {})
                    payload = {
                        "role": message["role"],
                        "text": original.get("text", message["content"]),
                        "context_source_id": message["source_id"],
                        "files": await self._copy_files(
                            conn, branch_id, original.get("files", []), copied
                        ),
                        **{
                            key: original[key]
                            for key in (
                                "render_markdown",
                                "source_chat_id",
                                "source_event_id",
                                "source_title",
                            )
                            if key in original
                        },
                    }
                    history.append((branch_id, json.dumps(payload), message["created_at"]))
                await conn.executemany(
                    "INSERT INTO dashboard_events(dashboard_id,kind,payload_json,created_at) "
                    "VALUES(?,'history_message',?,?)",
                    history,
                )
                await conn.execute(
                    "INSERT INTO dashboard_events(dashboard_id,kind,payload_json,created_at) "
                    "VALUES(?,'branch_created',?,?)",
                    (
                        parent.id,
                        json.dumps({"chat_id": branch_id, "title": title, "event_id": event_id}),
                        now,
                    ),
                )
        branch = await self.store.get(branch_id, user_id=parent.user_id, guild_id=parent.guild_id)
        assert branch is not None
        return branch

    async def return_result(
        self, branch: DashboardConversation, parent: DashboardConversation, *, event_id: int
    ) -> None:
        async with self.store.db.immediate_write_transaction() as conn:
            current = await self.store.get(
                branch.id, user_id=branch.user_id, guild_id=branch.guild_id
            )
            target = await self.store.get(
                parent.id, user_id=branch.user_id, guild_id=branch.guild_id
            )
            if current is None or target is None or current.parent_id != target.id:
                raise LookupError("Parent conversation no longer exists")
            source_id = f"branch-return:{branch.id}:{event_id}"
            if await self.store.event_by_key(parent.id, source_id):
                return
            async with conn.execute(
                "SELECT 1 FROM dashboard_turns WHERE dashboard_id=? AND status IN ('accepted','running')",
                (parent.id,),
            ) as cursor:
                if await cursor.fetchone():
                    raise DashboardBusyError(
                        "Wait for the parent conversation's response to finish"
                    )
            selected, payload = await self._message(conn, current, event_id)
            if selected["role"] != "assistant":
                raise DashboardBusyError("Choose an assistant response to bring back")
            now = time.time()
            text = selected["content"] or ""
            context = f"Result brought back from branch ‘{current.title}’ by the user:\n\n{text}"
            await conn.execute(
                "INSERT INTO messages(conversation_id,role,user_id,content,message_data,source_id,created_at) "
                "VALUES(?,'user',?,?,?,?,?)",
                (
                    parent.conversation_id,
                    branch.user_id,
                    context,
                    json.dumps({"role": "user", "content": [{"type": "text", "text": context}]}),
                    source_id,
                    now,
                ),
            )
            await conn.execute(
                "INSERT INTO dashboard_events(dashboard_id,kind,payload_json,dedup_key,created_at) "
                "VALUES(?,'branch_result',?,?,?)",
                (
                    parent.id,
                    json.dumps(
                        {
                            "text": text,
                            "source_chat_id": branch.id,
                            "source_event_id": event_id,
                            "source_title": current.title,
                            "context_source_id": source_id,
                            "files": await self._copy_files(
                                conn, parent.id, payload.get("files", []), {}
                            ),
                        }
                    ),
                    source_id,
                    now,
                ),
            )
            await conn.execute(
                "INSERT INTO dashboard_events(dashboard_id,kind,payload_json,dedup_key,created_at) "
                "VALUES(?,'branch_returned',?,?,?)",
                (
                    branch.id,
                    json.dumps({"event_id": event_id, "parent_id": parent.id}),
                    source_id,
                    now,
                ),
            )
            await conn.execute(
                "UPDATE dashboard_conversations SET updated_at=? WHERE id=?", (now, parent.id)
            )
            await conn.execute(
                "UPDATE conversations SET last_active_at=? WHERE id=?",
                (now, parent.conversation_id),
            )
