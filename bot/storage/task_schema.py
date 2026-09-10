"""Schema shared by fresh databases and the scheduled-task migration."""

TASK_SCHEMA = """
CREATE TABLE IF NOT EXISTS scheduled_task_wizards (
    owner_id TEXT NOT NULL,
    guild_id TEXT NOT NULL,
    context_key TEXT NOT NULL,
    task_id TEXT REFERENCES scheduled_tasks(id) ON DELETE CASCADE,
    PRIMARY KEY(owner_id,guild_id,context_key)
);
CREATE TABLE IF NOT EXISTS scheduled_tasks (
    id TEXT PRIMARY KEY,
    guild_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    revision INTEGER NOT NULL DEFAULT 0,
    active_revision INTEGER,
    state_json TEXT NOT NULL DEFAULT '{}',
    state_generation INTEGER NOT NULL DEFAULT 0,
    next_run REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS scheduled_tasks_due ON scheduled_tasks(status, next_run);
CREATE INDEX IF NOT EXISTS scheduled_tasks_owner ON scheduled_tasks(owner_id);
CREATE TABLE IF NOT EXISTS scheduled_task_revisions (
    task_id TEXT NOT NULL REFERENCES scheduled_tasks(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL,
    definition_json TEXT NOT NULL,
    proposer_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY(task_id, revision)
);
CREATE TABLE IF NOT EXISTS scheduled_task_runs (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES scheduled_tasks(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL,
    state_generation INTEGER NOT NULL,
    scheduled_for REAL NOT NULL,
    status TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    proposed_state TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    finished_at REAL,
    UNIQUE(task_id, revision, scheduled_for)
);
CREATE UNIQUE INDEX IF NOT EXISTS scheduled_task_single_run ON scheduled_task_runs(task_id)
    WHERE status IN ('running', 'delivery');
CREATE TABLE IF NOT EXISTS scheduled_task_deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES scheduled_task_runs(id) ON DELETE CASCADE,
    channel_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    is_log INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    message_id TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    retry_at REAL NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS scheduled_task_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES scheduled_task_runs(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    description TEXT,
    data BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS scheduled_task_runner (
    id INTEGER PRIMARY KEY CHECK(id=1),
    token TEXT NOT NULL DEFAULT '',
    expires_at REAL NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO scheduled_task_runner(id) VALUES(1);
"""
