"""Owner-only Activity conversations and their resumable presentation journal."""

DASHBOARD_SCHEMA = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_conv_source
    ON messages(conversation_id, source_id);

CREATE TABLE IF NOT EXISTS dashboard_conversations (
    id TEXT PRIMARY KEY,
    conversation_id INTEGER NOT NULL UNIQUE REFERENCES conversations(id) ON DELETE CASCADE,
    title TEXT NOT NULL DEFAULT 'New chat',
    title_edited INTEGER NOT NULL DEFAULT 0,
    parent_channel_id TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS dashboard_turns (
    id TEXT PRIMARY KEY,
    dashboard_id TEXT NOT NULL REFERENCES dashboard_conversations(id) ON DELETE CASCADE,
    request_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('accepted','running','completed','failed','cancelled','interrupted')),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(dashboard_id, request_id)
);
CREATE INDEX IF NOT EXISTS dashboard_turns_status ON dashboard_turns(dashboard_id, status);

CREATE TABLE IF NOT EXISTS dashboard_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dashboard_id TEXT NOT NULL REFERENCES dashboard_conversations(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    dedup_key TEXT,
    created_at REAL NOT NULL,
    UNIQUE(dashboard_id, dedup_key)
);
CREATE INDEX IF NOT EXISTS dashboard_events_chat ON dashboard_events(dashboard_id, id);

CREATE TABLE IF NOT EXISTS dashboard_files (
    id TEXT PRIMARY KEY,
    dashboard_id TEXT NOT NULL REFERENCES dashboard_conversations(id) ON DELETE CASCADE,
    owner_user_id TEXT NOT NULL,
    guild_id TEXT NOT NULL,
    path TEXT NOT NULL,
    filename TEXT NOT NULL,
    media_type TEXT NOT NULL,
    size INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('upload','output')),
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS dashboard_files_chat ON dashboard_files(dashboard_id, created_at);

CREATE TABLE IF NOT EXISTS dashboard_task_previews (
    dashboard_id TEXT NOT NULL REFERENCES dashboard_conversations(id) ON DELETE CASCADE,
    task_id TEXT NOT NULL REFERENCES scheduled_tasks(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL,
    PRIMARY KEY(dashboard_id,task_id,revision)
);
CREATE TABLE IF NOT EXISTS dashboard_actions (
    id TEXT PRIMARY KEY,
    dashboard_id TEXT NOT NULL REFERENCES dashboard_conversations(id) ON DELETE CASCADE,
    request_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    action TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE(dashboard_id,request_id)
);
"""
