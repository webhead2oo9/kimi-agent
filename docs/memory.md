# Memory

Long-term memory is optional and backed by [Hindsight](https://github.com/vectorize-io/hindsight). The `/memory` slash command is registered whether or not a backend is configured, since its opt-in/opt-out state lives in SQLite. When `HINDSIGHT_URL` is set and the startup probe succeeds, the bot also wires up automatic recall for the current user, an explicit "remember this" tool, and the community memory tools. Background auto-retention needs `MEMORY_AUTO_RETAIN_ENABLED` on top of that.

## Runtime wiring

Memory is owned by `app/memory.py:MemoryManager`. `build_app` always creates the manager, and the manager creates a `MemoryClient` (a wrapper around the Hindsight SDK) only when `HINDSIGHT_URL` is set. Whether the Hindsight-backed tools become available is decided during READY initialization in `app/lifecycle.py`, once the database is open:

- `PreferenceStore` reads and writes the `user_preferences` SQLite table.
- `register_memory_command(...)` registers the `/memory` slash-command group.
- `ensure_global_banks(...)` creates the globally-shared `bot-skills` Hindsight bank if needed; this doubles as the readiness probe. Per-guild community banks are created lazily on first use, not at startup.
- `MemoryManager.ensure_ready(...)` confirms the shared Hindsight banks, then registers memory tools into the application-owned `ToolRegistry`.
- `init_community_tools(registry, memory_client)` registers `recall_community`, `reflect_community`, and the staff-only `teach` tool, and only after those shared banks are confirmed.
- `init_user_memory_tools(registry, memory_client, recall_types=settings.user_memory_recall_types)` registers `recall_user` and `reflect_user`, again only after those shared banks are confirmed.
- `init_user_memory_write_tools(registry, ...)` registers `remember_user_memory` once the SQLite conversation store exists.
- When `MEMORY_AUTO_RETAIN_ENABLED` is set, READY initialization also starts the auto-retain sweeper task (see Automatic Retention below), and shutdown cancels it before tearing down memory and database resources.

If `HINDSIGHT_URL` is empty, or if startup cannot initialize the shared Hindsight banks, the bot runs without Hindsight-backed tools, automatic recall, explicit retention, or bank deletion. The `/privacy` memory-delete path can still disable future memory in SQLite when no Hindsight client exists. If the durable local bank-state flag says that the user's bank may already exist, however, the confirmed deletion stays pending until Hindsight is available to confirm the bank is gone.

User memory is **default-on, opt-out** whenever the backend is available. A missing `user_preferences` row means memory is enabled. `/memory opt-out` disables future recall and retention, and `/memory opt-in` re-enables it after an opt-out. This per-user default is separate from `MEMORY_AUTO_RETAIN_ENABLED`, which controls whether the deployment runs the background transcript sweeper at all.

## Banks

- User banks use `user:{discord_id}` and are created on demand by `ensure_user_bank(...)`, which is called from the responding turn when a memory-enabled user talks to the bot, and from the auto-retain flusher.
- Before any user-bank create or retain reaches Hindsight, `MemoryClient` records `user_memory_bank_states.may_exist = 1` in SQLite. That record is cleared only by a confirmed whole-bank delete, so disabling the backend cannot hide a bank from the privacy workflow. A user with no tracking row has no locally known bank.
- Bank personality is applied at create time through the backend `/config` PATCH, not the bank-create PUT. `MemoryClient.create_bank` registers the bank shell, then `update_bank_config(...)` sets `reflect_mission` (recall/reflect framing), `retain_mission` + `retain_extraction_mode` (what auto-extraction keeps), `observations_mission` (consolidation/dedup), and disposition. The backend reads these fields from `/config`, which is why bank creation and configuration are separate calls. Per-bank configs live in `memory/banks.py` (`USER_CONFIG`/`COMMUNITY_CONFIG`/`SKILLS_CONFIG`, mapped onto `create_bank` by `bank_config_kwargs`). The user retain mission is selective: durable first-party facts only, ignoring transient, roleplay, and assistant-side content, and excluding sensitive attributes unless the user asks. See the capture-vs-noise caveat under Automatic Retention.
- Community memory is keyed per guild: `community_bank_id(guild_id)` resolves to `community:{guild_id}`, created lazily by `ensure_community_bank(...)` on the first teach or recall. There is no cross-guild community recall, so a fact taught in one server is invisible in another. Off-guild contexts (no `guild_id`) resolve to `None` and skip community reads and writes entirely.
- The bot skills bank is `bot-skills`. It is global (operator/bot-authored, not sharded by guild), initialized with `ensure_global_banks`, and reserved for the bot's procedural skill memory.

`memory.banks` keeps an in-process `_initialized_banks` cache. Bank deletion paths call `forget_initialized_bank(...)` so that a later turn can recreate a deleted bank if memory is re-enabled and new writes arrive.

## Per-guild scoping

A user has one bank across all guilds, but its memories are scoped by tag so that one community's conversations never surface in another:

- **Writes** tag their scope. Conversation-derived auto-retain memory is tagged `guild:{guild_id}` (plus `source:auto_retain`, `scope:user`). Auto-retain from the personal `userchat:` root is instead tagged `scope:global`. Explicit first-party facts written by `remember_user_memory` are also tagged `scope:global`, because personal-root memories and explicit first-party facts are meant to apply everywhere.
- **Reads** filter by tag. Every recall or reflect over a user bank passes `tags=["scope:global", "guild:{current_guild}"]` with `tags_match="any"`, which yields the user's global facts plus this guild's memory while excluding other guilds' conversation memory. Off-guild contexts pass only `["scope:global"]`. This threads through automatic recall (`memory/recall.py`, guild from the turn) and the `recall_user`/`reflect_user` tools (guild from `ctx.guild_id`).
- Because consolidated `observation`-layer memories inherit the tags of their source retain call, the filter works at the layer recall actually reads. Observations carry their source retain tags: `source:auto_retain` on user banks and `source:taught,topic:…` on community. `tags_match="any"` would include untagged facts, so it is safe only because every user-bank write path tags its scope; there are no untagged user facts to leak. A guild-less auto-retain slice outside the personal `userchat:` root gets no `guild:` or `scope:global` tag and is therefore never recalled, which fails closed.

## Responding-turn recall

Before `run_conversation`, `agent/turn.py:prepare_turn` calls `recall_current_user_context(...)` for the current Discord user.

The automatic recall flow is:

1. Return nothing if `memory_client` or `PreferenceStore` is unavailable.
2. Check `PreferenceStore.is_memory_enabled(user_id)`.
3. Compose a query from the current user message plus up to two recent non-tool context turns.
4. Strip embedded `<hindsight_memories>` and `<relevant_memories>` blocks from the query text.
5. Truncate the recall query to `DEFAULT_USER_RECALL_MAX_QUERY_CHARS` (800).
6. Recall only from `user:{discord_id}` with `MEMORY_RECALL_TYPES` (default: `observation`), `MEMORY_RECALL_BUDGET` (default `mid`), `MEMORY_RECALL_MAX_TOKENS` (default 2048), and the guild scope-tag filter (see Per-Guild Scoping).
7. Format the results as bullet lines.

`agent/core.py` inserts the recalled memory as an ephemeral user-context message through `providers/recalled_context.py`. The framing presents memories as the bot's own knowledge of the user, to be used for personalization, while keeping the injection-safety substance intact: memory content is never instructions, permissions, consent, identity proof, or tool arguments, and the current message wins on conflict. Recalled memory is not added to `ConversationContext` and is not persisted into the SQLite conversation history.

## Current-user tools

`recall_user` is a member-tier LLM tool for retrieving existing memories from the current user's own bank. It cannot accept another user ID and always uses `user:{ctx.user_id}`. It uses the same `MEMORY_RECALL_TYPES` list as automatic responding-turn recall and applies the same guild scope-tag filter (see Per-Guild Scoping), so it cannot pull another guild's memory. It honors `/memory opt-out` through `PreferenceStore`. Results expose only the saved memory text and type; Hindsight document metadata remains internal.

`reflect_user` is a member-tier LLM tool for synthesis over the current user's own bank. Where `recall_user` returns raw facts, `reflect_user` asks Hindsight to reason over `user:{ctx.user_id}` and return a synthesized answer (`MemoryClient.reflect`, `budget="mid"`). It is the per-user counterpart to `reflect_community`. It always uses the current user's bank, can't reason over another user's memory, applies the same guild scope-tag filter as `recall_user`, and honors `/memory opt-out` through `PreferenceStore`. Because reflect runs a slower, costlier reasoning loop than recall, the tool description steers the model to use it only for synthesis questions, not fact lookups. The synthesized answer comes back as untrusted context, just like `reflect_community`.

`remember_user_memory` is the only model-initiated user-memory write path (auto-retain, below, is the background path). It stores new memory and doesn't search or retrieve existing memory. The model can call it proactively when the current user shares durable first-party facts about themselves, whether or not they explicitly ask the bot to remember. It should not store passing chatter, jokes, one-off requests, resolved troubleshooting, or facts about other people. The tool:

- checks `/memory opt-out` before any Hindsight write;
- anchors the memory to the triggering Discord message persisted in SQLite;
- retains only the current user's own source messages plus assistant replies attributed to their preceding user turn;
- excludes other participants and replies to them structurally, including assistant messages with no preceding user in the source window;
- writes Hindsight document metadata containing a source handle, including the conversation id, anchor SQLite message id, Discord message id, channel id/name, and source timestamp;
- uses stable document ids with `update_mode="replace"` so repeated writes for the same source/context replace instead of accumulating duplicate documents;
- caps writes per model turn with `MEMORY_MAX_WRITES_PER_TURN` (default `3`) to bound proactive-write volume.

The tool returns only whether the write succeeded. Its stable document ID and source metadata are internal and are not exposed through the model-facing memory tools.

## Automatic retention (auto-retain)

This is gated on `MEMORY_AUTO_RETAIN_ENABLED` (default off). A background sweeper (`discord_adapter/lifecycle.py:auto_retain_sweeper`, started in `on_ready`) flushes idle conversations into each memory-enabled participant's own bank (every user by default unless they opt out), which lets Hindsight's server-side extraction and consolidation build `observation`-layer knowledge from normal conversations. The persisted SQLite transcript is the buffer, so there is no in-memory turn buffer to lose.

Each sweep (`memory/auto_retain.py:AutoRetainFlusher`, with the SQL in `storage/auto_retain.py:AutoRetainStore`) proceeds as follows:

1. Find `(conversation, user)` pairs where the conversation has been idle for `MEMORY_AUTO_RETAIN_IDLE_MINUTES` and the user has transcript rows past the stored watermark. Only real Discord messages count: ids must be numeric snowflakes (`NOT GLOB '*[^0-9]*'`), which structurally excludes synthetic and non-Discord rows from user memory. Any user with a pending `privacy_deletion_requests` row is excluded from selection outright, and the flush re-checks that inside the per-user mutation boundary, so an in-flight deletion can never race a retain.
2. Conversations first seen already idle past `MEMORY_AUTO_RETAIN_BACKFILL_HORIZON_HOURS` are watermarked without retaining, so enabling the feature never bulk-ingests history.
3. `/memory opt-out` is honored at flush time. The watermark still advances, so re-enabling memory never ingests content from the opted-out window.
4. The slice contains only the subject user's messages plus the bot replies attributed to them. Walking the full interleaved transcript, an assistant row is kept only when the most recent preceding user turn belongs to the subject. Other participants' messages, and the bot replies answering them, are excluded structurally, so multi-user conversations never leak one user's content into another's bank secondhand. Slices below `MEMORY_AUTO_RETAIN_MIN_USER_CHARS` of user-authored text are skipped, with the watermark still advancing.
5. The slice is retained synchronously (`retain_async=False`) before its watermark advances, with a deterministic document id (`auto-retain:{user_id}:{conversation_id}:{start_message_id}`, with `:pN` suffixes for parts above `MEMORY_AUTO_RETAIN_MAX_CONTENT_CHARS`), `update_mode="replace"`, the last user message's source timestamp, and provenance tags (`source:auto_retain`, `scope:user`, and `guild:{guild_id}` when the conversation has a guild; personal `userchat:` roots instead add `scope:global`; see Per-Guild Scoping above). Keying by the fixed slice start keeps a partial multi-part retry idempotent even if new messages extend the slice before the retry. The per-call retain context stays light: it identifies the participants and scopes extraction to the subject user. What actually gets kept is steered at the **bank** level by `USER_CONFIG["retain_mission"]` plus disposition (see Banks), which is the lever that actually applies; the per-call `context` is not the only steering surface.

   **Capture-vs-noise trade.** Steering through `retain_mission`, `retain_extraction_mode=concise`, and raised skepticism trades some capture for much less junk: transient events, assistant-side facts, roleplay, and triple-stored noise. If real durable facts start going missing, soften `USER_CONFIG["retain_mission"]` or lower the skepticism.
6. On success the watermark (`auto_retain_watermarks` table) advances to the conversation's max message id. On failure it stays put, and the next sweep retries from the same slice start; existing parts are replaced under the same document IDs even if newer messages have extended the retry.

A conversation that wakes up and goes idle again produces a second document over the new, disjoint id range. The memory-forget path (`/privacy`) fast-forwards all of the user's watermarks so pre-forget history is never re-ingested into a recreated bank.

## User controls

The `/memory` slash command group exposes:

- `/memory status`: show whether memory is enabled for the current user.
- `/memory opt-out`: disable future automatic recall, `recall_user`, `reflect_user`, and `remember_user_memory` for the current user.
- `/memory opt-in`: re-enable future memory after opting out.

Self-service memory deletion lives on `/privacy` (the **Delete memory** button, or **Delete my data** to also purge transcripts and workspace files); see [privacy.md](privacy.md). Both run `memory/privacy.py:forget_user_memory` for the memory portion.

Opting out doesn't by itself delete existing Hindsight documents or memory units; `forget_user_memory` is the self-service destructive cleanup path. A shared per-user async mutation guard serializes preference changes, bank setup, user-bank retain calls, watermark commits, and deletion. An already-running retain therefore finishes before the bank is deleted, and a later automatic or explicit write observes memory disabled and can't recreate the bank. If Hindsight is unavailable, forgetting still disables future memory in SQLite. It reports that there is no backend to delete only when no durable bank-state flag exists; otherwise `/privacy` stays pending for a later retry, so an unconfigured outage can't be mistaken for confirmed absence.

## Community memory

Community memory is manual and staff-led in this version. Staff write public community knowledge through the `teach` tool; normal conversations are not automatically extracted into the community bank. Each guild has its own community bank (`community_bank_id(ctx.guild_id)`, see Banks above), so teaching and recall are scoped to the server they happen in, and the resolved bank is ensured lazily via `ensure_community_bank(...)` before each read or write.

- `teach` is staff-only and stores public community facts in the guild's community bank with `scope:public`, `topic:{topic}`, `source:taught`, `taught_by:{staff_id}`, and `confidence:high` tags (`recall_community` surfaces the last of these as its `confidence` field).
- `recall_community` searches the guild's community bank with `tags=["scope:public"]`.
- `reflect_community` asks Hindsight to synthesize over the guild's community bank with the same public-scope tag filter.
- Off-guild invocations (no `guild_id`) return a "community knowledge is only available inside a server" result without touching any bank.

There is no community auto-learning. Community memory changes only through the staff-led `teach` path described above.

## SQLite tables

Memory-related SQLite state lives in the main bot database:

- `user_preferences`: `user_id`, `memory_enabled`, `privacy_consent`(+`_at`), `persona_prompt`(+`_updated_at`), `created_at`, `updated_at`. Missing rows default to memory enabled.
- `conversations`: rooted logical conversation rows keyed by the Discord message that started the root. `eval_cursor` is unused by the current memory runtime.
- `message_contexts`: maps real Discord message ids to rooted conversations so replies can resume the right transcript after restarts.
- `conversation_activated_tools`: per-root searchable-tool activation rows that keep `browse_tools` loads available across turns in that root.
- `messages`: the canonical persisted Discord transcript rows for each root, including `discord_message_id` and `source_created_at` for source-backed memory lookup.
- `auto_retain_watermarks`: per-(conversation, user) high-water mark of transcript rows already handled by auto-retain. Advancing without retaining is how opt-out, trivial-content, backfill, and memory-forget slices are permanently skipped.
- `user_memory_bank_states`: per-user conservative knowledge that the remote Hindsight bank may exist. Writes set the flag before contacting Hindsight; `forget_user_memory` clears it only after confirmed bank deletion.

Hindsight memory units and source documents live in Hindsight, not SQLite.

## Configuration

The relevant environment keys are:

- `HINDSIGHT_URL`, `HINDSIGHT_API_KEY`
- `MEMORY_RECALL_TYPES`, `MEMORY_RECALL_BUDGET`, `MEMORY_RECALL_MAX_TOKENS`
- `MEMORY_MAX_WRITES_PER_TURN`
- `MEMORY_AUTO_RETAIN_ENABLED`, `MEMORY_AUTO_RETAIN_IDLE_MINUTES`, `MEMORY_AUTO_RETAIN_SWEEP_INTERVAL_SECONDS`, `MEMORY_AUTO_RETAIN_MIN_USER_CHARS`, `MEMORY_AUTO_RETAIN_MAX_CONTENT_CHARS`, `MEMORY_AUTO_RETAIN_BACKFILL_HORIZON_HOURS`, `MEMORY_AUTO_RETAIN_MAX_FLUSHES_PER_SWEEP`
- `DATABASE_PATH`

Set `HINDSIGHT_URL` to the reachable Hindsight service; there is no built-in endpoint default. For the generic self-hosted Docker deployment, see [`bot/deploy/hindsight/`](../bot/deploy/hindsight/README.md). The service's own extraction, consolidation, and reflection models are configured separately from the bot.

`MEMORY_RECALL_TYPES` is comma-separated and defaults to `observation`, Hindsight's consolidated, deduplicated layer. Adding the raw `world`/`experience` layers re-injects every fact two to four times (each consolidated observation plus its raw source units, sometimes re-translated), which floods the responding-turn prompt, so add them back only when you actually want the raw episodic detail. `MEMORY_RECALL_BUDGET` (default `mid`) and `MEMORY_RECALL_MAX_TOKENS` (default 2048) bound how much recalled context is injected; the defaults mirror `memory/recall.py:DEFAULT_USER_RECALL_*` and flow through `TurnPreparationConfig`.

## Failure behavior

These are the startup log lines you will see, and what each one means:

| What you see | What it means |
|---|---|
| `No Hindsight URL configured - running without memory` | `HINDSIGHT_URL` is empty. No client is constructed and no memory tool registers. This is a supported configuration, not an error. |
| `Hindsight memory unavailable at <url> - running without memory tools` | The URL is set, but `ensure_global_banks` could not create or confirm the shared `bot-skills` bank. Readiness stays false, and registered memory tools are removed. |
| `Hindsight memory connected at <url>` | Ready. Community and user memory tools are registered. |

A per-guild community bank that cannot be created fails more quietly: `ensure_community_bank` returns `None` and the calling tool reports that community memory is unavailable, so one guild can be without community memory while the rest of the deployment has it.

Beyond startup, the failure modes are:

- Automatic recall degrades to no recalled context if the preference lookup or the Hindsight recall fails.
- Explicit memory tools return safe JSON errors or disabled-memory responses instead of raising into Discord.
- `MemoryClient.recall` logs backend failures and returns no memories; retain, bank creation, and bank configuration log failures and return `False`.
- Privacy deletion uses strict whole-bank deletion. An unconfirmed backend failure raises internally so the durable deletion request remains pending for retry; an already-absent bank is treated as a successful idempotent retry.
- Auto-retain failures are isolated per slice. A failed retain leaves the watermark untouched (the next sweep retries the identical slice as a replace), one slice's exception never aborts the sweep, and an exception in the sweep itself is logged without killing the sweeper loop.
- `/privacy` requires a confirmation-button click before deleting the user's whole bank. The bot does not expose document-level memory administration.
