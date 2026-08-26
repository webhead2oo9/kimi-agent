from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
import logging
from pathlib import Path

import aiosqlite

log = logging.getLogger(__name__)
SCHEMA_VERSION = 2

_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER PRIMARY KEY,
    name       TEXT,
    applied_at TEXT
);

-- Optional application modules version their own schemas independently from
-- the flattened core v1 baseline. Removing a module never drops its data.
CREATE TABLE IF NOT EXISTS module_schema_versions (
    module_name TEXT NOT NULL,
    version     INTEGER NOT NULL CHECK (version > 0),
    name        TEXT NOT NULL,
    applied_at  TEXT NOT NULL,
    PRIMARY KEY (module_name, version)
);

CREATE TABLE IF NOT EXISTS conversations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    key             TEXT UNIQUE NOT NULL,
    channel_name    TEXT DEFAULT '',
    guild_id        TEXT,
    channel_id      TEXT,
    thread_id       TEXT,
    root_discord_message_id TEXT,
    owner_user_id   TEXT,
    access_scope    TEXT NOT NULL DEFAULT 'channel_shared'
                    CHECK (access_scope IN ('channel_shared', 'owner_only')),
    created_at      REAL NOT NULL,
    last_active_at  REAL NOT NULL,
    eval_cursor     INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_conversations_root
    ON conversations(guild_id, channel_id, thread_id, root_discord_message_id);

CREATE INDEX IF NOT EXISTS idx_conversations_owner
    ON conversations(owner_user_id);

CREATE TABLE IF NOT EXISTS conversation_activated_tools (
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    tool_name       TEXT NOT NULL,
    PRIMARY KEY (conversation_id, tool_name)
);

CREATE TABLE IF NOT EXISTS messages (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id     INTEGER NOT NULL REFERENCES conversations(id),
    role                TEXT NOT NULL,
    user_id             TEXT,
    user_name           TEXT,
    content             TEXT,
    message_data        TEXT NOT NULL,
    discord_message_id  TEXT,
    source_created_at   REAL,
    created_at          REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_conversation_id
    ON messages(conversation_id, id);

-- Dedup channel-transcript rows by their Discord message id (NULLs are distinct
-- in SQLite, so non-channel rows without an id never collide).
CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_conv_discord
    ON messages(conversation_id, discord_message_id);

-- Counting a user's prior messages across conversations (new-user onboarding) would
-- otherwise full-scan this ever-growing table on every mention-path turn.
CREATE INDEX IF NOT EXISTS idx_messages_user_id
    ON messages(user_id);

CREATE TABLE IF NOT EXISTS message_contexts (
    discord_message_id TEXT PRIMARY KEY,
    conversation_id    INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    channel_id         TEXT NOT NULL,
    created_at         REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_message_contexts_channel_message
    ON message_contexts(channel_id, discord_message_id);

-- Threads the bot created via thread handoff: every message in a mapped thread
-- continues the mapped root conversation. auto_respond is the thread's mode --
-- 1 means no mention is needed, 0 means the thread is paused and falls back to
-- the ordinary channel contract while staying mapped to its root.
CREATE TABLE IF NOT EXISTS thread_conversations (
    thread_id       TEXT PRIMARY KEY,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    creator_user_id TEXT,
    created_at      REAL NOT NULL,
    auto_respond    INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS user_preferences (
    user_id            TEXT PRIMARY KEY,
    memory_enabled     INTEGER DEFAULT 1,
    privacy_consent    INTEGER DEFAULT 0,
    privacy_consent_at REAL,
    persona_prompt     TEXT,
    persona_updated_at REAL,
    created_at         REAL NOT NULL,
    updated_at         REAL NOT NULL
);

-- Operator-selected global chat model. The nullable value means use the
-- roles.chat / scoped routing declared in config/models.yaml.
CREATE TABLE IF NOT EXISTS model_selection (
    singleton  INTEGER PRIMARY KEY CHECK (singleton = 1),
    model_name TEXT,
    updated_at REAL NOT NULL
);

INSERT OR IGNORE INTO model_selection (singleton, model_name, updated_at)
VALUES (1, NULL, 0);

CREATE TABLE IF NOT EXISTS blocked_users (
    user_id     TEXT PRIMARY KEY,
    blocked_by  TEXT NOT NULL,
    reason      TEXT DEFAULT '',
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS usage_ledger (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id             TEXT NOT NULL,
    user_name           TEXT,
    channel_id          TEXT,
    guild_id            TEXT,
    model               TEXT NOT NULL,
    role                TEXT NOT NULL,
    pricing_model       TEXT,
    input_tokens        INTEGER NOT NULL DEFAULT 0,
    cached_read_tokens  INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens       INTEGER NOT NULL DEFAULT 0,
    iterations          INTEGER NOT NULL DEFAULT 1,
    est_cost_usd        REAL,
    turn_id             TEXT,
    created_at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_usage_user_time
    ON usage_ledger(user_id, created_at);

CREATE INDEX IF NOT EXISTS idx_usage_guild_time
    ON usage_ledger(guild_id, created_at);

CREATE INDEX IF NOT EXISTS idx_usage_time
    ON usage_ledger(created_at);

CREATE INDEX IF NOT EXISTS idx_usage_turn
    ON usage_ledger(turn_id);

-- Cash spend charged by non-LLM tool providers. Each row represents one
-- backend that actually billed during a tool call; free and unpriced calls do
-- not create rows, so this ledger never invents spend.
CREATE TABLE IF NOT EXISTS paid_usage_ledger (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT NOT NULL,
    user_name   TEXT,
    channel_id  TEXT,
    guild_id    TEXT,
    tool_name   TEXT NOT NULL,
    provider    TEXT NOT NULL,
    cost_usd    REAL NOT NULL CHECK (cost_usd > 0),
    turn_id     TEXT,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_paid_usage_user_time
    ON paid_usage_ledger(user_id, created_at);

CREATE INDEX IF NOT EXISTS idx_paid_usage_guild_time
    ON paid_usage_ledger(guild_id, created_at);

CREATE INDEX IF NOT EXISTS idx_paid_usage_time
    ON paid_usage_ledger(created_at);

CREATE INDEX IF NOT EXISTS idx_paid_usage_turn
    ON paid_usage_ledger(turn_id);

-- Zero-cost counters for bounded tool surfaces. Kept separate from both spend
-- ledgers so rate-limit markers never appear as model calls or paid usage.
CREATE TABLE IF NOT EXISTS usage_markers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT NOT NULL,
    user_name   TEXT,
    channel_id  TEXT,
    guild_id    TEXT,
    surface     TEXT NOT NULL,
    operation   TEXT NOT NULL,
    unit_count  INTEGER NOT NULL DEFAULT 1 CHECK (unit_count > 0),
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_usage_markers_user_surface_time
    ON usage_markers(user_id, surface, created_at);

CREATE INDEX IF NOT EXISTS idx_usage_markers_time
    ON usage_markers(created_at);


-- Cached visual descriptions for non-vision chat models. Cache scope is one
-- conversation so descriptions never cross privacy or guild boundaries; the
-- foreign key removes them with transcript retention and /privacy deletion.
CREATE TABLE IF NOT EXISTS image_distillations (
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    cache_key       TEXT NOT NULL,
    model_name      TEXT NOT NULL,
    prompt_version  INTEGER NOT NULL,
    description     TEXT NOT NULL,
    created_at      REAL NOT NULL,
    PRIMARY KEY (conversation_id, cache_key)
);


-- Auto-retain progress markers (docs/memory.md): highest messages.id already
-- flushed to Hindsight per (conversation, user). Advancing the watermark
-- without retaining is how opt-out, trivial-content, and forget-me slices are
-- permanently skipped.
CREATE TABLE IF NOT EXISTS auto_retain_watermarks (
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    user_id         TEXT NOT NULL,
    last_retained_message_id INTEGER NOT NULL,
    updated_at      REAL NOT NULL,
    PRIMARY KEY (conversation_id, user_id)
);

-- Durable authorization for user-requested privacy deletion. One row per user
-- coalesces repeated requests; a unique token prevents an older worker from
-- completing a newer or wider request after a crash/race (including ABA).
CREATE TABLE IF NOT EXISTS privacy_deletion_requests (
    user_id                 TEXT PRIMARY KEY,
    scope                   TEXT NOT NULL CHECK (scope IN ('memory', 'all')),
    generation              INTEGER NOT NULL,
    request_token           TEXT NOT NULL,
    memory_backend_required INTEGER NOT NULL DEFAULT 0,
    requested_at            REAL NOT NULL,
    updated_at              REAL NOT NULL
);

-- Conservative local knowledge of per-user Hindsight banks. A true row is
-- written before any create/retain attempt and cleared only after a confirmed
-- bank delete (including an idempotent backend 404). This lets /privacy keep a
-- deletion pending while Hindsight is temporarily unconfigured.
CREATE TABLE IF NOT EXISTS user_memory_bank_states (
    user_id    TEXT PRIMARY KEY,
    may_exist  INTEGER NOT NULL CHECK (may_exist IN (0, 1)),
    updated_at REAL NOT NULL
);

-- Durable background coding tasks. The internal agent journal stays separate
-- from the user-facing conversation transcript, while the conversation FK lets
-- transcript retention remove task detail rooted in an expired conversation.
CREATE TABLE IF NOT EXISTS coding_tasks (
    id                         TEXT PRIMARY KEY,
    conversation_id            INTEGER REFERENCES conversations(id) ON DELETE CASCADE,
    root_key                    TEXT NOT NULL,
    workspace_key               TEXT NOT NULL,
    user_id                     TEXT NOT NULL,
    user_name                   TEXT NOT NULL DEFAULT '',
    guild_id                    TEXT,
    channel_id                  TEXT NOT NULL,
    thread_id                   TEXT,
    handoff_pending             INTEGER NOT NULL DEFAULT 0
                                CHECK (handoff_pending IN (0, 1)),
    trigger_discord_message_id  TEXT NOT NULL DEFAULT '',
    objective                   TEXT NOT NULL,
    acceptance_criteria_json    TEXT NOT NULL DEFAULT '[]',
    context_text                TEXT NOT NULL DEFAULT '',
    status                      TEXT NOT NULL
                                CHECK (status IN (
                                    'queued','recovering','running','waiting_for_job',
                                    'waiting_for_input','cancelling','completed','failed',
                                    'cancelled','timed_out'
                                )),
    plan_json                   TEXT NOT NULL DEFAULT '[]',
    milestone                  TEXT NOT NULL DEFAULT '',
    checkpoint_json            TEXT NOT NULL DEFAULT '{}',
    result_text                 TEXT NOT NULL DEFAULT '',
    error_text                  TEXT NOT NULL DEFAULT '',
    cancel_requested            INTEGER NOT NULL DEFAULT 0 CHECK (cancel_requested IN (0, 1)),
    status_discord_message_id   TEXT,
    final_discord_message_id    TEXT,
    delivery_state              TEXT NOT NULL DEFAULT 'pending'
                                CHECK (delivery_state IN (
                                    'pending','status_sent','final_pending','delivered','failed'
                                )),
    created_at                  REAL NOT NULL,
    updated_at                  REAL NOT NULL,
    started_at                  REAL,
    finished_at                 REAL,
    deadline_at                 REAL NOT NULL,
    heartbeat_at                REAL NOT NULL,
    display_summary             TEXT NOT NULL DEFAULT '',
    context_messages_json       TEXT NOT NULL DEFAULT '[]',
    input_files_json            TEXT NOT NULL DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_coding_tasks_workspace_queue
    ON coding_tasks(workspace_key, status, created_at);

CREATE INDEX IF NOT EXISTS idx_coding_tasks_user_status
    ON coding_tasks(user_id, status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_coding_tasks_root_status
    ON coding_tasks(root_key, status, created_at DESC);

CREATE TABLE IF NOT EXISTS coding_task_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT NOT NULL REFERENCES coding_tasks(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at  REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_coding_task_events_task
    ON coding_task_events(task_id, id);

CREATE TABLE IF NOT EXISTS coding_command_jobs (
    id              TEXT PRIMARY KEY,
    task_id         TEXT NOT NULL REFERENCES coding_tasks(id) ON DELETE CASCADE,
    status          TEXT NOT NULL
                    CHECK (status IN (
                        'queued','running','succeeded','failed','cancelled',
                        'timed_out','interrupted','unsafe'
                    )),
    request_json    TEXT NOT NULL,
    unit_name       TEXT,
    stdout_text     TEXT NOT NULL DEFAULT '',
    stderr_text     TEXT NOT NULL DEFAULT '',
    exit_code       INTEGER,
    timed_out       INTEGER NOT NULL DEFAULT 0 CHECK (timed_out IN (0, 1)),
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL,
    started_at      REAL,
    finished_at     REAL
);

CREATE INDEX IF NOT EXISTS idx_coding_jobs_task
    ON coding_command_jobs(task_id, created_at);

CREATE TABLE IF NOT EXISTS control_proposals (
    proposal_id       TEXT PRIMARY KEY,
    module_name       TEXT NOT NULL,
    action            TEXT NOT NULL,
    target            TEXT NOT NULL,
    summary           TEXT NOT NULL,
    changes_json      TEXT NOT NULL,
    actor_json        TEXT NOT NULL,
    expected_revision TEXT,
    preview_json      TEXT NOT NULL,
    state             TEXT NOT NULL CHECK (state IN (
                          'pending','rejected','stale','applying',
                          'restart_pending','applied','failed','rolled_back'
                      )),
    decided_by        TEXT,
    decision_reason   TEXT NOT NULL DEFAULT '',
    result_message    TEXT NOT NULL DEFAULT '',
    created_at        REAL NOT NULL,
    updated_at        REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_control_proposals_state_time
    ON control_proposals(state, created_at DESC);

CREATE TABLE IF NOT EXISTS control_proposal_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id TEXT NOT NULL REFERENCES control_proposals(proposal_id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at  REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_control_proposal_events_proposal
    ON control_proposal_events(proposal_id, id);

CREATE TABLE IF NOT EXISTS module_scheduler_jobs (
    job_id           TEXT PRIMARY KEY,
    module_name      TEXT NOT NULL,
    job_key          TEXT NOT NULL,
    handler          TEXT NOT NULL,
    run_at           REAL NOT NULL,
    interval_seconds REAL,
    jitter_seconds   REAL NOT NULL DEFAULT 0,
    backoff_json     TEXT NOT NULL DEFAULT '{}',
    payload_json     TEXT NOT NULL DEFAULT '{}',
    attempt          INTEGER NOT NULL DEFAULT 0,
    leased_until     REAL,
    lease_token      TEXT,
    last_error       TEXT,
    created_at       REAL NOT NULL,
    updated_at       REAL NOT NULL,
    UNIQUE (module_name, job_key)
);
CREATE INDEX IF NOT EXISTS idx_module_scheduler_jobs_due
    ON module_scheduler_jobs(run_at, leased_until);

"""


async def _table_exists(conn: aiosqlite.Connection, table: str) -> bool:
    async with conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ) as cur:
        return await cur.fetchone() is not None


async def _has_versioned_schema(conn: aiosqlite.Connection) -> bool:
    return await _table_exists(conn, "schema_version")


async def _current_schema_version(conn: aiosqlite.Connection) -> int:
    if not await _has_versioned_schema(conn):
        return 0
    async with conn.execute("SELECT MAX(version) FROM schema_version") as cur:
        row = await cur.fetchone()
    return int(row[0]) if row and row[0] else 0


async def _has_existing_user_tables(conn: aiosqlite.Connection) -> bool:
    async with conn.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table'
          AND name NOT LIKE 'sqlite_%'
          AND name != 'schema_version'
        LIMIT 1
        """
    ) as cur:
        return await cur.fetchone() is not None


type Migration = tuple[str, Callable[[aiosqlite.Connection], Awaitable[None]]]


_INITIAL_SCHEMA_NAME = "initial_schema"


async def _migrate_v1_to_v2(conn: aiosqlite.Connection) -> None:
    await conn.execute(
        "ALTER TABLE coding_tasks ADD COLUMN display_summary TEXT NOT NULL DEFAULT ''"
    )
    await conn.execute(
        "ALTER TABLE coding_tasks ADD COLUMN context_messages_json TEXT NOT NULL DEFAULT '[]'"
    )
    await conn.execute(
        "ALTER TABLE coding_tasks ADD COLUMN input_files_json TEXT NOT NULL DEFAULT '[]'"
    )


_MIGRATIONS: dict[int, Migration] = {
    2: ("coding_task_context_inputs", _migrate_v1_to_v2),
}


async def _record_schema_version(
    conn: aiosqlite.Connection,
    version: int,
    name: str,
) -> None:
    await conn.execute(
        """
        INSERT INTO schema_version (version, name, applied_at)
        VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        """,
        (version, name),
    )


def _require_migration(target: int) -> Migration:
    try:
        return _MIGRATIONS[target]
    except KeyError as exc:
        raise RuntimeError(f"No database migration registered for schema v{target}") from exc


async def _apply_migrations(conn: aiosqlite.Connection, current: int) -> None:
    for target in range(current + 1, SCHEMA_VERSION + 1):
        name, migrate = _require_migration(target)
        try:
            await conn.execute("BEGIN IMMEDIATE")
            await migrate(conn)
            await _record_schema_version(conn, target, name)
            await conn.commit()
        except BaseException:
            await conn.rollback()
            raise


async def _ensure_control_plane_schema(conn: aiosqlite.Connection) -> None:
    """Let pre-control-plane v1 development databases adopt the v1 tables."""
    statements = (
        """CREATE TABLE IF NOT EXISTS control_proposals (
            proposal_id TEXT PRIMARY KEY,
            module_name TEXT NOT NULL,
            action TEXT NOT NULL,
            target TEXT NOT NULL,
            summary TEXT NOT NULL,
            changes_json TEXT NOT NULL,
            actor_json TEXT NOT NULL,
            expected_revision TEXT,
            preview_json TEXT NOT NULL,
            state TEXT NOT NULL CHECK (state IN (
                'pending','rejected','stale','applying','restart_pending',
                'applied','failed','rolled_back'
            )),
            decided_by TEXT,
            decision_reason TEXT NOT NULL DEFAULT '',
            result_message TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )""",
        """CREATE INDEX IF NOT EXISTS idx_control_proposals_state_time
            ON control_proposals(state, created_at DESC)""",
        """CREATE TABLE IF NOT EXISTS control_proposal_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            proposal_id TEXT NOT NULL REFERENCES control_proposals(proposal_id) ON DELETE CASCADE,
            kind TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL
        )""",
        """CREATE INDEX IF NOT EXISTS idx_control_proposal_events_proposal
            ON control_proposal_events(proposal_id, id)""",
    )
    for statement in statements:
        await conn.execute(statement)


async def _ensure_module_runtime_schema(conn: aiosqlite.Connection) -> None:
    """Core-owned module runtime tables, adopted idempotently like the ledger."""
    await conn.execute(
        """CREATE TABLE IF NOT EXISTS module_scheduler_jobs (
            job_id           TEXT PRIMARY KEY,
            module_name      TEXT NOT NULL,
            job_key          TEXT NOT NULL,
            handler          TEXT NOT NULL,
            run_at           REAL NOT NULL,
            interval_seconds REAL,
            jitter_seconds   REAL NOT NULL DEFAULT 0,
            backoff_json     TEXT NOT NULL DEFAULT '{}',
            payload_json     TEXT NOT NULL DEFAULT '{}',
            attempt          INTEGER NOT NULL DEFAULT 0,
            leased_until     REAL,
            lease_token      TEXT,
            last_error       TEXT,
            created_at       REAL NOT NULL,
            updated_at       REAL NOT NULL,
            UNIQUE (module_name, job_key)
        )"""
    )
    await conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_module_scheduler_jobs_due
            ON module_scheduler_jobs(run_at, leased_until)"""
    )


def _sql_quote(value: str) -> str:
    """Quote a string as a SQL literal. PRAGMA key does not accept bound
    parameters, so the passphrase must be embedded; double single-quotes."""
    return "'" + value.replace("'", "''") + "'"


class Database:
    def __init__(self, path: str | Path = "data/bot.db", encryption_key: str | None = None) -> None:
        self._path = Path(path)
        # Empty/None key = plaintext sqlite3 (unchanged). A non-empty key opens
        # the DB through SQLCipher; see docs/database.md.
        self._encryption_key = encryption_key or None
        self._conn: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

    @property
    def encrypted(self) -> bool:
        """Whether this Database instance opened through the SQLCipher path."""
        return self._encryption_key is not None

    async def connect(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if self._encryption_key:
            # Lazy import: sqlcipher3 is a Linux-only dependency only required
            # when encryption is on. aiosqlite runs the connector in its worker
            # thread, so the sqlcipher connection is created and used there.
            from sqlcipher3 import dbapi2 as sqlcipher

            path = str(self._path)
            self._conn = await aiosqlite.Connection(
                lambda: sqlcipher.connect(path), iter_chunk_size=64
            )
            # sqlite3.Row rejects a sqlcipher3 cursor, so use sqlcipher's own
            # row factory; it exposes the same mapping/index access.
            self._conn.row_factory = sqlcipher.Row
            # Key the connection before any other statement touches the DB.
            await self._conn.execute(f"PRAGMA key = {_sql_quote(self._encryption_key)}")
        else:
            self._conn = await aiosqlite.connect(str(self._path))
            self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._initialize_schema()

    async def _initialize_schema(self) -> None:
        conn = self.conn
        current = await _current_schema_version(conn)

        if current > SCHEMA_VERSION:
            raise RuntimeError(
                f"Database schema v{current} is newer than supported v{SCHEMA_VERSION}"
            )

        if current == 0 and await _has_existing_user_tables(conn):
            raise RuntimeError(
                "Existing database has no schema_version; start with an empty database"
            )

        if current == 0:
            await conn.executescript(_SCHEMA_SQL)
            await _record_schema_version(conn, 1, _INITIAL_SCHEMA_NAME)
            for version in range(2, SCHEMA_VERSION + 1):
                name, _ = _require_migration(version)
                await _record_schema_version(conn, version, name)
            await conn.commit()
        elif current < SCHEMA_VERSION:
            await _apply_migrations(conn, current)

        # Keep the module ledger idempotent so older development databases can
        # adopt module support independently of the core migration history.
        await conn.execute(
            """CREATE TABLE IF NOT EXISTS module_schema_versions (
                module_name TEXT NOT NULL,
                version     INTEGER NOT NULL CHECK (version > 0),
                name        TEXT NOT NULL,
                applied_at  TEXT NOT NULL,
                PRIMARY KEY (module_name, version)
            )"""
        )
        await _ensure_control_plane_schema(conn)
        await _ensure_module_runtime_schema(conn)
        await conn.commit()

        log.info("Database ready at %s (schema v%d)", self._path, SCHEMA_VERSION)

    async def apply_module_migrations(
        self,
        module_name: str,
        migrations: tuple[tuple[str, Callable[[aiosqlite.Connection], Awaitable[None]]], ...],
    ) -> None:
        """Apply one module's ordered migrations using an independent ledger."""
        if (
            not module_name
            or len(module_name) > 64
            or not module_name[0].isalpha()
            or any(not (char.islower() or char.isdigit() or char in "_-") for char in module_name)
        ):
            raise ValueError(f"Invalid module schema name {module_name!r}")
        async with self._write_lock:
            async with self.conn.execute(
                "SELECT MAX(version) FROM module_schema_versions WHERE module_name = ?",
                (module_name,),
            ) as cursor:
                row = await cursor.fetchone()
            current = int(row[0]) if row and row[0] else 0
            if current > len(migrations):
                raise RuntimeError(
                    f"Module {module_name!r} database schema v{current} is newer than "
                    f"supported v{len(migrations)}"
                )
            for target in range(current + 1, len(migrations) + 1):
                migration_name, migrate = migrations[target - 1]
                try:
                    await self.conn.execute("BEGIN IMMEDIATE")
                    await migrate(self.conn)
                    await self.conn.execute(
                        "INSERT INTO module_schema_versions "
                        "(module_name, version, name, applied_at) "
                        "VALUES (?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))",
                        (module_name, target, migration_name),
                    )
                    await self.conn.commit()
                except BaseException:
                    await self.conn.rollback()
                    raise
        log.info("Module database ready: %s schema v%d", module_name, len(migrations))

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database not connected; call connect() first")
        return self._conn

    @asynccontextmanager
    async def write_transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """Scope a write unit: serialize, commit on success, roll back on error.

        The shared connection runs in sqlite3's implicit-transaction mode,
        where statements from concurrent tasks interleave into whatever
        transaction is open and any task's ``commit()`` finalizes it, so a
        multi-statement unit can be partially committed by a bystander, and a
        rollback could destroy a bystander's uncommitted write. EVERY writer must
        therefore go through this context manager (``_initialize_schema`` is the one
        exception: it runs at startup before any concurrency exists). With all
        writers serialized here, commit/rollback scoping is exact. ``BEGIN
        IMMEDIATE`` remains unusable on this shared connection (it would hold the
        write lock for the life of the connection, serializing every reader
        behind one writer). The lock is not reentrant: a writer must never call
        another writer; inline the statement instead.
        """
        async with self._write_lock:
            conn = self.conn
            try:
                yield conn
                await conn.commit()
            except BaseException:
                try:
                    await conn.rollback()
                except Exception:
                    log.exception("Rollback failed after write-transaction error")
                raise

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None
