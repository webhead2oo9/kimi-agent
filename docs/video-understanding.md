# Video understanding

The optional searchable `video` tool lets Kimi ask a stateful Gemini specialist
questions about one video. A session may start from:

- an exact public YouTube URL;
- a supported video attached to the current Discord message, selected by exact
  filename; or
- a safe workspace-relative video path.

The chat model loads the MEMBER tool through `browse_tools`. Once loaded it
stays available for the rest of that conversation like any searchable tool,
while each video session remains tied to the user and guild that started it.

YouTube videos are passed to Google by URL. Uploaded files go through Google's
resumable Files API in bounded chunks; the bot never holds a whole 500 MiB
attachment in memory.

## Availability

Registration is fail-closed and requires environment configuration and a configured `models.yaml` role:

1. Environment settings:

```dotenv
VIDEO_UNDERSTANDING_ENABLED=true
GEMINI_API_KEY=...
```

2. Role assignment in `config/models.yaml`:

```yaml
providers:
  gemini-video:
    type: gemini_interactions
    api_key_env: GEMINI_API_KEY

models:
  gemini-video-flash:
    provider: gemini-video
    model: gemini-3.7-flash
    context_window: 1048576
    capabilities: [video_input]
    # Example only; verify current rates with Google before deployment.
    pricing:
      input: 0.75
      output: 3.75
      cached_read: 0.075

roles:
  video: gemini-video-flash
```

The `VIDEO_UNDERSTANDING_ENABLED` flag enables all three source types. If it is
false, the tool is absent no matter what. If it is true but `roles.video` is
unassigned or the key is blank, the bot still starts, logs a clear warning, and
leaves the tool absent. The rates above are a configuration example, not a live
price feed; verify model availability and pricing against Google's current
documentation.

`GEMINI_API_KEY` is an environment-only `SecretStr`. It is never accepted from a
tool call, tool fragment, model catalog, prompt, or Discord command. The client
connects only to fixed Google Gemini hosts. With the key still configured,
cleanup remains available while video analysis is disabled, so registered
provider resources remain deletable. Without the key, deletion records stay
queued until credentials are restored.

The video specialist model and its token rate card are configured authoritatively in
`config/models.yaml` under `roles.video`. Fallbacks are unsupported because interaction
chains are stateful. The video specialist never substitutes for the configured chat model.

## Model-facing operations

One tool owns two actions.

### `start`

`start` requires a specific question and exactly one source:

```json
{"action":"start","url":"https://youtu.be/...","question":"What happens?"}
```

```json
{"action":"start","attachment":"clip.mp4","question":"What happens?"}
```

```json
{"action":"start","path":"imports/clip.webm","question":"What happens?"}
```

For YouTube, the handler accepts only exact HTTPS YouTube hosts and one valid
video id, canonicalizes watch/short/live/embed/`youtu.be` forms, and sends the
video before the question. Playlist-only, credentialed, fragmented,
explicit-port, non-YouTube, and invalid-id URLs fail locally. Privacy and
availability cannot be determined from URL structure: Google accepts only
public YouTube videos and rejects private, unlisted, or unavailable videos at
the provider call. Arbitrary MP4/web URLs are not a source; import them through
the existing guarded workspace path first.

For a Discord attachment, the exact filename must identify one current-message
attachment. The captured source must be an HTTPS Discord CDN attachment URL on
the fixed CDN host allowlist. Redirects, credentials, fragments, explicit ports,
size changes, truncation, and over-limit streams fail closed. The normal
`import_attachment` path remains separately bounded and may still withhold an
unmoderatable binary; the enabled video tool gets only this narrow video stream,
never a generic readable binary handle.

For a workspace path, `WorkspaceManager` rejects absolute paths, traversal,
symlinks, and reserved environment trees. The final file is opened no-follow
under the per-workspace activity lock and must be regular; the bound fd then
streams after releasing that lock, so network backpressure does not block global
workspace maintenance. General workspace quotas are unchanged;
a workspace cannot acquire a 500 MiB file merely because video analysis accepts
one from Discord.

Uploaded files must be 1–524,288,000 bytes and use a Gemini-supported format:
MP4, MPEG/MPG, MOV/QuickTime, AVI, FLV, WebM, WMV, or 3GPP. Common MIME aliases
are normalized, and an incompatible declared MIME/extension pair is rejected.
Gemini remains the authoritative container decoder.

The client reserves a provider File name locally before upload, performs the
resumable upload in 8 MiB chunks, polls until **ACTIVE**, and requires valid video
duration metadata. Videos over one hour, failed processing, malformed/missing
duration, or processing beyond 15 minutes are rejected and queued for deletion.
One upload runs at a time; ordinary analysis/follow-up concurrency remains
separately configurable.

A successful start stores an opaque local session handle plus every provider
resource id. The result contains an answer, limitations, and timestamped
evidence as untrusted tool context.

### `ask`

`ask` continues the Gemini chain through `previous_interaction_id`; the bot does
not reconstruct or resend the video or the chain's turn content:

```json
{"action":"ask","session":"video_...","question":"What happens next?"}
```

The handle may be omitted only when exactly one unexpired session belongs to
the current user in the current rooted conversation. More than one match fails
closed. The same system instruction, response schema, thinking level, and output
cap are repeated because continuation preserves conversation history, not those
interaction-scoped controls. A session pins both the catalog model name used for
pricing and the resolved upstream model ID used for every continuation. Changing
`roles.video` affects only new sessions. Keep an old catalog entry and its rate
card unchanged until its sessions have expired; removing it leaves those calls
unpriced, while reusing its name for different rates would misattribute them.

## Scope and crash consistency

A local handle is not authority by itself. Every lookup rechecks rooted
conversation id, initiating user id, guild id, expiry, and the expected latest
Interaction id during atomic advancement. Concurrent advances use compare-and-
swap; the loser deletes its newly created orphan rather than forking the chain.

For an upload, SQLite records a client-chosen `files/<id>` reservation before
any bytes reach Google. Provider network work never holds a database write
transaction. After Interaction creation, one transaction creates the session,
records its first Interaction, and claims the reservation. Normal failures
release the reservation into the durable deletion outbox; a crash leaves an
unattached reservation that startup and periodic cleanup expire after a
conservative grace period. The periodic interval is
`TRANSCRIPT_RETENTION_SWEEP_INTERVAL_SECONDS` (one hour by default).

Once Google has returned a billable result, its local session write finishes
before caller cancellation propagates. A committed write keeps its Interaction
and any claimed File; a failed write or lost compare-and-swap queues only the
unreferenced provider state for deletion. Usage attribution is preserved in
either case. Waiting for a concurrency slot and sleeping before a retry remain
immediately cancellable. If cancellation arrives after an Interaction-create
POST starts, the client finishes only that in-flight attempt so it can retain a
returned resource ID or usage record; it never dispatches a later retry for the
cancelled call.

Sessions survive turns and restarts in SQLite. Source rows retain only safe
metadata: source kind, display filename/relative locator, byte size, catalog and
upstream model identifiers, scope, timestamps, and opaque provider ids. They
never store video bytes,
Discord CDN URLs, Gemini File capability URIs, questions, or answers.

## Output and trust

Gemini must return `answer`, `limitations`, and evidence ranges containing
start/end seconds, `basis`, and `claim`. `basis` is one of `audio`, `visual`,
`audio_and_visual`, or `inference`, preserving whether a claim was heard, seen,
corroborated, or inferred.

YouTube evidence adds clickable `&t=<seconds>s` links. Uploaded-file evidence
has readable timestamps but no external link or local absolute path. Its source
metadata is limited to a safe display filename and `attachment`/`workspace`
origin.

The complete result carries `context_is_untrusted: true`. The fixed specialist
instruction treats video, audio, dialogue, captions, descriptions, and on-screen
text as evidence, never instructions. Fast cuts, brief overlays, OCR, and details
between sampled frames may be missed. Timestamps are useful grounding, not
editing-grade frame accuracy. Google Interactions supports processing-mode,
clipping, and custom-FPS inputs, but this bot does not expose or send those
controls; it uses Google's default video-processing behavior.

## Limits and live tool configuration

Safe per-call behavior remains in `<CONFIG_DIR>/tools/video.md`:

```markdown
---
thinking_level: low
max_output_tokens: 8192
max_calls_per_turn: 4
max_session_interactions: 20
session_ttl_minutes: 1440
---
```

| Field | Default | Allowed range |
|---|---:|---|
| `thinking_level` | `low` | `low`, `medium`, `high` |
| `max_output_tokens` | `8192` | 1,024–32,768 |
| `max_calls_per_turn` | `4` | 1–8 |
| `max_session_interactions` | `20` | 2–50, including `start` |
| `session_ttl_minutes` | `1440` | 5–1,440 |

The 500 MiB file ceiling and one-hour duration ceiling are code-owned hard
policy, not fragment knobs. A call consumes its per-turn allowance only after
local source validation reaches provider work.

## Usage, caching, and latency

Files API upload/polling is not an LLM ledger row. Every completed Interaction
records one `LLMUsageCall` under `video_analysis`, splitting ordinary input,
cached input, and output including thought tokens. It stores no source content,
question, or answer.

Google defines each continuation's input usage as the complete context processed
for that call, including preceding turns, so the bot records full response usage
rather than deltas. Cache hits are automatic but not guaranteed.

Pricing is resolved from the `config/models.yaml` rate card named by each
session's pinned catalog model. For a new session that is the model currently
assigned to `roles.video`; later role changes do not rewrite it. The vendor
dashboard remains authoritative.

Up to `VIDEO_UNDERSTANDING_MAX_CONCURRENCY` questions (default 4, range 1–32)
can be in flight at once; a call that waits more than 30 seconds for a slot
fails with a busy error. Uploads run one at a time and are bounded to 30 minutes
end to end, with the processing poll capped at 15 minutes inside that. A stored
Interaction create or upload initialization makes at most two retries, and only
for explicit 408, 425, or 429 responses. Numeric and HTTP-date `Retry-After`
values are honored when they fit the 30-second per-retry wait ceiling; a
longer provider minimum fails the call instead of retrying early. Without a valid
header, bounded exponential backoff uses full jitter. Transport failures and
ambiguous 5xx responses are not replayed because they can follow a state-changing
request whose resource ID was lost. Resumable chunk failures are different: the
client queries Google's committed byte offset before deciding whether to continue.
Provider deletion uses its own small pool with a 30-second per-request deadline.

## Retention, privacy, and deletion

Local sessions expire after at most 24 hours idle. Expiry, transcript retention,
and full `/privacy` remove local access and enqueue every known Interaction and
Files API resource for deletion. Interaction deletion is completed before its
backing File deletion. Provider cleanup drains in bounded batches with capped
one-minute-to-six-hour exponential backoff; no attempt count discards privacy
cleanup metadata.

Files API uploads are independently documented by Google as retained for up to
48 hours, and paid-tier Interactions for up to 55 days. The bot manually requests
deletion and does not rely on those clocks. If Google or the credential is
unavailable during `/privacy`, local deletion still completes, the user barrier
is released, and content-free provider deletion rows remain queued.

Google may retain limited safety/security logs, backups, or legally required
records separately. Deployments should use a dedicated billing-enabled project;
Google's terms say paid-service prompts and responses are not used to improve
its products, while unpaid-service data may be.

## Operator checklist

1. Use a dedicated billing-enabled Gemini project and key.
2. Configure and verify a `gemini_interactions` provider, `video_input` model
   and current rate card, then assign it to `roles.video`.
3. Enable video understanding and restart.
4. Confirm startup reports `video understanding`.
5. Test a public YouTube URL, a small Discord MP4, a workspace video, one
   follow-up, and `/privacy` cleanup before broad use.
6. Monitor video duration, provider storage/quota, latency, token usage, and
   cache ratios rather than assuming follow-ups are cheap.

Current Google references:

- [Video understanding](https://ai.google.dev/gemini-api/docs/video-understanding)
- [Files API](https://ai.google.dev/gemini-api/docs/files)
- [File input methods](https://ai.google.dev/gemini-api/docs/file-input-methods)
- [Interactions API](https://ai.google.dev/gemini-api/docs/interactions-overview)
- [Interaction token accounting](https://ai.google.dev/gemini-api/docs/interactions/tokens)
- [Gemini API pricing](https://ai.google.dev/gemini-api/docs/pricing)
- [Gemini API data terms](https://ai.google.dev/gemini-api/terms)
