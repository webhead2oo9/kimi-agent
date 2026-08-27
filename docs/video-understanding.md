# Video understanding

The optional `video` tool lets Kimi ask a stateful Gemini specialist questions
about public YouTube videos. It is a searchable `MEMBER` tool: the chat model
loads it through `browse_tools` only when a request needs video analysis, and
activation then follows the rooted conversation like other searchable tools.

The implementation sends the YouTube URL directly to Google's Gemini
Interactions API. It never downloads, transcodes, or stores YouTube audiovisual
content on the bot host.

## Availability

Registration is fail-closed and requires both settings:

```dotenv
VIDEO_UNDERSTANDING_ENABLED=true
GEMINI_API_KEY=...
```

The default is disabled. If the flag is false, the tool is absent regardless of
the key. If the flag is true but the key is blank, startup continues, logs a
clear warning, and leaves the tool absent. Dispatch and `browse_tools` mask an
absent tool in the normal way.

`GEMINI_API_KEY` is an environment-only `SecretStr`. It is never accepted from
a tool call, tool fragment, model catalog, prompt, or Discord command. The
client connects only to Google's fixed
`https://generativelanguage.googleapis.com` endpoint. The key is still used for
provider-side cleanup when the feature flag is later disabled, so an operator
can turn off new sessions without making old stored Interactions undeletable.

The shipped model is the stable `gemini-3.7-flash`. This is a specialist owned
by the tool, not a role in `config/models.yaml`, and it never substitutes for
the configured chat model.

## Model-facing operations

One tool owns two operations.

### `start`

`start` requires a public YouTube URL and a specific question. The handler:

1. accepts only exact HTTPS YouTube hosts and one valid video id;
2. canonicalizes supported watch, short, live, embed, and `youtu.be` forms to a
   normal watch URL;
3. sends the video before the question to Gemini 3.7 Flash with `store=true`;
4. stores an opaque local session handle and the returned Interaction id; and
5. returns the answer, limitations, and timestamped evidence as untrusted tool
   context.

Only public videos are supported. Private, unlisted, age/region-restricted,
deleted, or otherwise unavailable videos fail with a concise error. Playlists
without one video id, non-YouTube URLs, credentials, fragments, and explicit
ports are rejected before any provider call.

### `ask`

`ask` continues the Gemini chain through `previous_interaction_id`; the URL and
prior turns are not reconstructed or resent by the bot. Pass the opaque session
handle returned by `start`. It may be omitted only when exactly one unexpired
session belongs to the current user in the current rooted conversation. More
than one match fails closed and asks the model to select a handle.

The same interaction-scoped system instruction, structured response format,
thinking level, and output cap are repeated on every call because
`previous_interaction_id` preserves conversation history, not those controls.
A session keeps the model it started with even if live tool configuration is
edited later.

## Session scope and lifetime

A local handle is not authority by itself. Every lookup rechecks:

- rooted SQLite `conversation_id`;
- initiating Discord `user_id`;
- guild id;
- expiry; and
- the expected latest remote Interaction id during an atomic advance.

Another member in a shared channel root therefore cannot continue someone
else's provider session, and a leaked handle from another guild or root does
nothing. Concurrent advances use compare-and-swap semantics; the loser deletes
its newly created remote Interaction rather than forking the local chain.

Sessions survive normal turns and process restarts in SQLite schema v3. Their
idle lifetime defaults to 24 hours and can only be reduced through live tool
configuration. Each follow-up extends the idle deadline. Expiry deletes the
local session immediately and queues every known Gemini Interaction in the
session for provider deletion. Deleting the parent conversation through
transcript retention does the same through a database trigger.

Google currently documents paid-tier Interaction retention of up to 55 days.
The local 24-hour session clock is not a claim that Google has already deleted
its copy: the bot calls Google's Interaction deletion endpoint and keeps a
durable, content-free retry row until that succeeds or Google reports the id is
already absent.

## Output and trust

Gemini is required to return structured data:

- `answer`;
- zero or more evidence ranges with start/end seconds, basis, and claim; and
- explicit limitations.

The tool adds readable timestamps and clickable `&t=<seconds>s` YouTube links.
Evidence distinguishes `audio`, `visual`, `audio_and_visual`, and `inference`.
Malformed, incomplete, or empty provider responses fail instead of being passed
through as prose.

The complete result carries `context_is_untrusted: true`. The specialist's
fixed system instruction treats video, audio, dialogue, captions, descriptions,
and on-screen text as evidence and never as instructions. This matters because
a video can contain prompt injection just as easily as a web page can. Kimi
must not execute a request merely because a speaker or title card asked it to.

Gemini's video processing is sampled and probabilistic. Timestamp evidence is
useful grounding, not editing-grade frame accuracy; fast cuts, brief overlays,
and details between sampled frames may be missed. The MVP intentionally does
not expose clipping or custom FPS because those `video_metadata` controls are
available in `generateContent`, while stateful continuation lives in the
Interactions API.

## Limits and live tool configuration

Safe behavior lives in `<CONFIG_DIR>/tools/video.md` and is read fresh each
turn. No file is required; these are the shipped defaults:

```markdown
---
model: gemini-3.7-flash
thinking_level: low
max_output_tokens: 8192
max_calls_per_turn: 4
max_session_interactions: 20
session_ttl_minutes: 1440
---
```

| Field | Default | Allowed range |
|---|---:|---|
| `model` | `gemini-3.7-flash` | closed model choice |
| `thinking_level` | `low` | `low`, `medium`, `high` |
| `max_output_tokens` | `8192` | 1,024–32,768 |
| `max_calls_per_turn` | `4` | 1–8 |
| `max_session_interactions` | `20` | 2–50, including `start` |
| `session_ttl_minutes` | `1440` | 5–1,440 |

A call that reaches Gemini consumes the per-turn allowance even if the provider
rejects the video. Internal HTTP behavior, endpoint selection, credentials, and
provider retention are not model- or fragment-configurable.

## Usage, caching, and latency

Every completed specialist call records an `LLMUsageCall` with role
`video_analysis`. The ledger stores ordinary input, cached input, and output
(including thought) token counts, but not the URL, question, or answer. The tool
ships Google's scheduled paid-tier standard rates for Gemini 3.7 Flash: through
December 31, 2026, $0.75/M ordinary input, $0.075/M cached input, and $3.75/M
output; from January 1, 2027, $1.50/M, $0.15/M, and $7.50/M respectively. The
vendor dashboard remains authoritative if Google changes those published rates.

The Interactions API enables implicit caching automatically. Stateful
`previous_interaction_id` chains improve cache locality, but a follow-up is not
assumed cheap: Google defines each interaction's input usage as the complete
context processed for that call, including preceding turns. The bot therefore
records every response's full input count—not deltas between chain snapshots—
and splits out `total_cached_tokens` at its discounted rate. Output and thought
counts are for the current generation. A live three-turn check confirmed input
rising with chain history while output remained per-turn. The principal
guaranteed wins are server-side continuity and not transferring the whole
history from the bot; latency and cache hits must be measured in production.

Provider calls use one application-owned async client and a fixed five-minute
request deadline. `VIDEO_UNDERSTANDING_MAX_CONCURRENCY` controls 1–32 concurrent
interactive calls (default 4); waiting for an interactive slot is capped at 30
seconds. Provider deletion has a separate bounded pool and 30-second request
deadline, so cleanup never occupies interactive slots or waits on a five-minute
analysis deadline. The service never retries a create blindly, because
losing the response and repeating it could create an untracked stored
Interaction.

## Retention, privacy, and deletion

The local tables are `video_sessions`, `video_interactions`, and the
content-free `video_interaction_deletions` outbox. Session rows contain the
canonical public URL, model, owner/root scope, timestamps, and opaque local and
provider ids. They do not contain questions or answers; those remain in
Google's stored Interaction and any normal Discord reply that enters Kimi's
ordinary transcript.

A full `/privacy` deletion removes all sessions initiated by that user,
including sessions inside a surviving shared root, then immediately attempts up
to four provider deletion rows. Provider cleanup is independently durable: if Google or
the credential is unavailable, the privacy request still completes after local
data is gone, the user barrier is released, and the result says that Gemini
deletion remains queued. This avoids indefinitely locking out a user while
preserving eventual provider cleanup.

The hourly video-session sweeper expires sessions and retries provider deletes.
Its startup pass runs in the installed background task rather than blocking a
Discord READY event. Cleanup drains at most 100 rows per pass; failures use
capped exponential backoff from one minute to six hours, and retry-ready rows
are ordered ahead of delayed failures so a poison row cannot starve new work.
Rows are never dropped merely for reaching an attempt count. The sweeper runs
even when new video analysis is disabled. Without `GEMINI_API_KEY`, it can
remove expired local state but must leave provider deletion rows queued until
provider access is restored.

Google may separately retain limited safety/security logs under its paid-service
terms. Deleting an Interaction is not a promise that provider backups or legally
required records disappear immediately. Deployments should use a billing-enabled
Gemini project: Google's terms say paid-service prompts and responses are not
used to improve its products, while unpaid-service data may be.

## Operator checklist

1. Use a dedicated, billing-enabled Gemini project and key.
2. Set both registration values and restart.
3. Confirm the startup capability summary lists `video understanding`.
4. Optionally create `<CONFIG_DIR>/tools/video.md` with lower limits first.
5. Test one public YouTube URL, one follow-up, and `/privacy` provider deletion
   before enabling the tool broadly.
6. Monitor `video_analysis` usage and cached-token ratios rather than assuming a
   follow-up cost.

Current Google references:

- [Gemini video understanding](https://ai.google.dev/gemini-api/docs/video-understanding)
- [Interactions API](https://ai.google.dev/gemini-api/docs/interactions/interactions-overview)
- [Interactions API reference](https://ai.google.dev/api/interactions-api)
- [Interaction token accounting](https://ai.google.dev/gemini-api/docs/interactions/tokens)
- [Gemini API pricing](https://ai.google.dev/gemini-api/docs/pricing)
- [Gemini API data terms](https://ai.google.dev/gemini-api/terms)
