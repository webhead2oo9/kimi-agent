# Database

The bot uses async SQLite through `storage/db.py`, with the database path set by
`DATABASE_PATH` (default `data/bot.db`). Treat the database as production
state: it holds rooted conversations, Discord reply routing, user preferences,
privacy consent, moderation blocks, the operator's global chat-model selection,
managed-thread state, LLM and paid-tool usage ledgers, durable authorization for
confirmed privacy deletions, cached image distillations, stateful video-session
handles and provider-deletion retries, memory auto-retain watermarks, and
per-user Hindsight bank tracking.

The core database is schema v3. A newer core database is left alone and
rejected, so an older bot cannot accidentally use it. Optional application
modules own their own independent schemas and versions.

## Encryption at rest

The database can be encrypted at rest with
[SQLCipher](https://www.zetetic.net/sqlcipher/). It is off by default and fully
transparent above the page layer, so schema initialization, WAL mode, and every
store behave identically whether encryption is on or off.

- Set `DATABASE_ENCRYPTION_KEY` to a passphrase to enable it. Leaving it empty
  (the default) keeps the plaintext `sqlite3` path unchanged. The Linux-only
  `sqlcipher3-binary` dependency supplies the engine (bundled SQLCipher, no
  system library). It is imported lazily and only when a key is set, so the
  dev/CI interpreter does not need it.
- `Database.connect()` opens the file through `sqlcipher3` and issues
  `PRAGMA key` before any other statement. Because `sqlite3.Row` rejects a
  sqlcipher cursor, the encrypted path uses `sqlcipher3.Row`, which has the same
  access API.
- The key lives in the environment or an untracked dotenv file, never in the
  repo. **Losing or changing it makes the database permanently unreadable, with
  no recovery.** Generate one with
  `uv run python -c "import secrets; print(secrets.token_urlsafe(48))"`.

Enabling encryption on an existing **plaintext** database does not encrypt it
in place, and a keyed connection cannot read a plaintext file. Convert it once,
offline, with SQLCipher's `sqlcipher_export` while the service is stopped:

```sql
-- sqlcipher data/bot.db
ATTACH DATABASE 'data/bot.enc.db' AS enc KEY 'YOUR_KEY';
SELECT sqlcipher_export('enc');
DETACH DATABASE enc;
```

Then replace `data/bot.db` with `data/bot.enc.db` (move the `-wal`/`-shm`
sidecars out of the way first), set `DATABASE_ENCRYPTION_KEY`, and restart.
Keep the plaintext backup until you have verified that the encrypted DB opens
and reads.

## Schema ownership

- `storage/db.py` owns the current schema baseline and `SCHEMA_VERSION`.
- `_SCHEMA_SQL` creates the complete baseline for a new database.
- The `schema_version` table records which schema changes have been applied and
  when.
- **`module_scheduler_jobs`** holds durable module jobs (`module_name`,
  `job_key`, `handler`, `run_at`, `interval_seconds`, lease columns,
  attempt and last error). Core owns it; modules reach it only through
  `ctx.scheduler`.
- **`config_proposals`** holds guild-scoped fragment proposals, including the
  proposed content hash and exact pre-change baseline needed for conflict
  detection and rollback. Old `control_proposals` and
  `control_proposal_events` tables may remain in upgraded databases but are
  orphaned and never read; fresh databases do not create them.
- `module_schema_versions` records the latest applied version for each active
  application module. Module migrations run transactionally before module
  startup, and module tables are not part of the core baseline.
- Stores under `storage/` can assume `Database.connect()` has already brought
  the database to the current supported schema.

## Tables

Every table below is in the current schema. The columns named in parentheses
are the ones worth knowing about, not the full definition; `storage/db.py` is
authoritative.

### Conversations and transcript

- **`conversations`** is one row per rooted conversation, keyed by the Discord
  message that started it (`guild_id`, `channel_id`, `thread_id`,
  `root_discord_message_id`). `owner_user_id` records who rooted it, which
  privacy deletion relies on. `access_scope` is `channel_shared` or
  `owner_only`: a shared root lets another channel member continue a bot reply,
  while an owner-only root requires an exact requester match and fails closed
  on missing or mismatched ownership.
- **`messages`** is the per-root transcript, one row per real Discord message.
  A unique `(conversation_id, discord_message_id)` index dedups it; since
  SQLite treats NULLs as distinct, rows without a Discord id never collide.
  `user_id` is indexed because counting a member's prior messages for new-user
  onboarding would otherwise scan the whole table on every mention. A user row
  may also carry a machine-written description of its images as a text part in
  `message_data`. That description deliberately outlives the image parts
  themselves, because a conversation keeps only its ten newest images and
  evicts the rest.
- **`message_contexts`** maps a Discord message id back to its root
  conversation, so a reply resumes the right transcript after a restart.
- **`conversation_activated_tools`** remembers which searchable tools
  `browse_tools` has activated in a root, so a loaded tool stays available
  across turns.
- **`thread_conversations`** enrolls bot-created handoff threads. Every message
  in a mapped thread continues the mapped root. `auto_respond` is the thread's
  mode: 1 means no mention is needed, 0 means the thread is paused and back on
  the ordinary channel contract while staying mapped. `creator_user_id` is the
  durable initiator used to authorize close, pause, and resume; a row without
  one falls back to STAFF or Discord's Manage Threads permission.

`conversations.eval_cursor` exists, but nothing in the runtime reads it.

### People

- **`user_preferences`** holds one row per user: memory opt-out, privacy
  consent and its timestamp, and a compiled persona override.
- **`blocked_users`** holds member self-blocks and staff blocks. It is checked
  before any transcript write, lock, tool, or provider call.

### Memory and privacy

- **`auto_retain_watermarks`** records the highest `messages.id` already
  flushed to Hindsight per (conversation, user). Advancing a watermark without
  retaining is how opt-out, trivial-content, and forget-me slices are
  permanently skipped.
- **`privacy_deletion_requests`** is the durable authorization for a confirmed
  `/privacy` deletion: one coalesced row per user holding the widest requested
  scope, a generation counter, and a unique token. The token is what stops an
  older worker from completing a newer or wider request after a crash or race.
  It contains no message content.
- **`user_memory_bank_states`** is conservative local knowledge that a user's
  remote Hindsight bank may exist. The flag is written *before* any create or
  retain attempt and cleared only after a confirmed delete, so disabling the
  backend cannot hide a bank from the privacy workflow.
- **`video_sessions`** holds one actor/root/guild-scoped specialist session:
  opaque local handle, source kind, safe display filename/relative locator and
  byte size for uploads (or canonical YouTube URL/video id), model, latest
  Gemini Interaction id, count, and expiry. It stores no video bytes, provider
  capability URLs, questions, or answers.
- **`video_interactions`** records every Gemini Interaction id in each session,
  so deleting only the latest turn cannot strand earlier provider state.
- **`video_provider_files`** reserves each client-chosen Gemini Files API name
  before upload and later associates it with one session. Unattached rows let
  startup/hourly cleanup recover a crash between upload and session creation.
- **`video_interaction_deletions`** and **`video_provider_file_deletions`** are
  content-free provider deletion outboxes. Triggers fill them before local
  session/cascade deletion; Interaction deletion gates the backing File delete.
  Failed attempts use a capped one-minute-to-six-hour exponential delay;
  retry-ready rows sort ahead of delayed failures, and attempt count never
  discards privacy cleanup metadata.

### Operations

- **`coding_tasks`** holds the durable objective, owner/root/workspace scope,
  deadline, status, plan, checkpoint, Discord delivery ids, terminal result,
  conversation context and input-file references supplied to the worker, and
  the short-lived handoff hold that prevents execution before reply routing is
  settled.
- **`coding_task_events`** is the append-only task journal for steering,
  milestones, checkpoints, recovery, cancellation, and terminal transitions.
- **`coding_command_jobs`** records managed sandbox job requests and bounded
  terminal stdout/stderr. Active jobs become `interrupted` after a restart, so
  an agent cannot unknowingly replay a command whose outcome is uncertain.
- **`usage_ledger`** is one row per completed model request; see Model usage
  and cost below.
- **`paid_usage_ledger`** is one row per non-LLM tool backend that actually
  charged money; see Model and paid-tool usage below.
- **`usage_markers`** stores zero-cost per-user counters for bounded tool
  surfaces. Code execution uses it for the rolling network-run budget. Rows hold
  attribution, surface/operation, units, and time, never code or tool output.
- **`model_selection`** is a singleton holding the owner-selected global chat
  model, so a `/models` switch survives a restart. NULL means the normal
  `config/models.yaml` role and scope routing applies.
- **`image_distillations`** caches visual descriptions for text-only chat
  models. The key covers the image set, the vision model, and the prompt
  version. The cache is scoped to a single conversation so descriptions never
  cross a privacy or guild boundary, and deleting the parent conversation
  removes them by cascade. It is only a cache: the durable copy lives on the
  message row it describes.
- **`schema_version`** records the version the database has reached.

The coding tables began in schema v1. Schema v2 adds the task's display summary,
bounded conversation context, and input-file metadata. Schema v3 adds the
stateful video-session tables and provider deletion outbox.

## Model, paid-tool, and bounded-tool usage

Each completed request to an AI model adds one row to `usage_ledger`. A single
Discord interaction may make several model requests, such as generating the
reply, summarizing older context, describing an image, or compiling a persona,
and those rows share a `turn_id` so they can be reported as one interaction.

Each row records the user, channel, server, model, purpose, token counts,
timestamp, and an optional estimated cost. It does not store prompts or model
responses. Cost estimates normally use the `pricing` rates for the model in
`config/models.yaml`; when the required rates are not configured, the usage
remains unpriced. The fixed Gemini video specialist is the exception: its tool
attaches Google's published, date-scheduled Gemini 3.7 Flash estimate directly,
without adding that specialist to chat routing.

Rows are saved as each model request finishes, so if a later request fails or
the overall interaction times out, usage from the completed requests is still
recorded. The `/usage` command summarizes this data over time.

Separately billed tool providers write one row per charged backend to
`paid_usage_ledger`, so a blended search creates an Exa row and a Brave row
when both bill. Rows store attribution and dollars, not queries or results. A
provider-reported zero creates no row, and an absent configured or reported
price is never guessed. Ledger failures never fail the tool call, which means
recorded tool spend is an attributable floor; the vendor dashboard remains
authoritative.

`/usage` reports the estimated cost of each window and breaks paid-tool spend
out into its own column whenever a window has any. Members can view their own
usage, while staff can also view another member's usage and server totals.

`usage_markers` stays out of those totals on purpose: its rows count how often
something was used, not what it cost. Old markers are pruned whenever a new one
is written, so an active deployment never carries much more than the eight days
the seven-day window needs.

## Transcript retention

Conversation transcripts are kept for 30 days by default. The retention period
is measured from a conversation's most recent activity, so an active channel or
thread conversation is not removed midway through use. Set
`TRANSCRIPT_RETENTION_DAYS` to `0` to keep transcripts indefinitely.

A background task removes expired conversations in bounded batches. Removing a
conversation also removes its message-routing records, activated tools,
managed thread state, memory-retention markers, cached image descriptions, and
local video sessions. Triggers first move every known Gemini Interaction and
Files API resource name into independent provider-deletion outboxes.
Each batch is removed in one database transaction, so a failure cannot leave a
partially deleted transcript behind.

Three things sit outside that schedule and keep their own retention rules: the
usage and cost records in `usage_ledger` and `paid_usage_ledger`, the
short-lived rows in `usage_markers`, and long-term Hindsight memory. See
[Privacy](privacy.md#retention-and-deletion) for the complete list of stored
data and retention periods.

### On-demand per-user deletion

The `/privacy` command lets a user delete their data before it expires.
**Delete my data** removes entire conversations they started. In conversations
shared with other members, it removes only that user's messages and routing
records, leaving other participants' messages and the bot's replies in place.
It also clears cached image descriptions that may have been derived from the
deleted messages and removes the user's initiator marker from any surviving
managed threads.

Starting a conversation makes the user its owner for deletion purposes; it does
not make an ordinary channel conversation private. Private `owner_only`
conversations require an exact owner match whenever they are reopened, and
missing or mismatched ownership is rejected.

The bot records a deletion request before it begins and prevents new activity
for that user while the request is in progress. Already-running turns that
could write to affected conversations are allowed to finish before deletion
starts. If the bot stops or a dependency fails, the request remains pending and
resumes after restart, and repeating a deletion is safe.

Full deletion also removes every video session initiated by that user, including
sessions in a shared root that survives, and attempts provider deletion for the
complete Gemini Interaction chain plus any backing Files API upload. A provider
failure leaves the content-free outboxes pending for retry, but does not keep
the privacy request or user activity barrier open after local deletion succeeds.

Deleting SQLite transcript data leaves both usage ledgers alone, and it leaves
active rate-limit markers alone too: a capacity limit anyone can reset by
deleting their data is not a limit. Those markers age out on their own, pruned
once they are more than eight days old. Long-term Hindsight memory is deleted
separately as part of the wider privacy workflow. If the memory service is
unavailable and local tracking says a user may have stored memory, the request
remains pending rather than reporting a successful deletion that cannot be
confirmed.

## Schema upgrades

`Database.connect()` creates the current schema for a new database and records
one `schema_version` row per version, so a fresh database reports the same
history as one that was upgraded step by step. When it opens an older database,
it walks the registered migrations from the stored version up to
`SCHEMA_VERSION`. Each migration and its version record are committed together,
so a failed migration leaves the database at its previous version.

Schema v2, `coding_task_context_inputs`, adds durable context and input metadata
to coding tasks. Schema v3, `video_understanding_sessions`, adds the video
session and deletion-outbox tables. Future changes add
an entry keyed by the version they produce, paired with a permanent name for
the history row:

```python
async def _migrate_v3_to_v4(conn: aiosqlite.Connection) -> None:
    await conn.execute("ALTER TABLE conversations ADD COLUMN locale TEXT")


_MIGRATIONS: dict[int, Migration] = {
    2: ("coding_task_context_inputs", _migrate_v1_to_v2),
    3: ("video_understanding_sessions", _migrate_v2_to_v3),
    4: ("conversation_locale", _migrate_v3_to_v4),
}
```

`SCHEMA_VERSION` moves to `4` in the same change. Leaving a version in the
range unregistered raises at startup on both the fresh and the upgrade path,
rather than creating a database that claims a version nothing built. A
migration runs inside its own transaction, so it only has to do its own work;
recording the version and committing are handled for it.

Migrations only move forward and run automatically at startup. Before upgrading
a real instance, take a WAL-consistent backup. If you need to return to an older
release, restore its matching database backup too; the old release cannot open
the newer schema safely. The bot rejects a non-empty database without a schema
stamp, and any database stamped newer than it supports.

Before moving or restoring the database, stop the bot and copy the main file
with its `-wal` and `-shm` sidecars, or use SQLite's backup API. Do not change
`schema_version` by hand; the history and the actual schema must agree.

A disposable local development database can simply be deleted when its
contents are not needed.
