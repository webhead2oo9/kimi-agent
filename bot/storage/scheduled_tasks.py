"""Transactional task revisions, occurrence claims, and durable publication."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, cast

from storage.db import Database
from storage.task_types import (
    DeliveryRecord,
    DeliverySummary,
    DueTask,
    RunRecord,
    SavedTaskFile,
    TaskHistory,
    TaskRecord,
)


class ScheduledTaskStore:
    def __init__(self, database: Database) -> None:
        self.db = database

    async def get(self, task_id: str, *, active: bool = False) -> TaskRecord:
        revision = "active_revision" if active else "revision"
        async with self.db.conn.execute(
            f"SELECT t.*, r.definition_json, r.proposer_id, r.approval_status FROM scheduled_tasks t "
            f"JOIN scheduled_task_revisions r ON r.task_id=t.id AND r.revision=t.{revision} "
            "WHERE t.id=?",
            (task_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise ValueError("Task not found")
        result = dict(row)
        result["definition"] = json.loads(result.pop("definition_json"))
        result["state"] = json.loads(result.pop("state_json"))
        return cast(TaskRecord, result)

    async def wizard(
        self, owner_id: str, guild_id: str, key: str, *, instructions_only: bool = False
    ) -> dict[str, Any] | None:
        async with self.db.conn.execute(
            "SELECT w.* FROM scheduled_task_wizards w LEFT JOIN scheduled_tasks t ON t.id=w.task_id "
            "LEFT JOIN scheduled_task_revisions r ON r.task_id=t.id AND r.revision=t.revision "
            "WHERE w.owner_id=? AND w.guild_id=? AND w.context_key=? AND "
            "(?=0 OR w.task_id IS NULL OR w.context_key LIKE 'task-edit:%' OR "
            "(r.approval_status='pending' AND (t.active_revision IS NULL OR t.revision!=t.active_revision)))",
            (owner_id, guild_id, key, instructions_only),
        ) as cursor:
            row = await cursor.fetchone()
        return dict(row) if row is not None else None

    async def bind_wizard(
        self, owner_id: str, guild_id: str, key: str, task_id: str | None
    ) -> None:
        async with self.db.write_transaction() as conn:
            await conn.execute(
                "INSERT INTO scheduled_task_wizards(owner_id,guild_id,context_key,task_id) VALUES(?,?,?,?) "
                "ON CONFLICT(owner_id,guild_id,context_key) DO UPDATE SET task_id=excluded.task_id",
                (owner_id, guild_id, key, task_id),
            )

    async def cancel_wizard(self, owner_id: str, guild_id: str, key: str) -> None:
        async with self.db.write_transaction() as conn:
            await conn.execute(
                "DELETE FROM scheduled_task_wizards WHERE owner_id=? AND guild_id=? AND context_key=?",
                (owner_id, guild_id, key),
            )

    async def place_wizard(
        self, owner_id: str, guild_id: str, key: str, *, in_channel: bool
    ) -> None:
        async with self.db.write_transaction() as conn:
            await conn.execute(
                "UPDATE scheduled_task_wizards SET approval_in_channel=? "
                "WHERE owner_id=? AND guild_id=? AND context_key=?",
                (in_channel, owner_id, guild_id, key),
            )

    async def revision_approval(self, task_id: str, revision: int) -> tuple[str, str] | None:
        async with self.db.conn.execute(
            "SELECT proposer_id,approval_status FROM scheduled_task_revisions WHERE task_id=? AND revision=?",
            (task_id, revision),
        ) as cursor:
            row = await cursor.fetchone()
        return (str(row[0]), str(row[1])) if row is not None else None

    async def persistent_views(self) -> tuple[list[tuple[str, int]], list[str]]:
        async with self.db.conn.execute(
            "SELECT task_id,revision FROM scheduled_task_revisions"
        ) as cursor:
            revisions = [(str(row[0]), int(row[1])) for row in await cursor.fetchall()]
        async with self.db.conn.execute("SELECT id FROM scheduled_tasks") as cursor:
            tasks = [str(row[0]) for row in await cursor.fetchall()]
        return revisions, tasks

    async def owner_tasks(self, owner_id: str) -> list[str]:
        async with self.db.conn.execute(
            "SELECT id FROM scheduled_tasks WHERE owner_id=?", (owner_id,)
        ) as cursor:
            return [str(row[0]) for row in await cursor.fetchall()]

    async def clear_owner_wizards(self, owner_id: str) -> None:
        async with self.db.write_transaction() as conn:
            await conn.execute("DELETE FROM scheduled_task_wizards WHERE owner_id=?", (owner_id,))

    async def task_history(self, task_id: str) -> TaskHistory:
        async with self.db.conn.execute(
            "SELECT d.id,d.run_id,d.channel_id,d.status,d.message_id,d.error FROM "
            "scheduled_task_deliveries d JOIN scheduled_task_runs r ON r.id=d.run_id "
            "WHERE r.task_id=? ORDER BY d.id DESC LIMIT 100",
            (task_id,),
        ) as cursor:
            deliveries = [cast(DeliverySummary, dict(row)) for row in await cursor.fetchall()]
        return {"runs": await self.history(task_id), "deliveries": deliveries}

    async def save_files(
        self, run_id: str, files: list[tuple[str, str | None, bytes]]
    ) -> list[int]:
        ids: list[int] = []
        async with self.db.write_transaction() as conn:
            for filename, description, data in files:
                cursor = await conn.execute(
                    "INSERT INTO scheduled_task_files(run_id,filename,description,data) VALUES(?,?,?,?)",
                    (run_id, filename, description, data),
                )
                assert cursor.lastrowid is not None
                ids.append(cursor.lastrowid)
        return ids

    async def output_file(self, run_id: str, file_id: int) -> SavedTaskFile:
        async with self.db.conn.execute(
            "SELECT filename,description,data FROM scheduled_task_files WHERE id=? AND run_id=?",
            (file_id, run_id),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise ValueError("Saved task attachment is unavailable")
        return cast(SavedTaskFile, dict(row))

    async def list_tasks(
        self, guild_id: str, owner_id: str | None, *, limit: int = 100, offset: int = 0
    ) -> list[dict[str, Any]]:
        async with self.db.conn.execute(
            "SELECT t.id,t.owner_id,t.status,t.revision,t.active_revision,t.next_run,"
            "json_extract(r.definition_json,'$.name') AS name "
            "FROM scheduled_tasks t JOIN scheduled_task_revisions r ON r.task_id=t.id "
            "AND r.revision=t.revision WHERE t.guild_id=? AND (? IS NULL OR t.owner_id=?) "
            "ORDER BY t.rowid DESC LIMIT ? OFFSET ?",
            (guild_id, owner_id, owner_id, min(100, max(1, limit)), max(0, offset)),
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

    async def draft(
        self,
        *,
        task_id: str | None,
        guild_id: str,
        owner_id: str,
        channel_id: str,
        proposer_id: str,
        definition: dict[str, Any],
        expected_revision: int | None = None,
    ) -> str:
        now = time.time()
        async with self.db.immediate_write_transaction() as conn:
            if task_id is None:
                task_id = uuid.uuid4().hex
                await conn.execute(
                    "INSERT INTO scheduled_tasks(id,guild_id,owner_id,channel_id,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (task_id, guild_id, owner_id, channel_id, now, now),
                )
                revision = 1
            else:
                async with conn.execute(
                    "SELECT revision FROM scheduled_tasks WHERE id=? AND guild_id=?",
                    (task_id, guild_id),
                ) as cursor:
                    row = await cursor.fetchone()
                if row is None or row[0] != expected_revision:
                    raise ValueError("Task changed; reload it before editing")
                revision = int(row[0]) + 1
            await conn.execute(
                "INSERT INTO scheduled_task_revisions(task_id,revision,definition_json,proposer_id,created_at) VALUES(?,?,?,?,?)",
                (task_id, revision, json.dumps(definition), proposer_id, now),
            )
            await conn.execute(
                "UPDATE scheduled_tasks SET revision=?,updated_at=? WHERE id=?",
                (revision, now, task_id),
            )
            await conn.execute(
                "UPDATE scheduled_task_previews SET desired_state='superseded',attempts=0,retry_at=0 "
                "WHERE task_id=? AND revision<? AND desired_state='pending'",
                (task_id, revision),
            )
        return task_id

    async def activate(
        self,
        task_id: str,
        revision: int,
        proposer_id: str,
        next_run: float,
        *,
        reset_state: bool,
    ) -> None:
        async with self.db.immediate_write_transaction() as conn:
            cursor = await conn.execute(
                "UPDATE scheduled_tasks SET active_revision=revision,status='active',next_run=?,"
                "state_json=CASE WHEN ? THEN '{}' ELSE state_json END,"
                "state_generation=state_generation+?,updated_at=? "
                "WHERE id=? AND revision=? AND (active_revision IS NULL OR active_revision!=revision) "
                "AND EXISTS(SELECT 1 FROM scheduled_task_revisions "
                "WHERE task_id=? AND revision=? AND proposer_id=? AND approval_status='pending')",
                (
                    next_run,
                    reset_state,
                    int(reset_state),
                    time.time(),
                    task_id,
                    revision,
                    task_id,
                    revision,
                    proposer_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("This preview is stale or belongs to someone else")
            await conn.execute(
                "UPDATE scheduled_task_revisions SET approval_status='approved' WHERE task_id=? AND revision=?",
                (task_id, revision),
            )
            await conn.execute(
                "DELETE FROM scheduled_task_wizards WHERE task_id=? AND context_key LIKE 'task-edit:%'",
                (task_id,),
            )
            await conn.execute(
                "UPDATE scheduled_task_previews SET desired_state='activated',next_run=?,attempts=0,retry_at=0,close_pending=1 "
                "WHERE task_id=? AND revision=?",
                (next_run, task_id, revision),
            )

    async def reject(self, task_id: str, revision: int, proposer_id: str) -> None:
        async with self.db.immediate_write_transaction() as conn:
            cursor = await conn.execute(
                "UPDATE scheduled_task_revisions SET approval_status='rejected' WHERE task_id=? "
                "AND revision=? AND proposer_id=? AND approval_status='pending' AND EXISTS "
                "(SELECT 1 FROM scheduled_tasks WHERE id=? AND revision=?)",
                (task_id, revision, proposer_id, task_id, revision),
            )
            if cursor.rowcount != 1:
                raise ValueError("This revision has already been decided or replaced")
            await conn.execute(
                "DELETE FROM scheduled_task_wizards WHERE task_id=? AND context_key LIKE 'task-edit:%'",
                (task_id,),
            )
            await conn.execute(
                "UPDATE scheduled_tasks SET status='rejected' WHERE id=? AND active_revision IS NULL",
                (task_id,),
            )
            await conn.execute(
                "UPDATE scheduled_task_previews SET desired_state='denied',close_pending=1,attempts=0,retry_at=0 WHERE task_id=? AND revision=?",
                (task_id, revision),
            )

    async def set_status(
        self,
        task_id: str,
        status: str,
        *,
        next_run: float | None = None,
        answer: str | None = None,
    ) -> None:
        async with self.db.immediate_write_transaction() as conn:
            if answer is not None:
                async with conn.execute(
                    "SELECT state_json FROM scheduled_tasks WHERE id=? AND NOT EXISTS "
                    "(SELECT 1 FROM scheduled_task_runs WHERE task_id=? AND status IN "
                    "('running','delivery'))",
                    (task_id, task_id),
                ) as cursor:
                    row = await cursor.fetchone()
                if row is None:
                    raise ValueError("Pause the active run before supplying input")
                state = {**json.loads(row[0]), "human_input": answer}
                if len(json.dumps(state)) > 64000:
                    raise ValueError("Saved state plus input exceeds 64,000 characters")
                await conn.execute(
                    "UPDATE scheduled_tasks SET state_json=?,state_generation=state_generation+1 "
                    "WHERE id=?",
                    (json.dumps(state), task_id),
                )
            await conn.execute(
                "UPDATE scheduled_tasks SET status=?,next_run=COALESCE(?,next_run),updated_at=? "
                "WHERE id=?",
                (status, next_run, time.time(), task_id),
            )
            if status != "active":
                await conn.execute(
                    "UPDATE scheduled_task_runs SET status='cancelled',finished_at=? "
                    "WHERE task_id=? AND status IN ('running','delivery')",
                    (time.time(), task_id),
                )
                await conn.execute(
                    "UPDATE scheduled_task_deliveries SET status='cancelled' WHERE status='pending' "
                    "AND run_id IN (SELECT id FROM scheduled_task_runs WHERE task_id=?)",
                    (task_id,),
                )

    async def delete(self, task_id: str) -> None:
        async with self.db.write_transaction() as conn:
            await conn.execute("DELETE FROM scheduled_tasks WHERE id=?", (task_id,))

    async def attention(self, task_id: str, run_id: str) -> None:
        """Pause failed delivery without discarding saved output or comparison state."""
        async with self.db.write_transaction() as conn:
            await conn.execute(
                "UPDATE scheduled_tasks SET status='attention' WHERE id=?", (task_id,)
            )
            await conn.execute(
                "UPDATE scheduled_task_runs SET status='delivery_failed' WHERE id=? AND status='delivery'",
                (run_id,),
            )

    async def _retry_run(self, conn: Any, task_id: str) -> str:
        async with conn.execute(
            "SELECT id,state_generation FROM scheduled_task_runs "
            "WHERE task_id=? AND status='delivery_failed' ORDER BY rowid DESC LIMIT 1",
            (task_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise ValueError("No failed delivery to retry")
        run_id = str(row[0])
        # Claim insertion order is reliable even when wall-clock timestamps tie or regress.
        async with conn.execute(
            "SELECT 1 FROM scheduled_task_runs WHERE task_id=? AND rowid>"
            "(SELECT rowid FROM scheduled_task_runs WHERE id=?) LIMIT 1",
            (task_id, run_id),
        ) as cursor:
            if await cursor.fetchone():
                raise ValueError("A newer run has superseded this failed delivery")
        async with conn.execute(
            "SELECT 1 FROM scheduled_tasks WHERE id=? AND state_generation=?",
            (task_id, row[1]),
        ) as cursor:
            if await cursor.fetchone() is None:
                raise ValueError("Task state has changed since this failed delivery")
        async with conn.execute(
            "SELECT 1 FROM scheduled_task_deliveries WHERE run_id=? AND status='uncertain'",
            (run_id,),
        ) as cursor:
            if await cursor.fetchone():
                raise ValueError(
                    "A send has an uncertain outcome. Inspect the destination before starting a new run; it cannot be retried automatically."
                )
        async with conn.execute(
            "SELECT 1 FROM scheduled_task_deliveries WHERE run_id=? AND is_log=0 AND status='cancelled'",
            (run_id,),
        ) as cursor:
            if await cursor.fetchone():
                raise ValueError("This delivery was cancelled; start a new run instead")
        return run_id

    async def can_retry_delivery(self, task_id: str) -> bool:
        try:
            await self._retry_run(self.db.conn, task_id)
        except ValueError:
            return False
        return True

    async def retry_delivery(self, task_id: str) -> None:
        async with self.db.immediate_write_transaction() as conn:
            run_id = await self._retry_run(conn, task_id)
            await conn.execute(
                "UPDATE scheduled_task_deliveries SET status='pending',retry_at=0 WHERE run_id=? "
                "AND status='failed'",
                (run_id,),
            )
            await conn.execute(
                "UPDATE scheduled_task_runs SET status='delivery' WHERE id=?", (run_id,)
            )
            await conn.execute("UPDATE scheduled_tasks SET status='active' WHERE id=?", (task_id,))

    async def begin_delivery(self, delivery_id: int, token: str) -> bool:
        async with self.db.immediate_write_transaction() as conn:
            cursor = await conn.execute(
                "UPDATE scheduled_task_deliveries SET status='sending',attempts=attempts+1 WHERE id=? "
                "AND status='pending' AND EXISTS(SELECT 1 FROM scheduled_task_runner WHERE token=? AND expires_at>?) "
                "AND NOT EXISTS(SELECT 1 FROM scheduled_task_deliveries earlier "
                "WHERE earlier.run_id=scheduled_task_deliveries.run_id "
                "AND earlier.is_log=scheduled_task_deliveries.is_log "
                "AND earlier.id<scheduled_task_deliveries.id AND earlier.status IN ('pending','sending')) "
                "AND EXISTS(SELECT 1 FROM scheduled_task_runs r JOIN scheduled_tasks t ON t.id=r.task_id "
                "WHERE r.id=scheduled_task_deliveries.run_id AND (scheduled_task_deliveries.is_log=1 OR "
                "(t.status='active' AND r.status='delivery')))",
                (delivery_id, token, time.time()),
            )
            return cursor.rowcount == 1

    async def delete_owner(self, owner_id: str) -> None:
        async with self.db.write_transaction() as conn:
            await conn.execute("DELETE FROM scheduled_tasks WHERE owner_id=?", (owner_id,))

    async def lease(self, token: str, now: float) -> bool:
        async with self.db.immediate_write_transaction() as conn:
            cursor = await conn.execute(
                "UPDATE scheduled_task_runner SET token=?,expires_at=? "
                "WHERE id=1 AND (token=? OR expires_at<?)",
                (token, now + 60, token, now),
            )
            return cursor.rowcount == 1

    async def release(self, token: str) -> None:
        async with self.db.write_transaction() as conn:
            await conn.execute(
                "UPDATE scheduled_task_runner SET expires_at=0 WHERE token=?",
                (token,),
            )

    async def recover(self) -> None:
        async with self.db.write_transaction() as conn:
            await conn.execute(
                "UPDATE scheduled_tasks SET status='attention' WHERE id IN "
                "(SELECT task_id FROM scheduled_task_runs WHERE status='running' OR id IN "
                "(SELECT run_id FROM scheduled_task_deliveries WHERE status='sending' AND is_log=0))",
            )
            await conn.execute(
                "UPDATE scheduled_task_runs SET status='interrupted',detail='Interrupted; inspect "
                "actions before retrying',finished_at=? WHERE status='running' OR id IN "
                "(SELECT run_id FROM scheduled_task_deliveries WHERE status='sending' AND is_log=0)",
                (time.time(),),
            )
            await conn.execute(
                "UPDATE scheduled_task_deliveries SET status='uncertain',error='Send interrupted' "
                "WHERE status='sending'",
            )

    async def due(self, now: float) -> list[str]:
        return [item["id"] for item in await self.due_candidates(now)]

    async def due_candidates(self, now: float) -> list[DueTask]:
        """Oldest eligible task per owner and mode; no global backlog truncation."""
        async with self.db.conn.execute(
            "WITH candidates AS (SELECT t.id,t.owner_id,t.next_run,"
            "COALESCE(json_extract(v.definition_json,'$.execution'),'llm') AS execution,"
            "ROW_NUMBER() OVER (PARTITION BY t.owner_id,"
            "COALESCE(json_extract(v.definition_json,'$.execution'),'llm') "
            "ORDER BY t.next_run,t.created_at,t.id) AS position "
            "FROM scheduled_tasks t JOIN scheduled_task_revisions v ON v.task_id=t.id "
            "AND v.revision=t.active_revision WHERE t.status='active' AND t.next_run<=? "
            "AND NOT EXISTS(SELECT 1 FROM scheduled_task_runs r WHERE r.task_id=t.id "
            "AND r.status IN ('running','delivery')) "
            "AND NOT EXISTS(SELECT 1 FROM scheduled_task_runs r JOIN scheduled_tasks owner_task "
            "ON owner_task.id=r.task_id WHERE owner_task.owner_id=t.owner_id AND r.status='running')) "
            "SELECT id,owner_id,next_run,execution FROM candidates WHERE position=1 "
            "ORDER BY next_run,id",
            (now,),
        ) as cursor:
            return [cast(DueTask, dict(row)) for row in await cursor.fetchall()]

    async def claim(self, task: TaskRecord, next_run: float | None) -> str | None:
        run_id = uuid.uuid4().hex
        async with self.db.immediate_write_transaction() as conn:
            cursor = await conn.execute(
                "UPDATE scheduled_tasks SET next_run=? WHERE id=? AND status='active' "
                "AND active_revision=? AND next_run=? AND NOT EXISTS "
                "(SELECT 1 FROM scheduled_task_runs WHERE task_id=? AND status IN ('running','delivery')) "
                "AND NOT EXISTS(SELECT 1 FROM scheduled_task_runs r JOIN scheduled_tasks t ON t.id=r.task_id "
                "WHERE t.owner_id=? AND r.status='running')",
                (
                    next_run,
                    task["id"],
                    task["active_revision"],
                    task["next_run"],
                    task["id"],
                    task["owner_id"],
                ),
            )
            if cursor.rowcount != 1:
                return None
            await conn.execute(
                "INSERT INTO scheduled_task_runs(id,task_id,revision,state_generation,scheduled_for,"
                "status,created_at) VALUES(?,?,?,?,?,'running',?)",
                (
                    run_id,
                    task["id"],
                    task["active_revision"],
                    task["state_generation"],
                    task["next_run"],
                    time.time(),
                ),
            )
        return run_id

    async def live(self, task_id: str, run_id: str, token: str) -> bool:
        async with self.db.conn.execute(
            "SELECT 1 FROM scheduled_tasks t JOIN scheduled_task_runs r ON r.task_id=t.id "
            "WHERE t.id=? AND r.id=? AND t.status='active' AND r.status IN ('running','delivery') "
            "AND EXISTS(SELECT 1 FROM scheduled_task_runner WHERE token=? AND expires_at>?)",
            (task_id, run_id, token, time.time()),
        ) as cursor:
            return await cursor.fetchone() is not None

    async def finish(
        self,
        run_id: str,
        status: str,
        detail: str,
        state: dict[str, Any],
        deliveries: list[dict[str, Any]],
    ) -> None:
        async with self.db.immediate_write_transaction() as conn:
            cursor = await conn.execute(
                "UPDATE scheduled_task_runs SET status=?,detail=?,proposed_state=?,finished_at=? "
                "WHERE id=? AND status='running'",
                (status, detail[:4000], json.dumps(state), time.time(), run_id),
            )
            if cursor.rowcount != 1:
                return
            for delivery in deliveries:
                await conn.execute(
                    "INSERT INTO scheduled_task_deliveries(run_id,channel_id,payload_json,is_log) "
                    "VALUES(?,?,?,?)",
                    (
                        run_id,
                        delivery["channel_id"],
                        json.dumps(delivery),
                        delivery.get("is_log", False),
                    ),
                )
            if status in {"completed", "no_change"}:
                await self._commit_state(conn, run_id)
            elif status in {"needs_input", "failed"}:
                await conn.execute(
                    "UPDATE scheduled_tasks SET status='attention' WHERE id="
                    "(SELECT task_id FROM scheduled_task_runs WHERE id=?)",
                    (run_id,),
                )

    @staticmethod
    async def _commit_state(conn: Any, run_id: str) -> None:
        await conn.execute(
            "UPDATE scheduled_tasks SET state_json=(SELECT proposed_state FROM scheduled_task_runs "
            "WHERE id=?),state_generation=state_generation+1,"
            "status=CASE WHEN next_run IS NULL THEN 'completed' ELSE status END "
            "WHERE status='active' AND EXISTS (SELECT 1 FROM scheduled_task_runs r WHERE r.id=? "
            "AND r.task_id=scheduled_tasks.id AND r.state_generation=scheduled_tasks.state_generation)",
            (run_id, run_id),
        )

    async def delivery_runs(self, limit: int, excluded: set[str]) -> list[str]:
        """Choose runs, rather than chunks, so a long post cannot occupy the pool."""
        async with self.db.conn.execute(
            "SELECT d.run_id,MIN(d.id) AS first_id FROM scheduled_task_deliveries d "
            "JOIN scheduled_task_runs r ON r.id=d.run_id "
            "JOIN scheduled_tasks t ON t.id=r.task_id WHERE d.status='pending' AND d.retry_at<=? "
            "AND ((d.is_log=1 AND r.status!='delivery') OR "
            "(r.status='delivery' AND t.status='active' AND d.is_log=0)) "
            "AND NOT EXISTS(SELECT 1 FROM scheduled_task_deliveries earlier "
            "WHERE earlier.run_id=d.run_id AND earlier.is_log=d.is_log AND earlier.id<d.id "
            "AND earlier.status IN ('pending','sending')) GROUP BY d.run_id ORDER BY first_id",
            (time.time(),),
        ) as cursor:
            return [str(row[0]) for row in await cursor.fetchall() if row[0] not in excluded][
                :limit
            ]

    async def deliveries(self, *, run_id: str | None = None) -> list[DeliveryRecord]:
        async with self.db.conn.execute(
            "SELECT d.*,r.task_id,r.revision,r.status AS run_status,r.detail AS run_detail FROM scheduled_task_deliveries d "
            "JOIN scheduled_task_runs r ON r.id=d.run_id "
            "JOIN scheduled_tasks t ON t.id=r.task_id WHERE d.status='pending' AND d.retry_at<=? "
            "AND ((d.is_log=1 AND r.status!='delivery') OR (r.status='delivery' AND t.status='active' AND d.is_log=0)) "
            "AND (? IS NULL OR (d.run_id=? AND NOT EXISTS(SELECT 1 FROM scheduled_task_deliveries earlier "
            "WHERE earlier.run_id=d.run_id AND earlier.is_log=d.is_log AND earlier.id<d.id "
            "AND earlier.status IN ('pending','sending')))) ORDER BY d.id LIMIT 20",
            (time.time(), run_id, run_id),
        ) as cursor:
            return [cast(DeliveryRecord, dict(row)) for row in await cursor.fetchall()]

    async def delivery_status(
        self,
        delivery_id: int,
        status: str,
        *,
        message_id: str = "",
        error: str = "",
    ) -> None:
        async with self.db.immediate_write_transaction() as conn:
            await conn.execute(
                "UPDATE scheduled_task_deliveries SET status=?,message_id=?,error=?,"
                "attempts=attempts+?,retry_at=? WHERE id=?",
                (
                    status,
                    message_id,
                    error[:500],
                    int(status == "sending"),
                    time.time() + 60,
                    delivery_id,
                ),
            )
            if status == "sent":
                async with conn.execute(
                    "SELECT run_id FROM scheduled_task_deliveries WHERE id=?",
                    (delivery_id,),
                ) as cursor:
                    row = await cursor.fetchone()
                if row:
                    run_id = row[0]
                    cursor = await conn.execute(
                        "UPDATE scheduled_task_runs SET status='completed',finished_at=? WHERE id=? "
                        "AND status='delivery' AND NOT EXISTS(SELECT 1 FROM scheduled_task_deliveries "
                        "WHERE run_id=? AND is_log=0 AND status!='sent')",
                        (time.time(), run_id, run_id),
                    )
                    if cursor.rowcount:
                        await self._commit_state(conn, run_id)

    async def publication_context(
        self, guild_id: str, conversation_key: str
    ) -> dict[str, Any] | None:
        """Return only public origin metadata for a verified published conversation."""
        async with self.db.conn.execute(
            "SELECT r.task_id,r.id AS run_id,r.revision,"
            "json_extract(v.definition_json,'$.name') AS task_name,m.source_created_at AS published_at "
            "FROM conversations c JOIN messages m ON m.conversation_id=c.id "
            "AND m.discord_message_id=c.root_discord_message_id AND m.role='assistant' "
            "JOIN scheduled_task_deliveries d ON d.message_id=m.discord_message_id "
            "AND d.channel_id=c.channel_id AND d.status='sent' AND d.is_log=0 "
            "JOIN scheduled_task_runs r ON r.id=d.run_id JOIN scheduled_tasks t "
            "ON t.id=r.task_id AND t.guild_id=c.guild_id JOIN scheduled_task_revisions v "
            "ON v.task_id=r.task_id AND v.revision=r.revision "
            "WHERE c.key=? AND c.guild_id=? AND c.access_scope='channel_shared' LIMIT 1",
            (conversation_key, guild_id),
        ) as cursor:
            row = await cursor.fetchone()
        return dict(row) if row is not None else None

    async def history(self, task_id: str) -> list[RunRecord]:
        async with self.db.conn.execute(
            "SELECT id,revision,scheduled_for,status,detail,created_at,finished_at "
            "FROM scheduled_task_runs WHERE task_id=? ORDER BY rowid DESC LIMIT 30",
            (task_id,),
        ) as cursor:
            return [cast(RunRecord, dict(row)) for row in await cursor.fetchall()]

    async def prune(self) -> None:
        async with self.db.write_transaction() as conn:
            await conn.execute(
                "DELETE FROM scheduled_task_runs WHERE finished_at<? AND status NOT IN ('running',"
                "'delivery') AND NOT EXISTS(SELECT 1 FROM scheduled_task_deliveries d WHERE "
                "d.run_id=scheduled_task_runs.id AND d.status IN ('pending','sending'))",
                (time.time() - 30 * 86400,),
            )
