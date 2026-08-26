"""Durable, single-process scheduler for module jobs.

Jobs live in ``module_scheduler_jobs`` and survive restarts: a module registers
its handlers by name in ``start()`` and core re-binds persisted jobs to them.
One runner loop per process claims due jobs transactionally by setting a lease,
runs the handler while heartbeating the lease, and then either deletes the job
(one-shot), reschedules it from completion (periodic, with jitter), or backs it
off (failure). A job whose lease is still live is never run concurrently; an
expired lease from a crashed process is claimable again. A persisted job whose
handler is no longer registered stays paused and degrades the module's health.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from kimi_agent_module_api.contracts import (
    Backoff,
    HealthState,
    JobHandler,
    JobInfo,
    JobRun,
    ModuleContractError,
)

log = logging.getLogger(__name__)

TABLE = "module_scheduler_jobs"
DEFAULT_LEASE_SECONDS = 60.0
DEFAULT_POLL_SECONDS = 1.0
HEARTBEAT_FRACTION = 0.5
MAX_ERROR_CHARS = 300
_KEY_MAX = 128
_HANDLER_MAX = 64
_DEFAULT_BACKOFF = Backoff()

SCHEMA_SQL = f"""CREATE TABLE IF NOT EXISTS {TABLE} (
    job_id           TEXT PRIMARY KEY,
    module_name      TEXT NOT NULL,
    job_key          TEXT NOT NULL,
    handler          TEXT NOT NULL,
    run_at           REAL NOT NULL,
    interval_seconds REAL,
    jitter_seconds   REAL NOT NULL DEFAULT 0,
    backoff_json     TEXT NOT NULL DEFAULT '{{}}',
    payload_json     TEXT NOT NULL DEFAULT '{{}}',
    attempt          INTEGER NOT NULL DEFAULT 0,
    leased_until     REAL,
    lease_token      TEXT,
    last_error       TEXT,
    created_at       REAL NOT NULL,
    updated_at       REAL NOT NULL,
    UNIQUE (module_name, job_key)
)"""
INDEX_SQL = f"CREATE INDEX IF NOT EXISTS idx_{TABLE}_due ON {TABLE}(run_at, leased_until)"


def _dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, default=str)


def _loads(text: str | None) -> dict[str, Any]:
    if not text:
        return {}
    try:
        value = json.loads(text)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _validate(key: str, handler_name: str) -> None:
    if not key or len(key) > _KEY_MAX:
        raise ModuleContractError(f"invalid job key {key!r}")
    if not handler_name or len(handler_name) > _HANDLER_MAX:
        raise ModuleContractError(f"invalid handler name {handler_name!r}")


@dataclass(slots=True)
class _Row:
    job_id: str
    module_name: str
    job_key: str
    handler: str
    run_at: float
    interval_seconds: float | None
    jitter_seconds: float
    backoff: Backoff
    payload: dict[str, Any]
    attempt: int
    leased_until: float | None
    lease_token: str | None
    last_error: str | None

    @classmethod
    def from_row(cls, row: Any) -> _Row:
        backoff_raw = _loads(row["backoff_json"])
        backoff = Backoff(
            base_seconds=float(backoff_raw.get("base_seconds", _DEFAULT_BACKOFF.base_seconds)),
            max_seconds=float(backoff_raw.get("max_seconds", _DEFAULT_BACKOFF.max_seconds)),
            multiplier=float(backoff_raw.get("multiplier", _DEFAULT_BACKOFF.multiplier)),
        )
        return cls(
            job_id=str(row["job_id"]),
            module_name=str(row["module_name"]),
            job_key=str(row["job_key"]),
            handler=str(row["handler"]),
            run_at=float(row["run_at"]),
            interval_seconds=(
                float(row["interval_seconds"]) if row["interval_seconds"] is not None else None
            ),
            jitter_seconds=float(row["jitter_seconds"] or 0),
            backoff=backoff,
            payload=_loads(row["payload_json"]),
            attempt=int(row["attempt"] or 0),
            leased_until=float(row["leased_until"]) if row["leased_until"] is not None else None,
            lease_token=str(row["lease_token"]) if row["lease_token"] else None,
            last_error=str(row["last_error"]) if row["last_error"] else None,
        )


class DurableScheduler:
    """Process-wide runner over the shared database; modules get views."""

    def __init__(
        self,
        database: Any,
        *,
        clock: Callable[[], float] = time.time,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        on_health: Callable[[str, HealthState, str], None] | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self._database = database
        self._clock = clock
        self._lease_seconds = lease_seconds
        self._poll_seconds = poll_seconds
        self._on_health = on_health
        self._rng = rng or random.Random()
        self._handlers: dict[tuple[str, str], JobHandler] = {}
        self._runner: asyncio.Task[None] | None = None
        self._running: dict[str, asyncio.Task[None]] = {}
        self._wake = asyncio.Event()
        self._closed = False
        self._paused_reported: set[tuple[str, str]] = set()

    # ---- schema --------------------------------------------------------------

    @staticmethod
    async def ensure_schema(conn: Any) -> None:
        await conn.execute(SCHEMA_SQL)
        await conn.execute(INDEX_SQL)

    # ---- registration --------------------------------------------------------

    def register(self, module_name: str, handler_name: str, handler: JobHandler) -> None:
        _validate("x", handler_name)
        self._handlers[(module_name, handler_name)] = handler
        self._paused_reported.discard((module_name, handler_name))
        self._wake.set()

    def unregister_module(self, module_name: str) -> None:
        for key in [k for k in self._handlers if k[0] == module_name]:
            self._handlers.pop(key, None)

    def view_for(self, module_name: str) -> ModuleSchedulerView:
        return ModuleSchedulerView(self, module_name)

    # ---- job management -------------------------------------------------------

    async def schedule(
        self,
        module_name: str,
        key: str,
        handler_name: str,
        *,
        run_at: float,
        interval_seconds: float | None,
        payload: Mapping[str, Any] | None,
        jitter_seconds: float = 0.0,
        backoff: Backoff | None = None,
    ) -> None:
        _validate(key, handler_name)
        if interval_seconds is not None and interval_seconds <= 0:
            raise ModuleContractError("interval_seconds must be positive")
        now = self._clock()
        backoff = backoff or Backoff()
        job_id = f"{module_name}:{key}"
        async with self._database.write_transaction() as conn:
            await conn.execute(
                f"""INSERT INTO {TABLE} (
                    job_id, module_name, job_key, handler, run_at, interval_seconds,
                    jitter_seconds, backoff_json, payload_json, attempt, leased_until,
                    lease_token, last_error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL, NULL, ?, ?)
                ON CONFLICT(module_name, job_key) DO UPDATE SET
                    handler = excluded.handler,
                    run_at = excluded.run_at,
                    interval_seconds = excluded.interval_seconds,
                    jitter_seconds = excluded.jitter_seconds,
                    backoff_json = excluded.backoff_json,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at""",
                (
                    job_id,
                    module_name,
                    key,
                    handler_name,
                    float(run_at),
                    interval_seconds,
                    float(max(0.0, jitter_seconds)),
                    _dumps(
                        {
                            "base_seconds": backoff.base_seconds,
                            "max_seconds": backoff.max_seconds,
                            "multiplier": backoff.multiplier,
                        }
                    ),
                    _dumps(dict(payload or {})),
                    now,
                    now,
                ),
            )
        self._wake.set()

    async def cancel(self, module_name: str, key: str) -> bool:
        async with self._database.write_transaction() as conn:
            cursor = await conn.execute(
                f"DELETE FROM {TABLE} WHERE module_name = ? AND job_key = ?", (module_name, key)
            )
            return bool(cursor.rowcount)

    async def list_jobs(self, module_name: str) -> Sequence[JobInfo]:
        cursor = await self._database.conn.execute(
            f"SELECT * FROM {TABLE} WHERE module_name = ? ORDER BY run_at", (module_name,)
        )
        rows = [_Row.from_row(row) for row in await cursor.fetchall()]
        return [
            JobInfo(
                key=row.job_key,
                handler=row.handler,
                next_run_at=row.run_at,
                interval_seconds=row.interval_seconds,
                attempt=row.attempt,
                last_error=row.last_error,
            )
            for row in rows
        ]

    # ---- runner ----------------------------------------------------------------

    def start(self) -> None:
        if self._runner is None and not self._closed:
            self._runner = asyncio.create_task(self._run_loop(), name="module-scheduler")

    async def close(self) -> None:
        self._closed = True
        if self._runner is not None:
            self._runner.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._runner
            self._runner = None
        for task in list(self._running.values()):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._running.clear()

    async def run_due(self, *, now: float | None = None, limit: int = 50) -> int:
        """Claim and run every due job once; returns how many ran. Also used by tests."""
        now = self._clock() if now is None else now
        ran = 0
        for _ in range(limit):
            row = await self._claim_next(now)
            if row is None:
                break
            await self._execute(row)
            ran += 1
        return ran

    async def _run_loop(self) -> None:
        while not self._closed:
            try:
                ran = await self.run_due()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Module scheduler tick failed")
                ran = 0
            if ran:
                continue
            self._wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=self._poll_seconds)

    async def _claim_next(self, now: float) -> _Row | None:
        token = f"{now:.6f}:{self._rng.random():.12f}"
        async with self._database.write_transaction() as conn:
            cursor = await conn.execute(
                f"""SELECT * FROM {TABLE}
                    WHERE run_at <= ? AND (leased_until IS NULL OR leased_until < ?)
                    ORDER BY run_at LIMIT 1""",
                (now, now),
            )
            raw = await cursor.fetchone()
            if raw is None:
                return None
            row = _Row.from_row(raw)
            if (row.module_name, row.handler) not in self._handlers:
                self._report_paused(row)
                # Push it out so the loop does not spin on it; it stays persisted.
                await conn.execute(
                    f"UPDATE {TABLE} SET run_at = ?, last_error = ?, updated_at = ? WHERE job_id = ?",
                    (now + self._lease_seconds, f"no handler {row.handler!r}", now, row.job_id),
                )
                return None
            leased_until = now + self._lease_seconds
            await conn.execute(
                f"""UPDATE {TABLE} SET leased_until = ?, lease_token = ?, attempt = attempt + 1,
                    updated_at = ? WHERE job_id = ?""",
                (leased_until, token, now, row.job_id),
            )
            row.leased_until = leased_until
            row.lease_token = token
            row.attempt += 1
            return row

    def _report_paused(self, row: _Row) -> None:
        marker = (row.module_name, row.handler)
        if marker in self._paused_reported:
            return
        self._paused_reported.add(marker)
        log.warning(
            "Module %s job %s has no registered handler %r; paused",
            row.module_name,
            row.job_key,
            row.handler,
        )
        if self._on_health is not None:
            self._on_health(
                row.module_name, "degraded", f"scheduled job {row.job_key!r} has no handler"
            )

    async def _execute(self, row: _Row) -> None:
        handler = self._handlers[(row.module_name, row.handler)]
        run = JobRun(
            job_id=row.job_id,
            key=row.job_key,
            payload=dict(row.payload),
            attempt=row.attempt,
            scheduled_for=row.run_at,
        )
        heartbeat = asyncio.create_task(self._heartbeat(row), name=f"module-job-lease:{row.job_id}")
        error: str | None = None
        try:
            await handler(run)
        except asyncio.CancelledError:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
            raise
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"[:MAX_ERROR_CHARS]
            log.exception("Module %s job %s failed", row.module_name, row.job_key)
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
        await self._settle(row, error)

    async def _heartbeat(self, row: _Row) -> None:
        interval = max(0.05, self._lease_seconds * HEARTBEAT_FRACTION)
        while True:
            await asyncio.sleep(interval)
            async with self._database.write_transaction() as conn:
                await conn.execute(
                    f"UPDATE {TABLE} SET leased_until = ? WHERE job_id = ? AND lease_token = ?",
                    (self._clock() + self._lease_seconds, row.job_id, row.lease_token),
                )

    async def _settle(self, row: _Row, error: str | None) -> None:
        now = self._clock()
        async with self._database.write_transaction() as conn:
            if error is None and row.interval_seconds is None:
                await conn.execute(
                    f"DELETE FROM {TABLE} WHERE job_id = ? AND lease_token = ?",
                    (row.job_id, row.lease_token),
                )
                return
            if error is None:
                jitter = self._rng.uniform(0, row.jitter_seconds) if row.jitter_seconds else 0.0
                next_run = now + float(row.interval_seconds or 0) + jitter
                attempt = 0
            else:
                delay = min(
                    row.backoff.max_seconds,
                    row.backoff.base_seconds * (row.backoff.multiplier ** max(0, row.attempt - 1)),
                )
                next_run = now + delay
                attempt = row.attempt
            await conn.execute(
                f"""UPDATE {TABLE} SET run_at = ?, attempt = ?, leased_until = NULL,
                    lease_token = NULL, last_error = ?, updated_at = ?
                    WHERE job_id = ? AND lease_token = ?""",
                (next_run, attempt, error, now, row.job_id, row.lease_token),
            )


@dataclass(frozen=True, slots=True)
class ModuleSchedulerView:
    """The ``Scheduler`` port handed to one module."""

    scheduler: DurableScheduler
    module_name: str

    def register(self, handler_name: str, handler: JobHandler) -> None:
        self.scheduler.register(self.module_name, handler_name, handler)

    async def run_at(
        self,
        key: str,
        when: float,
        handler_name: str,
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        await self.scheduler.schedule(
            self.module_name,
            key,
            handler_name,
            run_at=when,
            interval_seconds=None,
            payload=payload,
        )

    async def run_every(
        self,
        key: str,
        interval_seconds: float,
        handler_name: str,
        payload: Mapping[str, Any] | None = None,
        *,
        jitter_seconds: float = 0.0,
        backoff: Backoff | None = None,
    ) -> None:
        await self.scheduler.schedule(
            self.module_name,
            key,
            handler_name,
            run_at=self.scheduler._clock(),
            interval_seconds=interval_seconds,
            payload=payload,
            jitter_seconds=jitter_seconds,
            backoff=backoff,
        )

    async def cancel(self, key: str) -> bool:
        return await self.scheduler.cancel(self.module_name, key)

    async def list(self) -> Sequence[JobInfo]:
        return await self.scheduler.list_jobs(self.module_name)


__all__ = ["INDEX_SQL", "SCHEMA_SQL", "TABLE", "DurableScheduler", "ModuleSchedulerView"]

_ = field  # dataclasses.field retained for future per-view state
