-- Frozen from the initial public release (3df6faa) so migration parity starts
-- from the oldest supported on-disk core schema.
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
    heartbeat_at                REAL NOT NULL
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

INSERT INTO schema_version (version, name, applied_at)
VALUES (1, 'initial_schema', '2026-08-25T09:30:12.000Z');

