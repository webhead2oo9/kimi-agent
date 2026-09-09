"""Durable references and bounded reconciliation for task approval messages."""

from __future__ import annotations

import time
from typing import Any
from storage.db import Database


class TaskPreviewStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def remember(self, task_id: str, revision: int, channel_id: str, message_id: str) -> None:
        async with self.db.write_transaction() as conn:
            await conn.execute(
                "INSERT OR IGNORE INTO scheduled_task_previews(message_id,channel_id,task_id,revision,desired_state,next_run,close_pending) "
                "SELECT ?,?,t.id,r.revision,CASE WHEN r.approval_status='approved' THEN 'activated' "
                "WHEN r.approval_status='rejected' THEN 'denied' WHEN t.revision!=r.revision THEN 'superseded' "
                "ELSE 'pending' END,t.next_run,r.approval_status IN ('approved','rejected') "
                "FROM scheduled_tasks t JOIN scheduled_task_revisions r ON r.task_id=t.id "
                "WHERE t.id=? AND r.revision=?",
                (message_id, channel_id, task_id, revision),
            )

    async def updates(self, *, message_id: str | None = None) -> list[dict[str, Any]]:
        async with self.db.conn.execute(
            "SELECT p.*,r.definition_json,r.proposer_id,t.owner_id,t.guild_id FROM scheduled_task_previews p "
            "JOIN scheduled_tasks t ON t.id=p.task_id JOIN scheduled_task_revisions r "
            "ON r.task_id=p.task_id AND r.revision=p.revision WHERE "
            "(p.desired_state!=p.rendered_state OR p.close_pending=1) AND "
            "((? IS NOT NULL AND p.message_id=?) OR (? IS NULL AND p.attempts<5 AND p.retry_at<=?)) LIMIT 20",
            (message_id, message_id, message_id, time.time()),
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

    async def thread_in_use(self, guild_id: str, channel_id: str) -> bool:
        """Closing an approval surface must not disable an approved task's channels."""
        async with self.db.conn.execute(
            "SELECT 1 FROM scheduled_tasks t JOIN scheduled_task_revisions r "
            "ON r.task_id=t.id AND r.revision=t.active_revision WHERE t.guild_id=? AND "
            "(t.channel_id=? OR json_extract(r.definition_json,'$.log_channel')=? OR "
            "EXISTS(SELECT 1 FROM json_each(r.definition_json,'$.destinations') WHERE value=?)) LIMIT 1",
            (guild_id, channel_id, channel_id, channel_id),
        ) as cursor:
            return await cursor.fetchone() is not None

    async def rendered(self, message_id: str, state: str, *, closed: bool) -> None:
        async with self.db.write_transaction() as conn:
            await conn.execute(
                "UPDATE scheduled_task_previews SET rendered_state=?,close_pending=CASE WHEN ? THEN 0 ELSE close_pending END "
                "WHERE message_id=?",
                (state, closed, message_id),
            )

    async def failed(self, message_id: str, *, permanent: bool = False) -> None:
        async with self.db.write_transaction() as conn:
            await conn.execute(
                "UPDATE scheduled_task_previews SET attempts=CASE WHEN ? THEN 5 ELSE attempts+1 END, "
                "retry_at=?+MIN(1800,30*(1 << MIN(attempts,6))) WHERE message_id=?",
                (permanent, time.time(), message_id),
            )
