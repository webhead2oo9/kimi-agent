from __future__ import annotations

import json
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import uuid4

from storage.db import Database


class CodingTaskStatus(StrEnum):
    QUEUED = "queued"
    RECOVERING = "recovering"
    RUNNING = "running"
    WAITING_FOR_JOB = "waiting_for_job"
    WAITING_FOR_INPUT = "waiting_for_input"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


ACTIVE_TASK_STATUSES = frozenset(
    {
        CodingTaskStatus.QUEUED,
        CodingTaskStatus.RECOVERING,
        CodingTaskStatus.RUNNING,
        CodingTaskStatus.WAITING_FOR_JOB,
        CodingTaskStatus.WAITING_FOR_INPUT,
        CodingTaskStatus.CANCELLING,
    }
)
TERMINAL_TASK_STATUSES = frozenset(set(CodingTaskStatus) - set(ACTIVE_TASK_STATUSES))

DELIVERY_MAX_ATTEMPTS = 10
DELIVERY_MAX_AGE_SECONDS = 24 * 60 * 60
DELIVERY_RETRY_DELAYS_SECONDS = (10, 30, 120, 600, 1_800, 3_600)


class CodingJobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    INTERRUPTED = "interrupted"
    UNSAFE = "unsafe"


ACTIVE_JOB_STATUSES = frozenset(
    {CodingJobStatus.QUEUED, CodingJobStatus.RUNNING, CodingJobStatus.UNSAFE}
)


class CodingTaskQueueFull(RuntimeError):
    def __init__(self, scope: str) -> None:
        super().__init__(f"coding task {scope} queue is full")
        self.scope = scope


def _json_dict(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_list(value: str) -> list[Any]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


@dataclass(frozen=True, slots=True)
class CodingTask:
    id: str
    conversation_id: int | None
    root_key: str
    workspace_key: str
    user_id: str
    user_name: str
    guild_id: str | None
    channel_id: str
    thread_id: str | None
    handoff_pending: bool
    trigger_discord_message_id: str
    objective: str
    acceptance_criteria: list[str]
    context_text: str
    display_summary: str
    context_messages: list[dict[str, str]]
    input_files: list[dict[str, str]]
    status: CodingTaskStatus
    plan: list[dict[str, str]]
    milestone: str
    checkpoint: dict[str, Any]
    result_text: str
    error_text: str
    cancel_requested: bool
    status_discord_message_id: str | None
    final_discord_message_id: str | None
    delivery_state: str
    created_at: float
    updated_at: float
    started_at: float | None
    finished_at: float | None
    deadline_at: float
    heartbeat_at: float


@dataclass(frozen=True, slots=True)
class CodingTaskEvent:
    id: int
    task_id: str
    kind: str
    payload: dict[str, Any]
    created_at: float


@dataclass(frozen=True, slots=True)
class CodingCommandJob:
    id: str
    task_id: str
    status: CodingJobStatus
    request: dict[str, Any]
    unit_name: str | None
    stdout: str
    stderr: str
    exit_code: int | None
    timed_out: bool
    created_at: float
    updated_at: float
    started_at: float | None
    finished_at: float | None


class CodingTaskStore:
    """Durable coding-task state and append-only agent journal."""

    def __init__(self, database: Database) -> None:
        self._db = database

    async def create_task(
        self,
        *,
        conversation_id: int | None,
        root_key: str,
        workspace_key: str,
        user_id: str,
        user_name: str,
        guild_id: str | None,
        channel_id: str,
        thread_id: str | None,
        handoff_pending: bool = False,
        trigger_discord_message_id: str,
        objective: str,
        acceptance_criteria: list[str],
        context_text: str,
        display_summary: str = "",
        context_messages: list[dict[str, str]] | None = None,
        input_files: list[dict[str, str]] | None = None,
        max_seconds: float,
        initial_checkpoint: dict[str, Any] | None = None,
        max_queued_per_user: int | None = None,
        max_queued_per_workspace: int | None = None,
    ) -> CodingTask:
        now = time.time()
        task_id = uuid4().hex
        async with self._db.write_transaction() as conn:
            if max_queued_per_user is not None or max_queued_per_workspace is not None:
                async with conn.execute(
                    """
                    SELECT
                        SUM(CASE WHEN user_id = ? AND status = 'queued' THEN 1 ELSE 0 END),
                        SUM(CASE WHEN workspace_key = ? AND status = 'queued' THEN 1 ELSE 0 END)
                    FROM coding_tasks
                    """,
                    (user_id, workspace_key),
                ) as cursor:
                    row = await cursor.fetchone()
                assert row is not None
                if max_queued_per_user is not None and int(row[0] or 0) >= max_queued_per_user:
                    raise CodingTaskQueueFull("user")
                if (
                    max_queued_per_workspace is not None
                    and int(row[1] or 0) >= max_queued_per_workspace
                ):
                    raise CodingTaskQueueFull("workspace")
            await conn.execute(
                """
                INSERT INTO coding_tasks (
                    id, conversation_id, root_key, workspace_key, user_id, user_name,
                    guild_id, channel_id, thread_id, handoff_pending,
                    trigger_discord_message_id,
                    objective, acceptance_criteria_json, context_text,
                    display_summary, context_messages_json, input_files_json, status,
                    checkpoint_json, created_at, updated_at, deadline_at, heartbeat_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    conversation_id,
                    root_key,
                    workspace_key,
                    user_id,
                    user_name,
                    guild_id,
                    channel_id,
                    thread_id,
                    int(handoff_pending),
                    trigger_discord_message_id,
                    objective,
                    json.dumps(acceptance_criteria),
                    context_text,
                    display_summary,
                    json.dumps(context_messages or []),
                    json.dumps(input_files or []),
                    json.dumps(initial_checkpoint or {}),
                    now,
                    now,
                    now + max_seconds,
                    now,
                ),
            )
            await self._append_event_conn(conn, task_id, "created", {}, now)
        task = await self.get_task(task_id)
        assert task is not None
        return task

    async def get_task(self, task_id: str) -> CodingTask | None:
        async with self._db.conn.execute(
            "SELECT * FROM coding_tasks WHERE id = ?", (task_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return self._task_from_row(row) if row is not None else None

    async def list_tasks_by_id_prefix(self, task_id_prefix: str) -> list[CodingTask]:
        """Return tasks matching a validated UUID-hex prefix, newest first."""
        async with self._db.conn.execute(
            "SELECT * FROM coding_tasks WHERE id LIKE ? ORDER BY created_at DESC",
            (f"{task_id_prefix}%",),
        ) as cursor:
            rows = await cursor.fetchall()
        return [self._task_from_row(row) for row in rows]

    async def list_active(
        self,
        *,
        user_id: str | None = None,
        root_key: str | None = None,
        channel_id: str | None = None,
    ) -> list[CodingTask]:
        clauses = [f"status IN ({','.join('?' for _ in ACTIVE_TASK_STATUSES)})"]
        params: list[Any] = [status.value for status in ACTIVE_TASK_STATUSES]
        for column, value in (
            ("user_id", user_id),
            ("root_key", root_key),
            ("channel_id", channel_id),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        async with self._db.conn.execute(
            f"SELECT * FROM coding_tasks WHERE {' AND '.join(clauses)} ORDER BY created_at DESC",
            params,
        ) as cursor:
            rows = await cursor.fetchall()
        return [self._task_from_row(row) for row in rows]

    async def list_pending_delivery(self, *, now: float | None = None) -> list[CodingTask]:
        due_at = time.time() if now is None else now
        async with self._db.conn.execute(
            """
            SELECT * FROM coding_tasks
            WHERE status IN ('completed','failed','cancelled','timed_out')
              AND final_discord_message_id IS NULL
              AND delivery_state = 'final_pending'
            ORDER BY finished_at
            """
        ) as cursor:
            rows = await cursor.fetchall()
        tasks = [self._task_from_row(row) for row in rows]
        return [task for task in tasks if self._delivery_retry_due(task, due_at)]

    async def list_handoff_pending(self) -> list[CodingTask]:
        async with self._db.conn.execute(
            """
            SELECT * FROM coding_tasks
            WHERE status = 'queued' AND cancel_requested = 0 AND handoff_pending = 1
            ORDER BY created_at
            """
        ) as cursor:
            rows = await cursor.fetchall()
        return [self._task_from_row(row) for row in rows]

    async def queued_counts(self, *, user_id: str, workspace_key: str) -> tuple[int, int]:
        async with self._db.conn.execute(
            """
            SELECT
                SUM(CASE WHEN user_id = ? AND status = 'queued' THEN 1 ELSE 0 END),
                SUM(CASE WHEN workspace_key = ? AND status = 'queued' THEN 1 ELSE 0 END)
            FROM coding_tasks
            """,
            (user_id, workspace_key),
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        return int(row[0] or 0), int(row[1] or 0)

    async def claim_next(self) -> CodingTask | None:
        """Atomically claim the oldest task whose workspace has no active writer."""

        now = time.time()
        async with self._db.write_transaction() as conn:
            async with conn.execute(
                """
                SELECT q.id
                FROM coding_tasks q
                WHERE q.status = 'queued'
                  AND q.cancel_requested = 0
                  AND q.handoff_pending = 0
                  AND NOT EXISTS (
                      SELECT 1 FROM coding_tasks earlier
                      WHERE earlier.workspace_key = q.workspace_key
                        AND earlier.status = 'queued'
                        AND earlier.cancel_requested = 0
                        AND earlier.created_at < q.created_at
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM coding_tasks a
                      WHERE a.workspace_key = q.workspace_key
                        AND a.id != q.id
                        AND a.status IN (
                            'recovering','running','waiting_for_job',
                            'cancelling'
                        )
                  )
                ORDER BY q.created_at
                LIMIT 1
                """
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                return None
            task_id = str(row[0])
            changed = await conn.execute(
                """
                UPDATE coding_tasks
                SET status = 'running', started_at = COALESCE(started_at, ?),
                    updated_at = ?, heartbeat_at = ?
                WHERE id = ? AND status = 'queued' AND cancel_requested = 0
                  AND handoff_pending = 0
                """,
                (now, now, now, task_id),
            )
            if changed.rowcount != 1:
                return None
            await self._append_event_conn(conn, task_id, "started", {}, now)
        return await self.get_task(task_id)

    async def release_claim(self, task_id: str) -> bool:
        """Put a just-claimed task back in the queue.

        For the window between claim_next() and worker registration where the
        scheduler decides the claim must not stand (for example the block check
        raised): without this the row stays 'running' with no worker until the
        next restart, and its workspace admits no other task.
        """

        now = time.time()
        async with self._db.write_transaction() as conn:
            changed = await conn.execute(
                """
                UPDATE coding_tasks
                SET status = 'queued', updated_at = ?, heartbeat_at = ?
                WHERE id = ? AND status = 'running'
                """,
                (now, now, task_id),
            )
            if changed.rowcount != 1:
                # The row moved (a cancel landed, or privacy deletion removed
                # it); recording a release that did not happen would falsify
                # the durable event log.
                return False
            await self._append_event_conn(conn, task_id, "claim_released", {}, now)
        return True

    async def bind_handoff_target(
        self,
        task_id: str,
        *,
        channel_id: str,
        thread_id: str | None,
    ) -> bool:
        """Persist the final Discord target while keeping the worker held."""

        now = time.time()
        async with self._db.write_transaction() as conn:
            changed = await conn.execute(
                """
                UPDATE coding_tasks
                SET channel_id = ?, thread_id = ?, updated_at = ?, heartbeat_at = ?
                WHERE id = ? AND status = 'queued' AND cancel_requested = 0
                  AND handoff_pending = 1
                """,
                (channel_id, thread_id, now, now, task_id),
            )
            if changed.rowcount != 1:
                return False
            await self._append_event_conn(
                conn,
                task_id,
                "handoff_target",
                {"channel_id": channel_id, "thread_id": thread_id},
                now,
            )
        return True

    async def release_handoff(self, task_id: str) -> bool:
        """Make a routed task claimable after its initial status was attempted."""

        now = time.time()
        async with self._db.write_transaction() as conn:
            changed = await conn.execute(
                """
                UPDATE coding_tasks
                SET handoff_pending = 0, updated_at = ?, heartbeat_at = ?
                WHERE id = ? AND status = 'queued' AND cancel_requested = 0
                  AND handoff_pending = 1
                """,
                (now, now, task_id),
            )
            if changed.rowcount != 1:
                return False
            await self._append_event_conn(conn, task_id, "handoff_released", {}, now)
        return True

    async def abandon_handoff(self, task_id: str, *, reason: str) -> bool:
        """Cancel a committed task whose foreground turn ended before acknowledgement."""

        now = time.time()
        async with self._db.write_transaction() as conn:
            changed = await conn.execute(
                """
                UPDATE coding_tasks
                SET status = 'cancelled', cancel_requested = 1,
                    handoff_pending = 0, delivery_state = 'delivered',
                    finished_at = ?, updated_at = ?, heartbeat_at = ?
                WHERE id = ? AND status = 'queued' AND handoff_pending = 1
                """,
                (now, now, now, task_id),
            )
            if changed.rowcount != 1:
                return False
            await self._append_event_conn(
                conn, task_id, "handoff_abandoned", {"reason": reason}, now
            )
        return True

    async def append_event(
        self, task_id: str, kind: str, payload: dict[str, Any] | None = None
    ) -> None:
        now = time.time()
        async with self._db.write_transaction() as conn:
            await self._append_event_conn(conn, task_id, kind, payload or {}, now)
            await conn.execute(
                "UPDATE coding_tasks SET updated_at = ?, heartbeat_at = ? WHERE id = ?",
                (now, now, task_id),
            )

    async def steer_active_task(
        self,
        task_id: str,
        message: str,
        *,
        max_queued_per_user: int | None = None,
        max_queued_per_workspace: int | None = None,
    ) -> CodingTask | None:
        """Atomically record steering and resume a paused task when capacity permits."""

        now = time.time()
        async with self._db.write_transaction() as conn:
            # Database serializes writers only within one connection. This no-op
            # DML takes SQLite's cross-connection write reservation before either
            # the task state or queue counts are read, closing the paused-resume
            # admission race without broadening every write transaction.
            await conn.execute(
                "UPDATE coding_tasks SET id = id WHERE id = ?",
                (task_id,),
            )
            async with conn.execute(
                "SELECT * FROM coding_tasks WHERE id = ?", (task_id,)
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                return None
            task = self._task_from_row(row)
            if task.status not in ACTIVE_TASK_STATUSES or task.cancel_requested:
                return None

            if task.status == CodingTaskStatus.WAITING_FOR_INPUT:
                async with conn.execute(
                    """
                    SELECT
                        SUM(CASE WHEN user_id = ? AND status = 'queued' THEN 1 ELSE 0 END),
                        SUM(CASE WHEN workspace_key = ? AND status = 'queued' THEN 1 ELSE 0 END)
                    FROM coding_tasks
                    """,
                    (task.user_id, task.workspace_key),
                ) as cursor:
                    counts = await cursor.fetchone()
                assert counts is not None
                if max_queued_per_user is not None and int(counts[0] or 0) >= max_queued_per_user:
                    raise CodingTaskQueueFull("user")
                if (
                    max_queued_per_workspace is not None
                    and int(counts[1] or 0) >= max_queued_per_workspace
                ):
                    raise CodingTaskQueueFull("workspace")

                changed = await conn.execute(
                    """
                    UPDATE coding_tasks
                    SET status = 'queued', updated_at = ?, heartbeat_at = ?
                    WHERE id = ? AND status = 'waiting_for_input'
                      AND cancel_requested = 0
                    """,
                    (now, now, task_id),
                )
                if changed.rowcount != 1:
                    return None
                await self._append_event_conn(conn, task_id, "steering", {"message": message}, now)
                await self._append_event_conn(
                    conn,
                    task_id,
                    "status",
                    {"status": CodingTaskStatus.QUEUED.value},
                    now,
                )
            else:
                await self._append_event_conn(conn, task_id, "steering", {"message": message}, now)
                await conn.execute(
                    "UPDATE coding_tasks SET updated_at = ?, heartbeat_at = ? WHERE id = ?",
                    (now, now, task_id),
                )
        return await self.get_task(task_id)

    async def heartbeat(self, task_id: str) -> None:
        async with self._db.write_transaction() as conn:
            await conn.execute(
                "UPDATE coding_tasks SET heartbeat_at = ? WHERE id = ?",
                (time.time(), task_id),
            )

    async def events(self, task_id: str, *, after_id: int = 0) -> list[CodingTaskEvent]:
        async with self._db.conn.execute(
            "SELECT * FROM coding_task_events WHERE task_id = ? AND id > ? ORDER BY id",
            (task_id, after_id),
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            CodingTaskEvent(
                id=int(row["id"]),
                task_id=str(row["task_id"]),
                kind=str(row["kind"]),
                payload=_json_dict(str(row["payload_json"])),
                created_at=float(row["created_at"]),
            )
            for row in rows
        ]

    async def set_plan(self, task_id: str, plan: list[dict[str, str]]) -> None:
        await self._update_projection(
            task_id, plan_json=json.dumps(plan), event=("plan", {"steps": plan})
        )

    async def set_milestone(self, task_id: str, milestone: str) -> None:
        await self._update_projection(
            task_id, milestone=milestone, event=("milestone", {"message": milestone})
        )

    async def set_checkpoint(self, task_id: str, checkpoint: dict[str, Any]) -> None:
        raw_messages = checkpoint.get("messages")
        message_count = len(raw_messages) if isinstance(raw_messages, list) else 0
        await self._update_projection(
            task_id,
            checkpoint_json=json.dumps(checkpoint),
            event=(
                "checkpoint",
                {
                    "message_count": message_count,
                    "event_cursor": checkpoint.get("event_cursor", 0),
                },
            ),
        )

    async def set_status(self, task_id: str, status: CodingTaskStatus) -> bool:
        now = time.time()
        terminal = status in TERMINAL_TASK_STATUSES
        async with self._db.write_transaction() as conn:
            changed = await conn.execute(
                """
                UPDATE coding_tasks
                SET status = ?, updated_at = ?, heartbeat_at = ?,
                    finished_at = CASE WHEN ? THEN COALESCE(finished_at, ?) ELSE finished_at END
                WHERE id = ?
                """,
                (status.value, now, now, int(terminal), now, task_id),
            )
            if changed.rowcount != 1:
                return False
            await self._append_event_conn(conn, task_id, "status", {"status": status.value}, now)
        return True

    async def transition_active_status(
        self,
        task_id: str,
        status: CodingTaskStatus,
        *,
        from_statuses: frozenset[CodingTaskStatus],
    ) -> bool:
        """Compare-and-set an active status without resurrecting cancellation."""

        if not from_statuses or status in TERMINAL_TASK_STATUSES:
            raise ValueError("active status transition requires active states")
        now = time.time()
        placeholders = ",".join("?" for _ in from_statuses)
        async with self._db.write_transaction() as conn:
            changed = await conn.execute(
                f"""
                UPDATE coding_tasks
                SET status = ?, updated_at = ?, heartbeat_at = ?
                WHERE id = ? AND cancel_requested = 0
                  AND status IN ({placeholders})
                """,
                (
                    status.value,
                    now,
                    now,
                    task_id,
                    *(value.value for value in from_statuses),
                ),
            )
            if changed.rowcount != 1:
                return False
            await self._append_event_conn(conn, task_id, "status", {"status": status.value}, now)
        return True

    async def expire_waiting_for_input(self) -> list[CodingTask]:
        """Terminalize paused tasks whose total task deadline has elapsed."""

        now = time.time()
        expired_ids: list[str] = []
        async with self._db.write_transaction() as conn:
            async with conn.execute(
                """
                SELECT id FROM coding_tasks
                WHERE status = 'waiting_for_input' AND deadline_at <= ?
                """,
                (now,),
            ) as cursor:
                rows = await cursor.fetchall()
            for row in rows:
                task_id = str(row[0])
                changed = await conn.execute(
                    """
                    UPDATE coding_tasks
                    SET status = 'timed_out',
                        error_text = 'The coding task reached its total time limit while waiting for input.',
                        finished_at = ?, updated_at = ?, heartbeat_at = ?,
                        delivery_state = 'final_pending'
                    WHERE id = ? AND status = 'waiting_for_input'
                    """,
                    (now, now, now, task_id),
                )
                if changed.rowcount != 1:
                    continue
                expired_ids.append(task_id)
                await self._append_event_conn(
                    conn,
                    task_id,
                    "finished",
                    {
                        "status": CodingTaskStatus.TIMED_OUT.value,
                        "error": "The coding task reached its total time limit while waiting for input.",
                    },
                    now,
                )
        tasks = [await self.get_task(task_id) for task_id in expired_ids]
        return [task for task in tasks if task is not None]

    async def request_cancel(self, task_id: str, *, reason: str = "") -> bool:
        now = time.time()
        async with self._db.write_transaction() as conn:
            async with conn.execute(
                "SELECT status FROM coding_tasks WHERE id = ?", (task_id,)
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                return False
            prior = str(row[0])
            terminalized = prior in ("queued", "waiting_for_input")
            await conn.execute(
                """
                UPDATE coding_tasks
                SET cancel_requested = 1,
                    handoff_pending = 0,
                    status = CASE WHEN status IN ('queued','waiting_for_input')
                                      THEN 'cancelled'
                                  WHEN status IN ('completed','failed','cancelled','timed_out')
                                      THEN status
                                  ELSE 'cancelling' END,
                    finished_at = CASE WHEN status IN ('queued','waiting_for_input')
                                       THEN ? ELSE finished_at END,
                    delivery_state = CASE WHEN status IN ('queued','waiting_for_input')
                                          THEN 'final_pending' ELSE delivery_state END,
                    updated_at = ?, heartbeat_at = ?
                WHERE id = ?
                """,
                (now, now, now, task_id),
            )
            await self._append_event_conn(
                conn, task_id, "cancel_requested", {"reason": reason}, now
            )
            if terminalized:
                # A row cancelled straight from the queue reaches no worker and
                # no finish() call; without the finished event and the
                # final_pending state the sweeper never announces it when the
                # caller's immediate notify fails.
                await self._append_event_conn(
                    conn, task_id, "finished", {"status": "cancelled", "error": ""}, now
                )
        return True

    async def finish(
        self,
        task_id: str,
        status: CodingTaskStatus,
        *,
        result_text: str = "",
        error_text: str = "",
    ) -> None:
        if status not in TERMINAL_TASK_STATUSES:
            raise ValueError("finish requires a terminal task status")
        now = time.time()
        async with self._db.write_transaction() as conn:
            async with conn.execute(
                "SELECT cancel_requested FROM coding_tasks WHERE id = ?", (task_id,)
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                return
            final_status = CodingTaskStatus.CANCELLED if bool(row[0]) else status
            await conn.execute(
                """
                UPDATE coding_tasks
                SET status = ?, result_text = ?, error_text = ?, finished_at = ?,
                    updated_at = ?, heartbeat_at = ?, delivery_state = 'final_pending'
                WHERE id = ?
                """,
                (
                    final_status.value,
                    result_text if final_status == status else "",
                    error_text if final_status == status else "",
                    now,
                    now,
                    now,
                    task_id,
                ),
            )
            await self._append_event_conn(
                conn,
                task_id,
                "finished",
                {
                    "status": final_status.value,
                    "error": error_text if final_status == status else "",
                },
                now,
            )

    async def mark_status_message(self, task_id: str, message_id: str) -> None:
        now = time.time()
        async with self._db.write_transaction() as conn:
            await conn.execute(
                """
                UPDATE coding_tasks SET status_discord_message_id = ?,
                    delivery_state = CASE WHEN delivery_state = 'final_pending'
                        THEN delivery_state ELSE 'status_sent' END,
                    updated_at = ? WHERE id = ?
                """,
                (message_id, now, task_id),
            )

    async def mark_delivered(self, task_id: str, message_id: str) -> None:
        now = time.time()
        async with self._db.write_transaction() as conn:
            await conn.execute(
                """
                UPDATE coding_tasks SET final_discord_message_id = ?,
                    delivery_state = 'delivered', updated_at = ? WHERE id = ?
                """,
                (message_id, now, task_id),
            )

    async def set_delivery_attachment_plan_if_absent(
        self,
        task_id: str,
        attachment_plan: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Atomically freeze durable attachment preparation before final delivery."""

        now = time.time()
        async with self._db.write_transaction() as conn:
            async with conn.execute(
                "SELECT checkpoint_json, final_discord_message_id FROM coding_tasks WHERE id = ?",
                (task_id,),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None or row[1] is not None:
                return None
            checkpoint = _json_dict(str(row[0]))
            delivery = checkpoint.get("delivery")
            delivery = dict(delivery) if isinstance(delivery, dict) else {}
            existing = delivery.get("attachment_plan")
            if isinstance(existing, dict):
                return dict(existing)
            frozen = dict(attachment_plan)
            delivery["attachment_plan"] = frozen
            checkpoint["delivery"] = delivery
            await conn.execute(
                "UPDATE coding_tasks SET checkpoint_json = ?, updated_at = ? WHERE id = ? "
                "AND final_discord_message_id IS NULL",
                (json.dumps(checkpoint), now, task_id),
            )
            omitted = frozen.get("omitted")
            await self._append_event_conn(
                conn,
                task_id,
                "delivery_attachment_plan",
                {"omitted_count": len(omitted) if isinstance(omitted, list) else 0},
                now,
            )
            return frozen

    async def record_delivery_failure(
        self,
        task_id: str,
        error: str,
        *,
        permanent: bool = False,
        now: float | None = None,
    ) -> CodingTask | None:
        """Persist one failed final-delivery attempt and its bounded retry schedule."""

        attempted_at = time.time() if now is None else now
        async with self._db.write_transaction() as conn:
            async with conn.execute(
                """
                SELECT checkpoint_json, delivery_state, final_discord_message_id,
                       COALESCE(finished_at, updated_at, created_at)
                FROM coding_tasks WHERE id = ?
                """,
                (task_id,),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None or row[2] is not None or str(row[1]) != "final_pending":
                return None

            checkpoint = _json_dict(str(row[0]))
            retry = checkpoint.get("delivery_retry")
            retry = dict(retry) if isinstance(retry, dict) else {}
            previous_attempts = retry.get("attempts", 0)
            attempts = (previous_attempts if isinstance(previous_attempts, int) else 0) + 1
            started_at = float(row[3])
            exhausted = (
                permanent
                or attempts >= DELIVERY_MAX_ATTEMPTS
                or attempted_at - started_at >= DELIVERY_MAX_AGE_SECONDS
            )
            delay_index = min(attempts - 1, len(DELIVERY_RETRY_DELAYS_SECONDS) - 1)
            next_attempt_at = (
                None if exhausted else attempted_at + DELIVERY_RETRY_DELAYS_SECONDS[delay_index]
            )
            safe_error = error.strip()[:500] or "Discord delivery did not complete"
            retry.update(
                {
                    "attempts": attempts,
                    "last_attempt_at": attempted_at,
                    "next_attempt_at": next_attempt_at,
                    "last_error": safe_error,
                    "permanent": permanent,
                    "exhausted": exhausted,
                }
            )
            checkpoint["delivery_retry"] = retry
            delivery_state = "failed" if exhausted else "final_pending"
            await conn.execute(
                """
                UPDATE coding_tasks
                SET checkpoint_json = ?, delivery_state = ?, updated_at = ?
                WHERE id = ? AND final_discord_message_id IS NULL
                  AND delivery_state = 'final_pending'
                """,
                (json.dumps(checkpoint), delivery_state, attempted_at, task_id),
            )
            await self._append_event_conn(
                conn,
                task_id,
                "delivery_failed" if exhausted else "delivery_retry",
                {
                    "attempt": attempts,
                    "next_attempt_at": next_attempt_at,
                    "permanent": permanent,
                    "error": safe_error,
                },
                attempted_at,
            )
        return await self.get_task(task_id)

    async def reset_delivery_retry(self, task_id: str) -> bool:
        """Make an exhausted terminal delivery immediately eligible for manual retry."""

        now = time.time()
        async with self._db.write_transaction() as conn:
            async with conn.execute(
                "SELECT checkpoint_json FROM coding_tasks WHERE id = ?",
                (task_id,),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                return False
            checkpoint = _json_dict(str(row[0]))
            checkpoint.pop("delivery_retry", None)
            changed = await conn.execute(
                """
                UPDATE coding_tasks
                SET checkpoint_json = ?, delivery_state = 'final_pending', updated_at = ?
                WHERE id = ? AND status IN ('completed','failed','cancelled','timed_out')
                  AND final_discord_message_id IS NULL AND delivery_state = 'failed'
                """,
                (json.dumps(checkpoint), now, task_id),
            )
            if changed.rowcount != 1:
                return False
            await self._append_event_conn(conn, task_id, "delivery_retry_reset", {}, now)
        return True

    async def recover_interrupted(self) -> list[CodingTask]:
        """Requeue non-terminal work after recording the uncertain boundary."""

        now = time.time()
        recovered: list[str] = []
        async with self._db.write_transaction() as conn:
            async with conn.execute(
                """
                SELECT id, cancel_requested FROM coding_tasks
                WHERE status IN (
                    'recovering','running','waiting_for_job','waiting_for_input','cancelling'
                )
                """
            ) as cursor:
                rows = await cursor.fetchall()
            for row in rows:
                task_id = str(row[0])
                recovered.append(task_id)
                cancelled_on_recovery = bool(row[1])
                await conn.execute(
                    """
                    UPDATE coding_tasks
                    SET status = CASE
                            WHEN cancel_requested = 1 THEN 'cancelled'
                            WHEN status = 'waiting_for_input' THEN 'waiting_for_input'
                            ELSE 'queued'
                        END,
                        finished_at = CASE WHEN cancel_requested = 1 THEN ? ELSE NULL END,
                        delivery_state = CASE WHEN cancel_requested = 1
                                              THEN 'final_pending' ELSE delivery_state END,
                        updated_at = ?, heartbeat_at = ?
                    WHERE id = ?
                    """,
                    (now, now, now, task_id),
                )
                if cancelled_on_recovery:
                    await self._append_event_conn(
                        conn, task_id, "finished", {"status": "cancelled", "error": ""}, now
                    )
                await conn.execute(
                    """
                    UPDATE coding_command_jobs
                    SET status = 'interrupted', updated_at = ?, finished_at = ?
                    WHERE task_id = ? AND status IN ('queued','running','unsafe')
                    """,
                    (now, now, task_id),
                )
                await self._append_event_conn(
                    conn,
                    task_id,
                    "recovered",
                    {
                        "message": (
                            "The previous worker stopped at an uncertain boundary. "
                            "Do not replay an unfinished command; inspect the workspace first."
                        )
                    },
                    now,
                )
        tasks = [await self.get_task(task_id) for task_id in recovered]
        return [task for task in tasks if task is not None]

    async def create_job(self, task_id: str, request: dict[str, Any]) -> CodingCommandJob:
        now = time.time()
        job_id = uuid4().hex
        async with self._db.write_transaction() as conn:
            await conn.execute(
                """
                INSERT INTO coding_command_jobs (
                    id, task_id, status, request_json, created_at, updated_at
                ) VALUES (?, ?, 'queued', ?, ?, ?)
                """,
                (job_id, task_id, json.dumps(request), now, now),
            )
            await self._append_event_conn(conn, task_id, "job_created", {"job_id": job_id}, now)
        job = await self.get_job(job_id)
        assert job is not None
        return job

    async def create_job_if_active(
        self, task_id: str, request: dict[str, Any]
    ) -> CodingCommandJob | None:
        """Atomically admit a job only while its parent can still start work."""

        now = time.time()
        job_id = uuid4().hex
        async with self._db.write_transaction() as conn:
            changed = await conn.execute(
                """
                INSERT INTO coding_command_jobs (
                    id, task_id, status, request_json, created_at, updated_at
                )
                SELECT ?, id, 'queued', ?, ?, ?
                FROM coding_tasks
                WHERE id = ? AND cancel_requested = 0
                  AND status IN ('queued','running','waiting_for_job')
                """,
                (job_id, json.dumps(request), now, now, task_id),
            )
            if changed.rowcount != 1:
                return None
            await self._append_event_conn(conn, task_id, "job_created", {"job_id": job_id}, now)
        job = await self.get_job(job_id)
        assert job is not None
        return job

    async def get_job(self, job_id: str) -> CodingCommandJob | None:
        async with self._db.conn.execute(
            "SELECT * FROM coding_command_jobs WHERE id = ?", (job_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return self._job_from_row(row) if row is not None else None

    async def list_active_jobs(self, *, task_id: str | None = None) -> list[CodingCommandJob]:
        clause = " AND task_id = ?" if task_id is not None else ""
        params: tuple[str, ...] = (task_id,) if task_id is not None else ()
        async with self._db.conn.execute(
            "SELECT * FROM coding_command_jobs "
            "WHERE status IN ('queued','running','unsafe')"
            f"{clause} ORDER BY created_at",
            params,
        ) as cursor:
            rows = await cursor.fetchall()
        return [self._job_from_row(row) for row in rows]

    async def update_job(
        self,
        job_id: str,
        status: CodingJobStatus,
        *,
        stdout: str = "",
        stderr: str = "",
        exit_code: int | None = None,
        timed_out: bool = False,
        unit_name: str | None = None,
    ) -> None:
        now = time.time()
        terminal = status not in ACTIVE_JOB_STATUSES
        async with self._db.write_transaction() as conn:
            await conn.execute(
                """
                UPDATE coding_command_jobs
                SET status = ?, stdout_text = ?, stderr_text = ?, exit_code = ?,
                    timed_out = ?, unit_name = COALESCE(?, unit_name), updated_at = ?,
                    started_at = CASE WHEN ? = 'running'
                        THEN COALESCE(started_at, ?) ELSE started_at END,
                    finished_at = CASE WHEN ? THEN ? ELSE finished_at END
                WHERE id = ?
                """,
                (
                    status.value,
                    stdout,
                    stderr,
                    exit_code,
                    int(timed_out),
                    unit_name,
                    now,
                    status.value,
                    now,
                    int(terminal),
                    now,
                    job_id,
                ),
            )

    async def delete_user_data(self, user_id: str) -> int:
        async with self._db.write_transaction() as conn:
            async with conn.execute(
                "SELECT COUNT(*) FROM coding_tasks WHERE user_id = ?", (user_id,)
            ) as cursor:
                row = await cursor.fetchone()
            assert row is not None
            count = int(row[0] or 0)
            await conn.execute("DELETE FROM coding_tasks WHERE user_id = ?", (user_id,))
        return count

    async def _update_projection(
        self,
        task_id: str,
        *,
        event: tuple[str, dict[str, Any]],
        **values: Any,
    ) -> None:
        now = time.time()
        columns = [f"{name} = ?" for name in values]
        params = [*values.values(), now, now, task_id]
        async with self._db.write_transaction() as conn:
            await conn.execute(
                f"UPDATE coding_tasks SET {', '.join(columns)}, updated_at = ?, "
                "heartbeat_at = ? WHERE id = ?",
                params,
            )
            await self._append_event_conn(conn, task_id, event[0], event[1], now)

    @staticmethod
    async def _append_event_conn(
        conn: Any,
        task_id: str,
        kind: str,
        payload: dict[str, Any],
        created_at: float,
    ) -> None:
        await conn.execute(
            "INSERT INTO coding_task_events (task_id, kind, payload_json, created_at) "
            "VALUES (?, ?, ?, ?)",
            (task_id, kind, json.dumps(payload), created_at),
        )

    @staticmethod
    def _task_from_row(row: Any) -> CodingTask:
        return CodingTask(
            id=str(row["id"]),
            conversation_id=(
                int(row["conversation_id"]) if row["conversation_id"] is not None else None
            ),
            root_key=str(row["root_key"]),
            workspace_key=str(row["workspace_key"]),
            user_id=str(row["user_id"]),
            user_name=str(row["user_name"]),
            guild_id=str(row["guild_id"]) if row["guild_id"] is not None else None,
            channel_id=str(row["channel_id"]),
            thread_id=str(row["thread_id"]) if row["thread_id"] is not None else None,
            handoff_pending=bool(row["handoff_pending"]),
            trigger_discord_message_id=str(row["trigger_discord_message_id"]),
            objective=str(row["objective"]),
            acceptance_criteria=[
                str(value) for value in _json_list(row["acceptance_criteria_json"])
            ],
            context_text=str(row["context_text"]),
            display_summary=str(row["display_summary"]),
            context_messages=[
                {str(k): str(v) for k, v in message.items()}
                for message in _json_list(row["context_messages_json"])
                if isinstance(message, dict)
            ],
            input_files=[
                {str(k): str(v) for k, v in item.items()}
                for item in _json_list(row["input_files_json"])
                if isinstance(item, dict)
            ],
            status=CodingTaskStatus(str(row["status"])),
            plan=[
                {str(k): str(v) for k, v in step.items()}
                for step in _json_list(row["plan_json"])
                if isinstance(step, dict)
            ],
            milestone=str(row["milestone"]),
            checkpoint=_json_dict(str(row["checkpoint_json"])),
            result_text=str(row["result_text"]),
            error_text=str(row["error_text"]),
            cancel_requested=bool(row["cancel_requested"]),
            status_discord_message_id=(
                str(row["status_discord_message_id"])
                if row["status_discord_message_id"] is not None
                else None
            ),
            final_discord_message_id=(
                str(row["final_discord_message_id"])
                if row["final_discord_message_id"] is not None
                else None
            ),
            delivery_state=str(row["delivery_state"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            started_at=float(row["started_at"]) if row["started_at"] is not None else None,
            finished_at=(float(row["finished_at"]) if row["finished_at"] is not None else None),
            deadline_at=float(row["deadline_at"]),
            heartbeat_at=float(row["heartbeat_at"]),
        )

    @staticmethod
    def _delivery_retry_due(task: CodingTask, now: float) -> bool:
        retry = task.checkpoint.get("delivery_retry")
        if not isinstance(retry, dict):
            return True
        next_attempt_at = retry.get("next_attempt_at")
        return not isinstance(next_attempt_at, int | float) or float(next_attempt_at) <= now

    @staticmethod
    def _job_from_row(row: Any) -> CodingCommandJob:
        return CodingCommandJob(
            id=str(row["id"]),
            task_id=str(row["task_id"]),
            status=CodingJobStatus(str(row["status"])),
            request=_json_dict(str(row["request_json"])),
            unit_name=str(row["unit_name"]) if row["unit_name"] is not None else None,
            stdout=str(row["stdout_text"]),
            stderr=str(row["stderr_text"]),
            exit_code=int(row["exit_code"]) if row["exit_code"] is not None else None,
            timed_out=bool(row["timed_out"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            started_at=float(row["started_at"]) if row["started_at"] is not None else None,
            finished_at=(float(row["finished_at"]) if row["finished_at"] is not None else None),
        )
