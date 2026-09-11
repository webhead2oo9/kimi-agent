"""Outbox for module follow-ups to confirmed scheduled-task publications."""

TASK_RESULT_SCHEMA = """
CREATE TABLE scheduled_result_subscriptions (
    module TEXT NOT NULL,
    name TEXT NOT NULL,
    guild_id TEXT NOT NULL,
    PRIMARY KEY(module,name,guild_id)
);
CREATE TABLE scheduled_result_notifications (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES scheduled_task_runs(id) ON DELETE CASCADE,
    module TEXT NOT NULL,
    name TEXT NOT NULL,
    guild_id TEXT NOT NULL,
    published_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    retry_at REAL NOT NULL DEFAULT 0,
    lease_token TEXT NOT NULL DEFAULT '',
    lease_until REAL NOT NULL DEFAULT 0,
    detail TEXT NOT NULL DEFAULT '',
    acknowledged_at REAL,
    FOREIGN KEY(module,name,guild_id)
        REFERENCES scheduled_result_subscriptions(module,name,guild_id) ON DELETE CASCADE,
    UNIQUE(run_id,module,name,guild_id)
);
CREATE INDEX scheduled_result_due ON scheduled_result_notifications(status,retry_at,lease_until);
"""
