"""Durable, independently acknowledged module notifications; never dispatch module code."""

from __future__ import annotations

import time
import uuid
from typing import Any, TypedDict, cast

import aiosqlite

from storage.db import Database

RESULT_RETENTION_SECONDS = 30 * 86400
RESULT_LEASE_SECONDS = 60


class ResultNotification(TypedDict):
    id: str
    run_id: str
    module: str
    name: str
    guild_id: str
    published_at: float
    expires_at: float
    status: str
    attempts: int
    lease_token: str
    task_id: str
    revision: int
    owner_id: str
    channel_id: str


async def enqueue_results(conn: aiosqlite.Connection, run_id: str, now: float) -> None:
    """Called in the transaction that confirms the last required Discord post."""
    await conn.execute(
        "INSERT OR IGNORE INTO scheduled_result_notifications "
        "(id,run_id,module,name,guild_id,published_at,expires_at) "
        "SELECT lower(hex(randomblob(16))),r.id,s.module,s.name,s.guild_id,?,? "
        "FROM scheduled_task_runs r JOIN scheduled_tasks t ON t.id=r.task_id "
        "JOIN scheduled_result_subscriptions s ON s.guild_id=t.guild_id "
        "WHERE r.id=? AND r.status='completed' AND EXISTS "
        "(SELECT 1 FROM scheduled_task_deliveries d WHERE d.run_id=r.id AND d.is_log=0 "
        "AND d.status='sent' AND d.message_id!='')",
        (now, now + RESULT_RETENTION_SECONDS, run_id),
    )


class TaskResultStore:
    def __init__(self, database: Database) -> None:
        self.db = database

    async def subscribe(self, module: str, name: str, guild_id: int) -> None:
        async with self.db.write_transaction() as conn:
            await conn.execute(
                "INSERT OR IGNORE INTO scheduled_result_subscriptions(module,name,guild_id) "
                "VALUES(?,?,?)",
                (module, name, str(guild_id)),
            )

    async def unsubscribe(self, module: str, name: str, guild_id: int) -> None:
        async with self.db.write_transaction() as conn:
            await conn.execute(
                "DELETE FROM scheduled_result_subscriptions WHERE module=? AND name=? AND guild_id=?",
                (module, name, str(guild_id)),
            )

    async def claim(self, *, limit: int = 4) -> list[ResultNotification]:
        now = time.time()
        async with self.db.immediate_write_transaction() as conn:
            async with conn.execute(
                "SELECT n.*,r.task_id,r.revision,t.owner_id,t.channel_id "
                "FROM scheduled_result_notifications n JOIN scheduled_task_runs r ON r.id=n.run_id "
                "JOIN scheduled_tasks t ON t.id=r.task_id WHERE n.status!='acknowledged' "
                "AND n.expires_at>? AND n.retry_at<=? AND n.lease_until<=? "
                "ORDER BY n.retry_at,n.published_at,n.id LIMIT ?",
                (now, now, now, limit),
            ) as cursor:
                rows = [cast(ResultNotification, dict(row)) for row in await cursor.fetchall()]
            for row in rows:
                token = uuid.uuid4().hex
                await conn.execute(
                    "UPDATE scheduled_result_notifications SET status='delivering',attempts=attempts+1,"
                    "lease_token=?,lease_until=? WHERE id=?",
                    (token, now + RESULT_LEASE_SECONDS, row["id"]),
                )
                row["lease_token"], row["status"] = token, "delivering"
                row["attempts"] += 1
        return rows

    async def settle(self, row: ResultNotification, status: str, detail: str = "") -> None:
        now = time.time()
        delay = 60 if status == "blocked" else min(3600, 30 * 2 ** min(row["attempts"] - 1, 7))
        async with self.db.write_transaction() as conn:
            await conn.execute(
                "UPDATE scheduled_result_notifications SET status=?,detail=?,lease_token='',"
                "lease_until=0,retry_at=?,acknowledged_at=? WHERE id=? AND lease_token=? "
                "AND lease_until>? AND expires_at>?",
                (
                    status,
                    detail[:300],
                    now + delay,
                    now if status == "acknowledged" else None,
                    row["id"],
                    row["lease_token"],
                    now,
                    now,
                ),
            )

    async def live(self, row: ResultNotification) -> bool:
        async with self.db.conn.execute(
            "SELECT 1 FROM scheduled_result_notifications WHERE id=? AND lease_token=? "
            "AND status='delivering' AND lease_until>? AND expires_at>?",
            (row["id"], row["lease_token"], time.time(), time.time()),
        ) as cursor:
            return await cursor.fetchone() is not None

    async def messages(self, run_id: str) -> list[dict[str, Any]]:
        async with self.db.conn.execute(
            "SELECT channel_id,message_id,payload_json FROM scheduled_task_deliveries "
            "WHERE run_id=? AND is_log=0 AND status='sent' ORDER BY id",
            (run_id,),
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

    async def attachments(self, run_id: str) -> dict[str, dict[str, Any]]:
        async with self.db.conn.execute(
            "SELECT id,filename,description,length(data) AS size_bytes FROM scheduled_task_files "
            "WHERE run_id=?",
            (run_id,),
        ) as cursor:
            return {str(row["id"]): dict(row) for row in await cursor.fetchall()}

    async def read_file(self, row: ResultNotification, file_id: str, max_bytes: int) -> bytes:
        # Filter both ownership and length in SQLite before materializing the blob.
        async with self.db.conn.execute(
            "SELECT f.data FROM scheduled_task_files f JOIN scheduled_result_notifications n "
            "ON n.run_id=f.run_id WHERE n.id=? AND n.lease_token=? AND n.status='delivering' "
            "AND n.lease_until>? AND n.expires_at>? AND CAST(f.id AS TEXT)=? AND length(f.data)<=? "
            "AND EXISTS (SELECT 1 FROM scheduled_task_deliveries d, "
            "json_each(d.payload_json,'$.file_ids') a WHERE d.run_id=n.run_id AND d.is_log=0 "
            "AND d.status='sent' AND CAST(a.value AS TEXT)=CAST(f.id AS TEXT))",
            (row["id"], row["lease_token"], time.time(), time.time(), file_id, max_bytes),
        ) as cursor:
            saved = await cursor.fetchone()
        if saved is None:
            raise ValueError("Published attachment unavailable or larger than the read limit")
        return bytes(saved[0])

    async def prune(self) -> None:
        async with self.db.write_transaction() as conn:
            await conn.execute(
                "DELETE FROM scheduled_result_notifications WHERE expires_at<=?", (time.time(),)
            )

    async def history(self, task_id: str) -> list[dict[str, Any]]:
        async with self.db.conn.execute(
            "SELECT n.id,n.run_id,n.module,n.name,n.guild_id,n.status,n.attempts,n.detail,"
            "n.retry_at,n.expires_at,n.acknowledged_at FROM scheduled_result_notifications n "
            "JOIN scheduled_task_runs r ON r.id=n.run_id WHERE r.task_id=? "
            "AND n.expires_at>? ORDER BY n.published_at DESC,n.id LIMIT 100",
            (task_id, time.time()),
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

    async def health_counts(self) -> dict[str, dict[str, int]]:
        async with self.db.conn.execute(
            "SELECT module,status,COUNT(*) AS count FROM scheduled_result_notifications "
            "WHERE expires_at>? GROUP BY module,status",
            (time.time(),),
        ) as cursor:
            counts: dict[str, dict[str, int]] = {}
            for row in await cursor.fetchall():
                counts.setdefault(row["module"], {})[row["status"]] = row["count"]
            return counts
