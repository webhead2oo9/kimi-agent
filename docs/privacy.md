# Privacy

This is the technical account of what the bot stores, where that data can
leave to, how long it is kept, who can see it, and which controls users have.
The plain-language version written for community members lives in
[privacy-policy.md](privacy-policy.md); when behavior changes, keep the two in
sync.

Two facts frame everything below:

- **The bot ignores DMs entirely.** `KimiApplication.on_message`
  (`app/runtime.py`) rejects `discord.DMChannel` before any reaction, transcript
  write, consent prompt, or provider call. The only code that runs earlier is
  the image-fingerprint offer, and it drops anything without a guild too. DM
  content is never read, stored, or logged.
- **Conversation handling is invocation-gated.** A turn starts only on an
  @mention, a reply with the reply-ping toggle on, a "hey <bot_name>" text
  invocation, or a message inside a bot-created thread. Ordinary channel chatter
  is not persisted as conversation history. There are three live, non-transcript
  exceptions, each documented below: the context tools, opt-in known-bad image
  fingerprint enforcement, and the optional staff moderation event feed.

## What the bot stores, and where

### SQLite (`data/bot.db`)

This is the primary store: async SQLite in WAL mode, with the schema owned by
`storage/db.py`. These are the tables that hold user-attributable data:

| Table | User data held |
|---|---|
| `messages` | The per-root transcript: `user_id`, `user_name`, message text, source timestamps, and up to `MAX_PERSISTED_CONVERSATION_IMAGES` (10) recent images per conversation stored inline as base64 (`storage/conversations.py`). One row per real Discord message the bot saw on an invoked turn. When the chat model cannot see images, the row also stores a machine-written description of that message's images, including OCR. That description is kept for the full retention window and outlives the image bytes, which are evicted once the conversation passes the image cap. |
| `conversations` | Root metadata: `guild_id`, `channel_id`, `thread_id`, `channel_name`, root message id, explicit user owner where applicable, timestamps. |
| `message_contexts` | Maps Discord message ids → conversation roots so reply routing survives a reboot. |
| `conversation_activated_tools`, `thread_conversations` | Per-root tool activation and thread-handoff enrollment, including the initiating user id used to authorize close/pause/resume. |
| `image_distillations` | Conversation-scoped, model-produced descriptions of transcript images, including OCR, approximate normalized bounding boxes, and uncertainty. No additional image bytes. Rows cascade with deleted conversations and are invalidated when one participant's messages are scrubbed from a surviving shared conversation. This table is only a cache; the durable copy is the description stored on the `messages` row it describes, which a per-user scrub removes along with that user's rows. |
| `video_sessions`, `video_interactions` | Actor/root/guild-scoped specialist metadata: source kind, safe display filename/relative locator and byte size (or canonical YouTube URL/video id), model, opaque local and Gemini Interaction ids, counts, and timestamps. No uploaded bytes, Discord CDN URLs, questions, or answers. |
| `video_provider_files` | Client-chosen Gemini File resource name, actor/root/guild scope, MIME, byte size, session association, and timestamps. No File capability URI or bytes. |
| `video_interaction_deletions`, `video_provider_file_deletions` | Content-free retry rows for provider-side Interaction/File deletion: opaque provider id, user/session grouping, timestamps, attempt count, and bounded last error. No source content, question, or answer. |
| `user_preferences` | `user_id`, `memory_enabled`, `privacy_consent`(+`_at`), `persona_prompt`(+`_updated_at`). Settings, not message content. |
| `usage_ledger` | Cost/usage metadata, one row per completed LLM call, grouped into logical turns by `turn_id`: `user_id`, `user_name`, `channel_id`, `guild_id`, serving model/role, token counts, estimated cost. **No message content.** |
| `paid_usage_ledger` | Cost metadata, one row per non-LLM tool backend that actually charged: user/channel/guild attribution, tool and provider names, dollars, turn id, and timestamp. **No query, result, or message content.** |
| `usage_markers` | Short-lived rate-limit metadata for bounded tools: user/channel/guild attribution, surface, operation, unit count, and timestamp. Code-exec markers contain no source code, arguments, output, or cost. |
| `blocked_users` | Moderation blocks: `user_id`, `blocked_by`, `reason`, timestamps. |
| `auto_retain_watermarks` | Per-(conversation, user) high-water marks for memory auto-retain. |
| `privacy_deletion_requests` | Durable authorization for a confirmed `/privacy` deletion: user id, coalesced scope, generation, unique completion token, memory-backend requirement, and timestamps. The row contains no message content. |
| `user_memory_bank_states` | A conservative per-user flag recording that a remote Hindsight bank may exist. It holds only the Discord user id, the flag, and an update timestamp. |
| `coding_tasks`, `coding_task_events`, `coding_command_jobs` | Durable background objectives, acceptance criteria, selected conversation context and starting-file metadata, plan/checkpoint, steering, bounded command output, status, and Discord delivery ids. Rows are scoped to the requesting user and their workspace and leave with the rooted conversation. |

**Encryption at rest (optional).** When `DATABASE_ENCRYPTION_KEY` is set, this
database, including the WAL sidecar, is encrypted on disk with SQLCipher
(AES-256), so a stolen `data/bot.db` or backup is unreadable without the key.
The key lives in the environment or an untracked dotenv file. Encryption is off
by default (plaintext), and it covers the SQLite store only: the workspace files
described next are not encrypted at the application layer and rely on host disk
encryption. See [database.md](database.md).

### Workspace files (`workspaces/<user_id>__<guild_id>/`)

Each (user, guild) pair gets its own sandboxed directory for the workspace tools
(`workspace/manager.py`). Because a user's files are scoped to the community
they were created in, they never bleed across guilds. Storage is bounded by a
**7-day inactivity TTL** (`workspace_file_ttl`) and a size quota
(`workspace_max_size_mb`, 150 MB) that applies **per (user, guild)**; the
background workspace sweeper (`discord_adapter/lifecycle.py`) enforces both.
None of this depends on the memory or consent settings.

The manager also writes per-conversation delivery artifacts under
`workspaces/generated/<context_key>/<job_id>/`. Each job directory carries an
`.owner-user-id` marker so the files stay user-attributable, which is what lets
them be swept on the same lifecycle and removed by full data deletion (below).

### Attachment temp store (`data/attachments/`)

Image attachments are written here transiently during a turn so the vision path
can read them, then deleted in a `finally` block at the end of every turn
(`agent/turn.py:cleanup_prepared_moderation_artifacts`). This is **not** a
persistent store. A turn interrupted before cleanup can leave a straggler file
behind, so a bounded orphan sweeper removes expired stages on startup and at the
configured interval. Persistent image storage is the inline-base64 cap in the
`messages` transcript, not this directory.

The optional known-bad image enforcement path separately reads supported image
attachments (plus, if budget remains, the first Discord-proxied embed image)
into bounded process memory for local pHash comparison. It runs only in
explicitly configured guild channels, and there it covers messages that did not
invoke Kimi. The bytes and the locally computed hash are discarded after the
comparison; they never enter SQLite, workspace storage, transcripts, Hindsight,
diagnostics, or FingerprintHub.

When an ordinary member's image matches, the bot deletes the message, attempts
the configured timeout, and opens a retained moderation case holding the
message/user ids and fingerprint metadata. A staff match opens a review case but
is not punished, and bot messages are skipped. This community-moderation gate
runs before conversational consent, so declining AI-provider consent stops chat
processing but does not exempt messages in an enrolled channel from local
fingerprint enforcement. The scan itself sends no user data to any third party.

### Browser profiles (`data/browser_profiles/`)

When the optional BetterWright browser is enabled, each Discord user gets one
profile directory, named from a truncated SHA-256 digest of their Discord id
rather than the id itself. It can hold cookies, site storage, cache, browsing
history, and screenshots produced while completing that user's requested web
tasks. Profiles are never shared between users. Browser page content reaches
SQLite only if it becomes part of the normal model reply and transcript, and
copied screenshots follow the generated-workspace lifecycle.

Inactive profiles are deleted after `BROWSER_PROFILE_TTL_SECONDS` (seven days
by default). A full `/privacy` deletion closes the user's active browser worker
and deletes the profile immediately. The browser credential vault, credential
capture, downloads, and live-view server are all switched off. See [Persistent
browser](browser.md).

### Gemini video interactions (optional)

When `VIDEO_UNDERSTANDING_ENABLED` and `GEMINI_API_KEY` register the searchable
`video` tool, `start` sends either a public YouTube URL or streamed bytes from an
exact current-message Discord attachment/safe workspace video plus the user's
question to Google's paid Gemini API. Uploads use Files API (500 MiB and one-hour
hard ceilings); Google documents File retention up to 48 hours. Interactions use
`store=true` and follow-ups use `previous_interaction_id`. The normal Kimi chat
provider sees only the untrusted specialist result.

Local sessions contain safe identifiers/scope metadata only and expire after at
most 24 hours idle. Expiry, transcript retention, and full `/privacy` remove
local access immediately and queue every known Gemini Interaction and uploaded
File for deletion. Outboxes retry hourly. If provider access is unavailable
during `/privacy`, local deletion completes, the user barrier is released, and
provider deletion stays independently queued. Google documents paid-tier
Interaction retention up to 55 days and may separately retain limited
safety/security records; local expiry is not a claim every provider copy has
already disappeared. See [Video understanding](video-understanding.md).

### Long-term memory: Hindsight (optional)

Long-term memory is backed by an operator-provided self-hosted
[Hindsight](https://github.com/vectorize-io/hindsight) service and wired only
when `HINDSIGHT_URL` is set. Per-user banks (`user:{discord_id}`) are written in
two ways: explicit `remember_user_memory` calls, and the background auto-retain
sweeper (`memory/auto_retain.py`, gated on `MEMORY_AUTO_RETAIN_ENABLED`, off by
default), which flushes idle conversations into each **memory-enabled**
participant's own bank behind watermarks and structurally excludes other
participants' messages. The per-user preference defaults to enabled;
`/memory opt-out` disables it and `/memory opt-in` re-enables it.

Per-guild community banks are separate shared stores, and only a STAFF `teach`
tool call writes them. The staff-facing **Teach Kimi** context menu can quote a
member's public message to the chat provider, which may then retain derived
knowledge in that community bank or a shared skill. Successful writes are also
summarized in the configured Discord learn-log channel. A member's `/privacy`
request does not delete community banks, shared skills, or those Discord log
messages. See [memory.md](memory.md) and [learning.md](learning.md).

### Diagnostic logs (optional, off by default)

- **Tool-event log** (`observability/events.py`, gated on
  `TOOL_EVENT_LOG_ENABLED`, default off): a JSONL diagnostic stream. Every mode
  records attribution and operational metadata such as user/channel ids, tool
  and model names, timing, success, usage, compaction counts, and moderation
  scores. `TOOL_EVENT_LOG_CONTENT_MODE=metadata` is the default and omits tool
  arguments/results plus request/response text. `redacted` and `full` also log
  clamped tool arguments/results and a clamped request/response snapshot, and
  that snapshot can include the system prompt, stored history, transient channel
  context, the current message, and the reply. `redacted` masks known configured
  secrets and sensitive-key fields; `full` is verbatim within the field cap.
  Diagnostics are never on the response path.

## Retention and deletion

Raw conversation transcripts are kept on a **rolling window**
(`transcript_retention_days`, default **30**), enforced by the transcript
retention sweeper (`discord_adapter/lifecycle.py:transcript_retention_sweeper`,
started in `on_ready` and run hourly by
`transcript_retention_sweep_interval_seconds`):

- **Conversation transcripts**: a whole conversation is purged once its last
  activity is older than the window. Deleting the conversation removes
  `messages` (inline transcript images and their generated descriptions
  included) plus every row that references it: `message_contexts`,
  `conversation_activated_tools`, `thread_conversations`,
  `image_distillations`, and `auto_retain_watermarks`. Because the sweep keys on
  `conversations.last_active_at`, a still-active thread is never pruned
  mid-conversation. Setting `transcript_retention_days = 0` disables the sweep
  and keeps transcripts forever.

A few stores are deliberately not on this clock:

- **Usage/cost ledgers** (`usage_ledger`, `paid_usage_ledger`) are retained for
  cost accounting and the `/usage` command (any member sees their own windows;
  other users and server totals are staff-only). They hold per-call cost
  metadata only, never message content.
- **Bounded-tool markers** (`usage_markers`) are exempt from `/privacy`
  deletion, because a capacity limit anyone can reset by deleting their data is
  not a limit. A row records only that a bounded tool was used, with no code,
  arguments, results, or message content, and is pruned once it passes eight
  days old.
- **Discord learning logs** are staff event cards posted as ordinary messages in
  configured Discord channels. Their lifecycle belongs to server staff and
  Discord, not to the local transcript sweep or `/privacy`.
- **Diagnostic logs** (the tool-event log) form an append-only JSONL file at
  `TOOL_EVENT_LOG_PATH` (default `logs/events.jsonl`) that rotates at 50 MB and
  keeps only the current file and one `.1` predecessor, so older events age out
  on their own; the transcript sweep's clock does not apply.

Everything else keeps its own lifecycle, by design:

| Data | Lifecycle |
|---|---|
| Workspace files | 7-day inactivity TTL + size quota (above). |
| Browser profile | 7-day inactivity TTL + per-profile quota; full `/privacy` deletion removes it immediately. |
| Video specialist sessions | 24-hour maximum idle lifetime; local access is removed on expiry/root deletion and all known Gemini Interaction/File ids are queued for provider deletion. Google Files API uploads are independently retained for up to 48 hours unless deleted sooner. |
| Attachment temp files | Deleted at the end of each turn. |
| Coding task state and command output | Retained with the local operational database until full `/privacy` deletion; terminal delivery state supports restart retries. |
| Long-term memory (Hindsight) | Per-user default on when the backend is configured; `/memory opt-out` stops future user-memory use and writes. Retained until the user deletes it via `/privacy` (**Delete memory** / **Delete my data**). Not part of the 30-day transcript sweep, since memory is the long-term store. |
| Community memory and private shared skills | Staff-managed shared knowledge retained until staff delete it. Not part of a member's `/privacy` deletion. |
| Personal skills (`data/personal_skills/<user_id>/`) | User-authored instruction documents; retained until the user removes them. |
| Preferences (consent, memory on/off, persona) | Retained as settings. `persona_prompt` is cleared by the memory-forget path. |
| Moderation blocks | Retained until unblocked. |
| Discord moderation/learning log messages | Retained under the server's Discord-channel lifecycle; not deleted by `/privacy`. |

### Deletion controls

- **`/privacy`** (`commands/privacy_cmd.py`) shows the plain-language TL;DR
  (mirroring `privacy-policy.md`) with two on-demand deletion buttons, both of
  which ask for confirmation before they act. That confirmation is committed to
  `privacy_deletion_requests` before Discord is acknowledged and before any
  destructive step, which is what makes the workflow survive a crash. Confirmed
  workflows are drained during graceful shutdown; after a hard restart, startup
  loads every remaining request, marks all affected users pending, and replays
  each through the same barrier and deletion dependencies before exposing
  normal turn context. A failed attempt leaves the row in place and pauses new
  messages and automatic memory retention for that user only, so unaffected
  users continue normally. The affected user can retry `/privacy`, or the
  request is retried at the next restart. The row is removed only after every
  step succeeds. Repeated requests coalesce to the widest scope, and a unique
  request token prevents an older worker from completing a newer authorization:
  - **Delete my data** first takes an exclusive per-user deletion lease. It
    waits for already-started turns to finish, cancels foreground responses and
    coding tasks (including managed sandbox teardown), prevents later ones from
    starting, and also drains the normal root lock for every conversation the
    user owns, spoke in, or initiated a managed thread within. That root drain
    covers another participant's already-running model turn, Discord delivery,
    and assistant-transcript persistence, so a shared-root reply derived from
    the deleted rows cannot land after deletion. It then purges the SQLite
    transcript, the user's per-guild workspace files, their persistent browser
    profile, and every video-specialist session they initiated, then deletes
    long-term memory without waiting for automatic expiry. Transcripts go through
    `ConversationStore.delete_user_data`, which deletes whole conversations the
    user rooted (tracked explicitly at root creation, including timeout
    transcripts), scrubs the user's own message rows from other shared
    conversations, and clears their managed-thread initiator marker while
    leaving other participants intact. A surviving thread whose initiator
    marker was cleared then requires STAFF or Discord Manage Threads for
    lifecycle changes. Workspace files go through
    `WorkspaceManager.delete_owner_dirs`, which removes every per-(user, guild)
    directory for that user (`<user_id>__*`) plus every generated job directory
    whose `.owner-user-id` marker names them. Memory goes through the same
    `forget_user_memory` path described below. A transcript deletion error
    aborts before later stores are touched; workspace, browser-profile or memory failures leave the durable request pending for retry.
    A provider-side video deletion failure does not: local video metadata is
    already gone, its content-free deletion outbox remains durable, the result
    reports pending provider cleanup, and the user barrier is released.
    Already-absent
    Hindsight banks count as success, so replay is safe if a crash happened
    after remote deletion. Personal skills have their own lifecycle and are not
    part of this action; users remove them individually with `my_skill_delete`.
    For video sessions the bot requests deletion of every known stored Gemini
    Interaction and Files API upload and retries failures; it still cannot guarantee removal from
    provider safety logs, backups, or legally required records. The action also
    cannot delete Discord messages, other provider-side copies or logs, backups,
    the tool-event log, community memory, shared skills, usage ledgers/markers,
    moderation cases or Discord staff-log messages, blocks, the retained consent
    choice, or the non-content bank-state marker.
  - **Delete memory** runs `forget_user_memory` only; the transcript is left to
    the retention sweep.
- **`forget_user_memory`** (`memory/privacy.py`) runs under a shared per-user
  mutation guard. It disables future memory, clears the stored persona,
  fast-forwards the user's auto-retain watermarks, and deletes the user's
  Hindsight bank. Every user-bank create/retain first records
  `user_memory_bank_states.may_exist = 1`, and only a confirmed bank delete
  (including an already-absent backend response) clears it. If Hindsight is
  temporarily unconfigured while that flag is set, `/privacy` stays durably
  pending instead of claiming there was no bank. Bank setup, explicit writes,
  automatic retention, and preference toggles all use the same guard, so an
  already-running write either finishes before deletion or observes the
  disabled state afterward. There is no parallel deletion path.
- **`/memory opt-out`** stops future memory recall/writes for the user. It does
  not delete existing data by itself.
- **`block_user`** (member self-block) and staff **`/moderation`** stop the bot
  responding to a user; neither deletes prior data.
- **Declining or ignoring the consent prompt** (if the gate is enabled) means
  the message never reaches the provider or the transcript at all.

## Third-party egress: where user data can leave to

The core chat model receives conversation content; that is the substance of the
consent gate. Optional services receive only the targeted data documented
below, never bulk transcripts or profile data. The workspace URL-fetch tool
contacts the public HTTPS target supplied for that task but sends no stored
transcript or memory.

**Third-party cloud services** (subject to that provider's own data handling):

| Service | What leaves | Gate | Default |
|---|---|---|---|
| Core chat LLM provider | Conversation turns (system prompt, recent history, the user's message, any image input) | always on when running | on |
| OpenAI moderation (`moderation/backends/openai_omni.py`, driven by `moderation/service.py`; `app/moderation.py` is the factory) | The user's message and the bot's drafted response (text/images) for policy scoring | `MODERATION_ENABLED` + key | off |
| Persona compiler | A user's raw persona request + display name when they set a persona | a `persona` role in `config/models.yaml` | off |
| Exa internet search | Search queries and filters; for page reading, the requested public URLs | `EXA_API_KEY` | off |
| Brave internet search | Search queries and filters | `BRAVE_API_KEY` | off |
| Google Gemini video understanding | A public YouTube URL or streamed Discord/workspace video bytes plus the user's questions; Google temporarily stores uploaded Files and the Interaction chain for stateful continuation | `VIDEO_UNDERSTANDING_ENABLED` + `GEMINI_API_KEY` | off |
| Workspace URL fetch | The normal HTTPS request for a user/model-supplied public URL; private, LAN, loopback, and unsafe redirects are blocked | core workspace tool | on |

**Operator-hosted services** (data stays on infrastructure the operator runs):

| Service | What it receives | Gate |
|---|---|---|
| Hindsight (long-term memory) | Conversation slices + metadata for users who have not opted out | `HINDSIGHT_URL` |
Application modules, operator plugins, and script-backed skill tools are
trusted deployment code and may add their own egress. Skill scripts run inside
the Linux Bubblewrap boundary with network denied by default and only the
per-call output workspace writable. A tool whose declaration opts into
`network: true` shares the service host's public, private, and loopback
reachability without a destination allowlist, so each operator-added tool must
document the services and data it uses.

Optional application modules may also add database tables, retention rules,
Discord events, and external services; their package documentation is the
source of truth for those additions. An empty `KIMI_MODULES` loads no module
code, but previously created module tables remain until the operator explicitly
removes them. Disabling a module is not data deletion.

Retrieved and third-party text is always framed to the model as untrusted
context and, by default, is not written into the persisted transcript.

Discord itself is also a data destination, both for normal replies and for the
optional staff audit feeds. The moderation feed can copy edited/deleted message
content, attachment names, member joins/leaves, role updates, and moderation
actions into a configured staff channel. The learning feed posts a bounded
summary of shared knowledge or skill content that staff caused the bot to
store. Those cards are Discord messages, not rows in the local transcript.

### Channel context from other members

Consent gates a user's own directed interactions; it is not a guarantee that no
text any member ever wrote reaches the provider. When a consented user's turn
calls `get_channel_context` (recent channel history) or `discord_text_search`
(operator-configured channels), public-channel messages from other members are
forwarded to the provider as transient, untrusted context. That is intended:
the text is already visible to everyone in the channel, and retrieval alone
does not persist it to the transcript or personal memory. It can show up in a
non-metadata diagnostic event when content logging is enabled, and staff can
separately teach derived content into community memory or a shared skill.
Consent records only exist for users who have interacted with the bot, so
filtering bystander messages by consent would not be meaningful.

## Operator and staff access

The bot operator can access the SQLite database, workspace files, diagnostic
logs, configuration, and Hindsight backend as the infrastructure administrator.

Discord commands expose only their bounded operational views. `/usage` shows a
member their own token and cost windows (viewing another user or the server
totals is staff-only), the staff-only `/moderation` manages blocks and reasons,
and the staff-only `/mod` group reads and appends to a member's moderation case
record (reasons, staff notes, and message links, not transcript content).
`/models` is bot-owner-only. None of these commands expose conversation
transcripts or private memory. Staff with access to the configured moderation
or learning log channels can also read the event cards posted there. The
privilege gate is the trust check at the command boundary, not prompt text.

## Diagnostic logging

The optional tool-event log exists for debugging and operability, not for
profiling users. It is **off by default** (`TOOL_EVENT_LOG_ENABLED`), and when
on it captures the fields allowed by `TOOL_EVENT_LOG_CONTENT_MODE` as described
under "Diagnostic logs" above. It is an append-only file that rotates at 50 MB
and keeps one predecessor, so it is self-bounding, but it is not covered by the
transcript retention sweep or `/privacy`. The usage ledger holds per-call cost
metadata (ids, channel, serving model, tokens, cost, grouped by `turn_id`) with
**no message content**, and backs the `/usage` command (self-view for every
member; other-user and server views staff-only).

Bounded-tool markers hold less again: attribution, a counter unit, and a
timestamp. They are pruned once they pass eight days old.

## Privacy consent gate

The bot runs on a third-party LLM provider that may transmit and log user
prompts server-side. The privacy consent gate gives each user a one-time,
explicit choice before any of their messages reach that provider.

It is **off by default** (`PRIVACY_CONSENT_ENABLED`) and entirely separate from
memory opt-out (`/memory opt-out`, which only governs Hindsight long-term
memory). The distinction is that memory opt-out controls retention, while the
consent gate controls whether the provider call happens at all.

### Behavior

On a user's first interaction after the feature is enabled, the bot posts a
privacy notice as an embed with **Accept** / **Decline** buttons and does
**not** run the model yet:

- **Accept** → consent is recorded in SQLite, the prompt updates to a
  confirmation, and the user's original message is then answered through the
  normal path. The user is never prompted again.
- **Decline** → the prompt updates to a dismissal and the message is dropped.
  Nothing is sent to the provider and nothing is written to the local
  transcript. Decline is **not** a permanent block; the gate reappears on the
  user's next mention.
- **Ignore / timeout** → after `PRIVACY_CONSENT_TIMEOUT` seconds the buttons
  are disabled and the message is dropped; the gate reappears next mention.

Only the user who triggered the prompt can use its buttons; another member's
click gets an ephemeral rejection.

### Where it sits

`KimiApplication.on_message` (`app/runtime.py`; the gate call itself lives in
the extracted `_on_message_for_user`) consults the gate immediately after it
has decided the bot would respond and **before** it acquires the response lock,
persists the triggering message, or calls the provider:

```
image-fingerprint offer (guild channels only; returns immediately for DMs)
eligibility + DM reject
should_respond (mention / "hey <bot_name>" / managed-thread gate)
block check
── if the bot can't post in this channel: return (skip a turn we can't deliver) ──
turn-admission gate (concurrency cap; may post a busy notice)
per-user privacy-barrier activity lease
★ consent gate: if enabled and not consented → post embed+buttons, return
resolve conversation ── if resolved is None: return ──
lock (should_respond re-checked under it) + handle_message → model turn → reply
```

Because the gate runs before `handle_message`, an un-consented or declined
message reaches neither the third-party provider nor the local SQLite
transcript. On accept, the gate re-dispatches the original `discord.Message`
back through `on_message`; consent is recorded by then, so the message passes
the gate and is answered with no special-case code. If the consent check itself
errors, the gate **fails closed** and treats the message as gated rather than
letting it through.

### Components

- `app/consent.py` is the Discord boundary. `PrivacyConsentGate` holds all the
  decision logic (`maybe_prompt`, accept/decline handling, and an in-memory
  pending set that dedupes a burst of mentions into a single prompt). It depends
  only on the `ConsentPreferenceStore` protocol plus a redispatch callback, so
  it is unit-testable without a live connection. `PrivacyConsentView` is the
  thin two-button `discord.ui.View`.
- `storage/preferences.py` provides `has_consented(user_id)` (defaults
  **False**, because consent must be explicit) and
  `set_consent(user_id, granted)`.
- `storage/db.py` owns the `user_preferences` table, which carries
  `privacy_consent`, `privacy_consent_at`, `memory_enabled`, `persona_prompt`,
  and `persona_updated_at`. It is part of the initial schema-v1 baseline; see
  [`database.md`](database.md).

### Configuration

| Setting | Default | Purpose |
|---|---|---|
| `PRIVACY_CONSENT_ENABLED` | `false` | Master switch for the gate. |
| `PRIVACY_CONSENT_TITLE` | "Before we chat: a quick privacy note" | Embed title. |
| `PRIVACY_CONSENT_TEXT` | (see `.env.example`) | Embed body shown to the user. |
| `PRIVACY_CONSENT_TIMEOUT` | `300` | Seconds the buttons stay live. |
| `PRIVACY_POLICY_URL` | "" | When set, the `/privacy` embed links the full policy. |

### Scope and limits

- **Per-user, global, permanent.** Consent is keyed by Discord user ID and
  applies across all channels and servers; once accepted it never re-prompts.
- **All tiers**, staff included.
- **Restart caveat.** The `View` is non-persistent and the pending set is
  in-memory, so if the bot restarts while a prompt is open, the buttons go dead
  and the user gets a fresh prompt on their next mention (consent was never
  written).
- **Not provided:** user-facing consent revocation, or a staff reset of a
  user's consent. `/privacy` deletes the retained stores listed above but does
  not revoke the recorded consent preference.

## User persona overrides

Regulars and staff can ask the bot to set, show, or clear a per-user
character/persona override. When a user asks to set a persona, the raw persona
request is sent to the model assigned to the `persona` role so it can produce a
13+-appropriate compiled persona. That compiler request includes the user's
display name, the maximum compiled-persona length, and the raw persona request.

The compiled persona is stored locally in SQLite in the user's
`user_preferences` row (`persona_prompt`, with `persona_updated_at`) and is
scoped to that Discord user only. That user's future normal turns include the
stored persona in the system prompt sent to the chat provider until they clear
or reset it. The persona tools do not store executable code or personal skill
files.

Clearing the persona removes it from SQLite. The memory-forget path
(`/privacy`) also clears the stored persona as part of deleting user-scoped
retained data.
