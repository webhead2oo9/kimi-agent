# Database

The bot keeps most of its working state in a single SQLite database at `data/bot.db`. You can change the path with `DATABASE_PATH`. That one file holds everything from conversation transcripts to provider circuit cooldowns, so treat it as production state and back it up.

The current schema version is v4. If you upgrade to a newer release, the bot runs the right migrations at startup. If you try to start an older release against a newer database, the bot refuses rather than guess at unknown tables. Optional application modules own their own schemas and versions.

## Contents

- [Quick reference: what's stored](#quick-reference-whats-stored)
- [Encryption at rest](#encryption-at-rest)
- [Backing up the database](#backing-up-the-database)
- [Schema ownership](#schema-ownership)
- [Schema upgrades](#schema-upgrades)
- [Tables](#tables)
  - [Conversations and transcript](#conversations-and-transcript)
  - [People](#people)
  - [Memory and privacy](#memory-and-privacy)
  - [Operations](#operations)
- [Model, paid-tool, and bounded-tool usage](#model-paid-tool-and-bounded-tool-usage)
- [Transcript retention](#transcript-retention)
- [On-demand per-user deletion](#on-demand-per-user-deletion)

## Quick reference: what's stored

If you only have a minute, here's the shape of the database:

- One row per conversation in `conversations`. Guild chats and personal `/chat` share the table with different key formats.
- The transcript itself in `messages`, deduped by Discord message id.
- Per-user stuff in `user_preferences` (memory opt-out, privacy consent, persona override) and `blocked_users` (self-blocks and staff blocks).
- Memory and privacy: watermarks, deletion requests, video session bookkeeping, provider deletion outboxes.
- Operations: durable coding tasks, usage ledgers, the owner's chat-model selection, provider circuit cooldowns, cached image descriptions, and the schema version.

The sections below cover encryption, backups, schema upgrades, retention, and `/privacy` deletion in more depth.

## Encryption at rest

You can encrypt the database with [SQLCipher](https://www.zetetic.net/sqlcipher/). It's off by default. When it's on, the encryption is transparent above the page layer: schema initialization, WAL mode, and every store behave the same way.

To turn it on:

- Set `DATABASE_ENCRYPTION_KEY` to a passphrase. Leave it empty to stay on plaintext `sqlite3`.
- The Linux-only `sqlcipher3-binary` dependency provides the engine (bundled SQLCipher, no system library). It loads lazily, only when a key is set. Dev and CI interpreters don't need it.
- `Database.connect()` opens the file through `sqlcipher3` and runs `PRAGMA key` before any other statement. Because `sqlite3.Row` rejects a sqlcipher cursor, the encrypted path uses `sqlcipher3.Row`, which has the same access API.
- The key belongs in the environment or an untracked dotenv file, never in the repo. **If you lose or change the key, the database is permanently unreadable. There is no recovery.** Generate one with `python3 -c "import secrets; print(secrets.token_urlsafe(48))"`.

Setting the key on a plaintext database doesn't encrypt it in place, and a keyed connection can't read a plaintext file. You have to convert it offline with SQLCipher's `sqlcipher_export`. Stop the service first:

```sql
-- sqlcipher data/bot.db
ATTACH DATABASE 'data/bot.enc.db' AS enc KEY 'YOUR_KEY';
SELECT sqlcipher_export('enc');
DETACH DATABASE enc;
```

Then swap `data/bot.db` for `data/bot.enc.db`. Move the `-wal` and `-shm` sidecars out of the way first, set `DATABASE_ENCRYPTION_KEY`, and restart. Keep the plaintext backup until you've confirmed the encrypted DB opens and reads.

## Backing up the database

The database is one main file plus its `-wal` and `-shm` sidecars. Back them up together, or you'll get a torn transaction.

Two ways to do it safely:

- **Stop the bot, then copy the three files.** This is the simplest option and always consistent.
- **Use SQLite's online backup API while the bot is running.** WAL mode lets readers see a consistent snapshot. From the `sqlite3` CLI: `sqlite3 data/bot.db ".backup data/bot.backup.db"`.

Whatever you do, test the backup. Open it with the bot's environment, run `/usage`, send a quick message, and confirm the rows look right. A backup you've never restored is a backup you don't have.

Schedule backups with the same cadence as the rest of your state. A daily snapshot plus a few days of rotation is the typical minimum. The bot doesn't run its own backup schedule.

## Schema ownership

- `storage/db.py` owns the current schema baseline and `SCHEMA_VERSION`.
- `_SCHEMA_SQL` builds the complete core schema for an empty database. The ordered `_MIGRATIONS` registry upgrades supported lower versions.
- The `schema_version` table tracks which schema changes have been applied and when.
- **`module_scheduler_runner`** is the module scheduler's singleton lease: one row (`token`, `leased_until`) that the running process renews every tick and releases on close. A second process against the same file pauses instead of running jobs.
- **`module_scheduler_jobs`** stores durable module jobs (`module_name`, `job_key`, `handler`, `run_at`, `interval_seconds`, lease columns, attempt and last error). Core owns it. Modules reach it through `ctx.scheduler`.
- **`config_proposals`** stores guild-scoped fragment proposals, including the proposed content hash and exact pre-change baseline needed for conflict detection and rollback. The runtime never reads `control_proposals` or `control_proposal_events`, and v4 doesn't create them.
- `module_schema_versions` tracks the latest applied version for every module that has run migrations. Module migrations run transactionally before module startup, and module tables aren't part of the core baseline.
- Stores under `storage/` can assume `Database.connect()` has already brought the database to the current supported schema.

## Schema upgrades

`Database.connect()` creates the current schema for an empty database and writes one `schema_version` row per version. A database on a supported lower version runs each registered migration in order.

The current registry:

| Version | Migration name | What it adds |
|---:|---|---|
| v1 → v2 | `coding_task_context_inputs` | Durable worker input metadata for coding tasks. |
| v2 → v3 | `video_understanding_sessions` | Video session bookkeeping, interactions, provider files, and deletion outboxes. |
| v3 → v4 | `provider_circuit_breakers` | Persistent provider circuit breaker. |

Each version has a permanent name in `schema_version`. An unregistered version raises at startup whether you're creating fresh or upgrading. A migration and its version record share one transaction, so a failure leaves the schema and version stamp unchanged.

Migrations only move forward and only run at startup. Back up before you upgrade a real instance. Rolling back means restoring that older release's database backup; an older process can't open a newer schema safely. The bot rejects a non-empty database without a schema stamp, and rejects any database stamped above its supported version.

Before moving or restoring the database, stop the bot and copy the main file with its `-wal` and `-shm` sidecars, or use SQLite's backup API. Don't edit `schema_version` by hand; the version ledger and the actual schema have to agree.

For local development, just delete the database when you don't care about its contents.

## Tables

Every table below is in the current schema. The columns in parentheses are the ones worth knowing about; `storage/db.py` is the full source.

### Conversations and transcript

- **`conversations`** holds one row per rooted conversation, keyed by its logical `key`. Guild conversations record the Discord message that started them in `root_discord_message_id`. Personal user-app chat uses `userchat:<user_id>` as a stable key and stores the first interaction or DM id in that field. `owner_user_id` records who rooted the conversation, which `/privacy` deletion relies on. `access_scope` is `channel_shared` or `owner_only`: a shared root lets another channel member continue a bot reply, while an owner-only root requires an exact requester match and fails closed on missing or mismatched ownership.

  `conversations.eval_cursor` exists but nothing in the runtime reads it. It's reserved for the offline eval harness.

- **`messages`** is the per-root transcript, with rows for persisted user input and assistant output. Guild message rows use the real Discord message ids; `/chat` rows use identifiers derived from the interaction. A unique `(conversation_id, discord_message_id)` index dedups it. SQLite treats NULLs as distinct, so rows without a Discord id never collide. `user_id` is indexed because counting a member's stored messages for new-user onboarding would otherwise scan the whole table on every mention. A row may also carry a machine-written description of its images as a text part in `message_data`. The description outlives the image parts, since a conversation only keeps its ten newest images and evicts the rest.

- **`message_contexts`** maps a Discord message id back to its root conversation, so a reply resumes the right transcript after a restart.

- **`conversation_activated_tools`** remembers which searchable tools `browse_tools` has activated in a root. Once a tool is loaded, it stays available across turns.

- **`thread_conversations`** enrolls bot-created handoff threads. Every message in a mapped thread continues the mapped root. `auto_respond` is the thread's mode: `1` means no mention needed, `0` means the thread is paused and back on the ordinary channel contract while staying mapped. `creator_user_id` is the durable initiator used to authorize close, pause, and resume; a row without one falls back to STAFF or Discord's Manage Threads permission.

### People

- **`user_preferences`** holds one row per user: memory opt-out, privacy consent and its timestamp, and a compiled persona override.
- **`blocked_users`** holds member self-blocks and staff blocks. The runtime checks it before any transcript write, lock, tool, or provider call.

### Memory and privacy

- **`auto_retain_watermarks`** records the highest `messages.id` flushed to Hindsight per (conversation, user). Advancing a watermark without retaining is how opt-out, trivial-content, and forget-me slices are permanently skipped.
- **`privacy_deletion_requests`** holds the durable authorization for a confirmed `/privacy` deletion: one coalesced row per user with the widest requested scope, a generation counter, and a unique token. The token stops a worker holding stale authorization from completing a superseded request after a crash or race. It contains no message content.
- **`user_memory_bank_states`** is conservative local knowledge that a user's remote Hindsight bank may exist. The flag is written *before* any create or retain attempt and cleared only after a confirmed delete, so disabling the backend can't hide a bank from the privacy workflow.
- **`video_sessions`** holds one actor/root/guild-scoped specialist session: opaque local handle, source kind, safe display filename/relative locator and byte size for uploads (or canonical YouTube URL/video id), model, latest Gemini Interaction id, count, and expiry. It stores no video bytes, provider capability URLs, questions, or answers.
- **`video_interactions`** records every Gemini Interaction id in each session, so deleting only the latest turn can't strand provider state.
- **`video_provider_files`** reserves each client-chosen Gemini Files API name before upload and later associates it with one session. Unattached rows let startup and periodic cleanup recover from a crash between upload and session creation; the cleanup interval is `TRANSCRIPT_RETENTION_SWEEP_INTERVAL_SECONDS` (one hour by default).
- **`video_interaction_deletions`** and **`video_provider_file_deletions`** are content-free provider deletion outboxes. Triggers fill them before local session and cascade deletion; Interaction deletion gates the backing File delete. Failed attempts use a capped one-minute-to-six-hour exponential delay. Retry-ready rows sort ahead of delayed failures, and the attempt count never discards privacy cleanup metadata.

### Operations

- **`coding_tasks`** holds the durable objective, owner/root/workspace scope, deadline, status, plan, checkpoint, Discord delivery ids, terminal result, conversation context and input-file references supplied to the worker, and the short-lived handoff hold that prevents execution before reply routing is settled.
- **`coding_task_events`** is the append-only task journal for steering, milestones, checkpoints, recovery, cancellation, and terminal transitions.
- **`coding_command_jobs`** records managed sandbox job requests and bounded terminal stdout/stderr. Active jobs become `interrupted` after a restart, so an agent can't unknowingly replay a command whose outcome is uncertain.
- **`usage_ledger`** holds one row per completed model request. See [Model, paid-tool, and bounded-tool usage](#model-paid-tool-and-bounded-tool-usage).
- **`paid_usage_ledger`** holds one row per non-LLM tool backend that actually charged money. Same section.
- **`usage_markers`** stores zero-cost per-user counters for bounded tool surfaces. Code execution uses it for the rolling network-run budget. Rows hold attribution, surface/operation, units, and time, never code or tool output.
- **`model_selection`** is a singleton holding the owner-selected global chat model, so a `/models` switch survives a restart. NULL means the normal `config/models.yaml` role and scope routing applies.
- **`provider_circuits`** stores active model- or account-scoped provider cooldowns, including the normalized reason, optional status/provider code, and retry time. Persisting them prevents a restart from immediately retrying a provider that is still unhealthy. Successful recovery or an owner reset removes the affected rows.
- **`image_distillations`** caches visual descriptions for text-only chat models. The key covers the image set, the vision model, and the prompt version. The cache is scoped to a single conversation so descriptions never cross a privacy or guild boundary, and deleting the parent conversation removes them by cascade. It's just a cache; the durable copy lives on the message row it describes.
- **`schema_version`** records the version the database has reached.

## Model, paid-tool, and bounded-tool usage

Every completed model request writes one row to `usage_ledger`. A single Discord interaction may make several model requests: the reply, compaction summaries, image descriptions, persona compilation. They share a `turn_id` so they can be reported as one interaction.

Each row records the user, channel, server, model, purpose, token counts, timestamp, and an optional estimated cost. It does not store prompts or model responses. Cost estimates use the `pricing` rates for the model in `config/models.yaml`; if those rates aren't configured, the usage stays unpriced. The fixed Gemini video specialist is the exception: its tool attaches Google's published, date-scheduled Gemini 3.7 Flash estimate directly, without adding that specialist to chat routing.

Rows save as each model request finishes, so if a later request fails or the interaction times out, usage from the completed requests is still recorded. The `/usage` command summarizes this data over time.

Tool providers that bill separately write one row per charged backend to `paid_usage_ledger`. A blended search creates an Exa row and a Brave row when both bill. Rows store attribution and dollars, not queries or results. A provider-reported zero creates no row, and an absent configured or reported price is never guessed. Ledger failures never fail the tool call, so recorded tool spend is an attributable floor; the vendor dashboard remains the source of truth.

`/usage` shows the estimated cost of each window and breaks paid-tool spend out into its own column whenever a window has any. Members can view their own usage. Staff can also view another member's usage and server totals.

`usage_markers` stays out of those totals on purpose: its rows count how often something was used, not what it cost. The bot prunes markers outside the eight-day storage window, which covers the seven-day reporting window.

## Transcript retention

Conversation transcripts live for 30 days by default. The retention clock measures a conversation's most recent activity, so an active channel or thread isn't removed mid-use. Set `TRANSCRIPT_RETENTION_DAYS` to `0` to keep transcripts forever.

A background task removes expired conversations in bounded batches. Removing a conversation also removes its message-routing records, activated tools, managed thread state, memory-retention markers, cached image descriptions, and local video sessions. Triggers first move every known Gemini Interaction and Files API resource name into independent provider-deletion outboxes. Each batch is removed in one database transaction, so a failure can't leave a partially deleted transcript behind.

Usage and cost records, short-lived `usage_markers`, provider circuit cooldowns, and long-term Hindsight memory all sit outside transcript retention and keep their own lifecycles. See [privacy.md](privacy.md#retention-and-deletion) for the complete list of stored data and retention periods.

## On-demand per-user deletion

The `/privacy` command lets a user delete their data before it expires. This section walks through what happens.

### What gets removed

**Delete my data** removes entire conversations the user started. In conversations shared with other members, it removes only that user's messages and routing records, leaving other participants' messages and the bot's replies in place. It also clears cached image descriptions derived from the deleted messages and removes the user's initiator marker from any surviving managed threads.

Starting a conversation makes the user its owner for deletion purposes. It doesn't make an ordinary channel conversation private. Private `owner_only` conversations require an exact owner match whenever they're reopened, and missing or mismatched ownership is rejected.

Full deletion also removes every video session initiated by that user, including sessions in a shared root that survives. The bot attempts provider deletion for the complete Gemini Interaction chain plus any backing Files API upload. A provider failure leaves the content-free outboxes pending for retry, but doesn't keep the privacy request or the user activity barrier open after local deletion succeeds.

### What stays

The deletion removes the transcript, routing, activated tools, managed-thread markers, cached image descriptions, and local video sessions. It does **not** remove:

- Usage ledgers. The history of model and paid-tool calls is preserved as audit data.
- Active rate-limit markers (`usage_markers`). A capacity limit anyone could reset by deleting their data is not a limit. These markers age out on their own, pruned after eight days.
- Long-term Hindsight memory. That's deleted separately as part of the wider privacy workflow.

If the memory service is unavailable and local tracking says a user may have stored memory, the request stays pending rather than reporting a successful deletion that can't be confirmed.

### How it runs

The bot records the deletion request before it begins and blocks new activity for that user while the request is in progress. In-flight turns that could write to affected conversations are allowed to finish first. If the bot stops or a dependency fails, the request stays pending and resumes after restart. Repeating a deletion is safe.

Deleting SQLite transcript data leaves both usage ledgers alone, and it leaves active rate-limit markers alone too.