"""Durable, single-process scheduler for module jobs.

Jobs live in ``module_scheduler_jobs`` and survive restarts: a module registers
its handlers by name in ``start()`` and core re-binds persisted jobs to them.
One runner loop per process claims due jobs transactionally by setting a lease,
runs the handler while heartbeating the lease, and then either deletes the job
(one-shot), reschedules it from completion (periodic, with jitter), or backs it
off (failure). A job whose lease is still live is never run concurrently; an
expired lease from a crashed process is claimable again. A persisted job whose
handler is no longer registered stays paused and degrades the module's health.

The runner executes up to ``max_concurrent`` jobs at once, at most one per
module, so one module's long job cannot delay another module's due work while
a module's own handlers still never overlap. Kimi is a single process: before
claiming anything, the runner must hold the singleton lease in
``module_scheduler_runner``, renewed every tick. A second process against the
same database cannot take it while it is live, so it pauses (degrading every
module with registered handlers) until the holder releases it on close or
lets it expire.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import random
import sys
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from modules.tasks import DEFAULT_CANCEL_GRACE_SECONDS, cancel_with_grace, run_bounded

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
RUNNER_TABLE = "module_scheduler_runner"
DEFAULT_LEASE_SECONDS = 60.0
DEFAULT_POLL_SECONDS = 1.0
DEFAULT_MAX_CONCURRENT = 4
FOREIGN_RUNNER_DETAIL = "another scheduler runner holds the lease; jobs paused"
HEARTBEAT_FRACTION = 0.5
MAX_ERROR_CHARS = 300
_KEY_MAX = 128
_HANDLER_MAX = 64
_DEFAULT_BACKOFF = Backoff()
_MAX_FLOAT_LOG = math.log(sys.float_info.max)

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
RUNNER_SCHEMA_SQL = f"""CREATE TABLE IF NOT EXISTS {RUNNER_TABLE} (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    token        TEXT,
    leased_until REAL NOT NULL DEFAULT 0
)"""
RUNNER_SEED_SQL = (
    f"INSERT OR IGNORE INTO {RUNNER_TABLE} (id, token, leased_until) VALUES (1, NULL, 0)"
)


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


def _capped_retry_delay(backoff: Backoff, attempt: int) -> float:
    exponent = max(0, attempt - 1)
    base = backoff.base_seconds
    cap = backoff.max_seconds
    if base >= cap:
        return cap
    if exponent == 0 or backoff.multiplier == 1:
        return base

    growth_log = exponent * math.log(backoff.multiplier)
    if growth_log >= math.log(cap) - math.log(base):
        return cap

    # Preserve the existing arithmetic (and rounding) when the intermediate
    # growth factor is representable. For a tiny base, the factor itself may
    # overflow even though the final product is finite and below the cap.
    if growth_log <= _MAX_FLOAT_LOG:
        try:
            return base * (backoff.multiplier**exponent)
        except OverflowError:
            pass
    return min(cap, math.exp(math.log(base) + growth_log))


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
    definition_token: str
    payload: dict[str, Any]
    attempt: int
    leased_until: float | None
    lease_token: str | None
    last_error: str | None

    @classmethod
    def from_row(cls, row: Any) -> _Row:
        definition_token = str(row["backoff_json"] or "{}")
        backoff_raw = _loads(definition_token)
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
            definition_token=definition_token,
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
        max_concurrent: int = DEFAULT_MAX_CONCURRENT,
        on_health: Callable[[str, HealthState, str], None] | None = None,
        rng: random.Random | None = None,
    ) -> None:
        if max_concurrent < 1:
            raise ModuleContractError("max_concurrent must be at least 1")
        self._database = database
        self._clock = clock
        self._lease_seconds = lease_seconds
        self._poll_seconds = poll_seconds
        self._max_concurrent = max_concurrent
        self._on_health = on_health
        self._rng = rng or random.Random()
        self._handlers: dict[tuple[str, str], JobHandler] = {}
        self._runner: asyncio.Task[None] | None = None
        # In-flight executions started by the runner loop, keyed by job id, and
        # the modules they belong to (one job per module at a time).
        self._running: dict[str, asyncio.Task[None]] = {}
        self._running_modules: set[str] = set()
        # This process's runner identity; the singleton lease row carries it.
        self._runner_token = uuid.uuid4().hex
        self._foreign_paused = False
        self._wake = asyncio.Event()
        self._closed = False
        # (module, handler) -> the detail reported when its job was found orphaned.
        self._paused_reported: dict[tuple[str, str], str] = {}
        # Claim token -> module reserved by an in-flight claim transaction.
        self._reserving: dict[str, str] = {}

    # ---- schema --------------------------------------------------------------

    @staticmethod
    async def ensure_schema(conn: Any) -> None:
        await conn.execute(SCHEMA_SQL)
        await conn.execute(INDEX_SQL)
        await conn.execute(RUNNER_SCHEMA_SQL)
        await conn.execute(RUNNER_SEED_SQL)

    # ---- registration --------------------------------------------------------

    def register(self, module_name: str, handler_name: str, handler: JobHandler) -> None:
        _validate("x", handler_name)
        self._handlers[(module_name, handler_name)] = handler
        marker = (module_name, handler_name)
        was_paused = self._paused_reported.pop(marker, None) is not None
        if was_paused:
            self._clear_paused_health_if_recovered(module_name)
        if self._foreign_paused and self._on_health is not None:
            # Joined during a pause: say so, or its jobs silently never run.
            self._on_health(module_name, "degraded", FOREIGN_RUNNER_DETAIL)
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
        # The revision lives in core-owned scheduler metadata, not the module's
        # payload. A running execution retains its lease (and can heartbeat it),
        # while settlement also checks this token before mutating the definition.
        backoff_json = _dumps(
            {
                "base_seconds": backoff.base_seconds,
                "max_seconds": backoff.max_seconds,
                "multiplier": backoff.multiplier,
                "_definition_revision": uuid.uuid4().hex,
            }
        )
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
                    attempt = 0,
                    last_error = NULL,
                    updated_at = excluded.updated_at""",
                (
                    job_id,
                    module_name,
                    key,
                    handler_name,
                    float(run_at),
                    interval_seconds,
                    float(max(0.0, jitter_seconds)),
                    backoff_json,
                    _dumps(dict(payload or {})),
                    now,
                    now,
                ),
            )
        self._wake.set()

    async def cancel(self, module_name: str, key: str) -> bool:
        async with self._database.write_transaction() as conn:
            existing = await conn.execute(
                f"SELECT handler FROM {TABLE} WHERE module_name = ? AND job_key = ?",
                (module_name, key),
            )
            row = await existing.fetchone()
            cursor = await conn.execute(
                f"DELETE FROM {TABLE} WHERE module_name = ? AND job_key = ?", (module_name, key)
            )
            removed = bool(cursor.rowcount)
            if removed and row is not None:
                handler_name = str(row["handler"])
                marker = (module_name, handler_name)
                remaining = await conn.execute(
                    f"""SELECT job_key FROM {TABLE} WHERE module_name = ? AND handler = ?
                        ORDER BY run_at LIMIT 1""",
                    (module_name, handler_name),
                )
                survivor = await remaining.fetchone()
                if survivor is None:
                    self._paused_reported.pop(marker, None)
                elif marker in self._paused_reported:
                    # The orphan detail must name a job that still exists.
                    detail = f"scheduled job {str(survivor['job_key'])!r} has no handler"
                    self._paused_reported[marker] = detail
                    if self._on_health is not None and not self._foreign_paused:
                        self._on_health(module_name, "degraded", detail)
        if removed:
            self._clear_paused_health_if_recovered(module_name)
        return removed

    def _clear_paused_health_if_recovered(self, module_name: str) -> None:
        if self._on_health is None or self._foreign_paused:
            return
        if not any(name == module_name for name, _handler in self._paused_reported):
            self._on_health(module_name, "healthy", "")

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
        await cancel_with_grace(
            list(self._running.values()),
            grace=DEFAULT_CANCEL_GRACE_SECONDS,
            what="module scheduler job",
        )
        self._running.clear()
        self._running_modules.clear()
        # An abandoned job may still hold the database write lock; releasing
        # the lease must not wait on it. An unreleased lease expires on its own.
        outcome = await run_bounded(
            self._release_runner_lease(),
            timeout=DEFAULT_CANCEL_GRACE_SECONDS,
            what="module scheduler lease release",
        )
        if outcome.timed_out:
            log.error(
                "Module scheduler could not release its runner lease; it expires in %gs",
                self._lease_seconds,
            )
        elif outcome.error is not None:
            raise outcome.error

    @property
    def paused_for_foreign_runner(self) -> bool:
        return self._foreign_paused

    async def run_due(self, *, now: float | None = None, limit: int = 50) -> int:
        """Claim and run due jobs one at a time, inline; returns how many ran.

        This is the serial path used by tests and by callers that want a
        deterministic tick. It holds the same runner lease and shares the
        one-job-per-module bookkeeping with the runner loop, so it never runs
        while another process owns the scheduler or overlaps a running job.
        """
        now = self._clock() if now is None else now
        if not await self._acquire_runner_lease(now):
            self._enter_foreign_pause()
            return 0
        self._exit_foreign_pause()
        ran = 0
        for _ in range(limit):
            row = await self._claim_next(now)
            if row is None:
                break
            # Tracked like a runner-started job so close() can cancel it.
            task = asyncio.create_task(self._run_claimed(row), name=f"module-job:{row.job_id}")
            self._running[row.job_id] = task
            await task
            ran += 1
        return ran

    async def _run_loop(self) -> None:
        while not self._closed:
            try:
                progressed = await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Module scheduler tick failed")
                progressed = False
            if progressed:
                continue
            self._wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=self._poll_seconds)

    async def _tick(self) -> bool:
        """Renew the runner lease, then start as many due jobs as capacity allows."""
        now = self._clock()
        if not await self._acquire_runner_lease(now):
            self._enter_foreign_pause()
            return False
        self._exit_foreign_pause()
        started = 0
        while len(self._running) < self._max_concurrent:
            row = await self._claim_next(now)
            if row is None:
                break
            task = asyncio.create_task(self._run_claimed(row), name=f"module-job:{row.job_id}")
            self._running[row.job_id] = task
            started += 1
        return started > 0

    async def _run_claimed(self, row: _Row) -> None:
        if row.lease_token is not None:
            self._reserving.pop(row.lease_token, None)
        try:
            await self._execute(row)
        except asyncio.CancelledError:
            raise
        except Exception:
            # _execute already contains handler failures; this is settlement
            # itself failing (database error). Never let it vanish with the task.
            log.exception("Module %s job %s could not be settled", row.module_name, row.job_key)
        finally:
            self._running.pop(row.job_id, None)
            self._running_modules.discard(row.module_name)
            # Capacity freed: let the loop claim the next due job immediately.
            self._wake.set()

    async def _acquire_runner_lease(self, now: float) -> bool:
        """Take or renew the singleton runner lease atomically.

        The conditional UPDATE is the whole protocol: it succeeds only when the
        row is unleased, expired, or already ours, and SQLite evaluates it under
        the write lock, so two processes cannot both win.
        """
        async with self._database.write_transaction() as conn:
            cursor = await conn.execute(
                f"""UPDATE {RUNNER_TABLE} SET token = ?, leased_until = ?
                    WHERE id = 1 AND (token IS NULL OR token = ? OR leased_until <= ?)""",
                (self._runner_token, now + self._lease_seconds, self._runner_token, now),
            )
            return bool(cursor.rowcount)

    async def _release_runner_lease(self) -> None:
        """Hand the lease back so a restart does not wait for it to expire.

        The scheduler closes before the database, so a failure here is a real
        error and surfaces as one.
        """
        async with self._database.write_transaction() as conn:
            await conn.execute(
                f"UPDATE {RUNNER_TABLE} SET token = NULL, leased_until = 0 WHERE token = ?",
                (self._runner_token,),
            )

    def _module_names(self) -> set[str]:
        return {module for module, _handler in self._handlers}

    def _enter_foreign_pause(self) -> None:
        if self._foreign_paused:
            return
        self._foreign_paused = True
        log.error(
            "Module scheduler paused: %s. Another Kimi process is running jobs against "
            "this database; stop it, or wait for its lease (%gs) to expire.",
            FOREIGN_RUNNER_DETAIL,
            self._lease_seconds,
        )
        if self._on_health is not None:
            for module in sorted(self._module_names()):
                self._on_health(module, "degraded", FOREIGN_RUNNER_DETAIL)

    def _exit_foreign_pause(self) -> None:
        if not self._foreign_paused:
            return
        self._foreign_paused = False
        log.info("Module scheduler resumed: runner lease acquired")
        if self._on_health is None:
            return
        for module in sorted(self._module_names()):
            details = sorted(
                detail
                for (name, _handler), detail in self._paused_reported.items()
                if name == module
            )
            if details:
                self._on_health(module, "degraded", details[0])
            else:
                self._on_health(module, "healthy", "")

    async def _claim_next(self, now: float) -> _Row | None:
        """Lease the next due job whose module has nothing running; reserve its module.

        The reservation in ``_running_modules`` is made under the same write
        lock as the lease, so two claimants (the runner loop and ``run_due``)
        cannot both take a job for one module. ``_run_claimed`` releases it.
        """
        token = f"{now:.6f}:{self._rng.random():.12f}"
        try:
            return await self._claim_locked(now, token)
        except BaseException:
            # The reservation is made inside the transaction; if the commit (or
            # anything after the add) fails there is no execution to release it.
            self._running_modules.discard(self._reserving.pop(token, ""))
            raise

    async def _claim_locked(self, now: float, token: str) -> _Row | None:
        async with self._database.write_transaction() as conn:
            excluded = sorted(self._running_modules)
            where = "run_at <= ? AND (leased_until IS NULL OR leased_until < ?)"
            params: list[Any] = [now, now]
            if excluded:
                where += " AND module_name NOT IN (" + ",".join("?" for _ in excluded) + ")"
                params.extend(excluded)
            while True:
                cursor = await conn.execute(
                    f"SELECT * FROM {TABLE} WHERE {where} ORDER BY run_at LIMIT 1", params
                )
                raw = await cursor.fetchone()
                if raw is None:
                    return None
                row = _Row.from_row(raw)
                if (row.module_name, row.handler) not in self._handlers:
                    self._report_paused(row)
                    # Push it out so this claim can continue to the next due job;
                    # the orphan stays persisted without starving runnable work.
                    await conn.execute(
                        f"""UPDATE {TABLE} SET run_at = ?, last_error = ?, updated_at = ?
                            WHERE job_id = ?""",
                        (
                            now + self._lease_seconds,
                            f"no handler {row.handler!r}",
                            now,
                            row.job_id,
                        ),
                    )
                    continue
                leased_until = now + self._lease_seconds
                await conn.execute(
                    f"""UPDATE {TABLE} SET leased_until = ?, lease_token = ?,
                        attempt = attempt + 1, updated_at = ? WHERE job_id = ?""",
                    (leased_until, token, now, row.job_id),
                )
                row.leased_until = leased_until
                row.lease_token = token
                row.attempt += 1
                self._running_modules.add(row.module_name)
                self._reserving[token] = row.module_name
                return row

    def _report_paused(self, row: _Row) -> None:
        marker = (row.module_name, row.handler)
        if marker in self._paused_reported:
            return
        detail = f"scheduled job {row.job_key!r} has no handler"
        self._paused_reported[marker] = detail
        log.warning(
            "Module %s job %s has no registered handler %r; paused",
            row.module_name,
            row.job_key,
            row.handler,
        )
        if self._on_health is not None:
            self._on_health(row.module_name, "degraded", detail)

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
        """Keep both the job lease and this process's runner lease alive while a job runs.

        Renewing the runner lease here, not only in ``_tick``, means a long
        job started from ``run_due`` (which has no loop ticking beside it)
        still keeps other processes out.
        """
        interval = max(0.05, self._lease_seconds * HEARTBEAT_FRACTION)
        while True:
            await asyncio.sleep(interval)
            if self._closed:
                # A handler that ignored cancellation may still own this task;
                # after close() the leases must be allowed to expire.
                return
            until = self._clock() + self._lease_seconds
            async with self._database.write_transaction() as conn:
                await conn.execute(
                    f"UPDATE {TABLE} SET leased_until = ? WHERE job_id = ? AND lease_token = ?",
                    (until, row.job_id, row.lease_token),
                )
                await conn.execute(
                    f"UPDATE {RUNNER_TABLE} SET leased_until = ? WHERE id = 1 AND token = ?",
                    (until, self._runner_token),
                )

    async def _settle(self, row: _Row, error: str | None) -> None:
        now = self._clock()
        async with self._database.write_transaction() as conn:
            if error is None and row.interval_seconds is None:
                cursor = await conn.execute(
                    f"""DELETE FROM {TABLE}
                        WHERE job_id = ? AND lease_token = ? AND backoff_json = ?""",
                    (row.job_id, row.lease_token, row.definition_token),
                )
                if not cursor.rowcount:
                    await self._release_stale_lease(conn, row)
                return
            if error is None:
                jitter = self._rng.uniform(0, row.jitter_seconds) if row.jitter_seconds else 0.0
                next_run = now + float(row.interval_seconds or 0) + jitter
                attempt = 0
            else:
                delay = _capped_retry_delay(row.backoff, row.attempt)
                next_run = now + delay
                attempt = row.attempt
            cursor = await conn.execute(
                f"""UPDATE {TABLE} SET run_at = ?, attempt = ?, leased_until = NULL,
                    lease_token = NULL, last_error = ?, updated_at = ?
                    WHERE job_id = ? AND lease_token = ? AND backoff_json = ?""",
                (
                    next_run,
                    attempt,
                    error,
                    now,
                    row.job_id,
                    row.lease_token,
                    row.definition_token,
                ),
            )
            if not cursor.rowcount:
                await self._release_stale_lease(conn, row)

    @staticmethod
    async def _release_stale_lease(conn: Any, row: _Row) -> None:
        """Release this execution's lease without touching a replacement definition."""
        await conn.execute(
            f"""UPDATE {TABLE} SET leased_until = NULL, lease_token = NULL
                WHERE job_id = ? AND lease_token = ?""",
            (row.job_id, row.lease_token),
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


__all__ = [
    "INDEX_SQL",
    "RUNNER_SCHEMA_SQL",
    "RUNNER_SEED_SQL",
    "RUNNER_TABLE",
    "SCHEMA_SQL",
    "TABLE",
    "DurableScheduler",
    "ModuleSchedulerView",
]

_ = field  # dataclasses.field retained for future per-view state
