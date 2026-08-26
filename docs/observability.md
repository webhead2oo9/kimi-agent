# Observability: the tool event stream

The bot can emit structured JSON lines for tool calls, turn summaries, and
context compaction events. By default those lines carry metadata only: timings,
ids, tool names, and outcomes. Getting the actual arguments, results, prompts,
and replies into the file takes a deliberate environment setting. The stream is
plain JSONL, so `tail -f logs/events.jsonl | jq .` works directly.

The stream is **off by default**. While it is disabled, no writer is started
and `emit_*` calls return without writing anything. The file is the whole
output contract; the bot does not depend on anything consuming it.

`module_health` events record every application-module health transition:
`module`, `state` (`starting`/`healthy`/`degraded`/`failed`), a truncated
`detail`, and bounded numeric `metrics`. They carry no message content.

## Enabling it

These settings live in `config/settings.py` and are mirrored in `.env.example`:

| Setting                         | Default             | Meaning                                              |
| ------------------------------- | ------------------- | ---------------------------------------------------- |
| `TOOL_EVENT_LOG_ENABLED`        | `false`             | Master switch. When off, nothing is written.         |
| `TOOL_EVENT_LOG_PATH`           | `logs/events.jsonl` | Append target.                                       |
| `TOOL_EVENT_LOG_CONTENT_MODE`   | `metadata`          | How much content each row carries: `metadata`, `redacted`, or `full`. Set here only, never from the overlay. |
| `TOOL_EVENT_LOG_MAX_FIELD_BYTES`| `8192`              | Per-field cap on `args`/`result`, each `request` message, and `response` before truncation. |

```bash
TOOL_EVENT_LOG_ENABLED=true uv run python bot.py
```

The three content modes trade detail for safety. `metadata` keeps tool
payloads, prompts, memories, user messages, and assistant replies out of the
file entirely. `redacted` writes them, but first strips values under
credential-looking keys and any secret it already knows from `Settings`.
`full` is the verbatim stream, unchanged; it warns at startup, because what
lands in the file can include both secrets and private conversation. `logs/`
is git-ignored.

## Data flow

Each turn carries one `turn_id`. On the production path `agent/turn.py` mints
it and passes it in via `ConversationRunRequest.turn_id`. The same id stamps
the turn's `usage_ledger` and `paid_usage_ledger` rows, so JSONL events join
directly to ledger rows; `agent/core.py:run_conversation` generates an id
itself only for direct callers that supply none.

At the dispatch boundary the loop calls `observability.events.emit_tool_call(...)`
for every tool the model invokes, including bad-tool-name and unparseable-args
attempts, which carry `ok: false` from their `{"error": ...}` result. If
in-loop context compaction runs, the loop calls `emit_compaction(...)` with the
compaction reason and the message and tool-result counts. A tool with its own
private dispatch loop can attach child `tool_call` events to the same turn by
reusing `ctx.tool_event_turn_id`, which lets a JSONL consumer correlate the
sub-loop with the outer call. No shipped tool does this today; the turn's own
`tool_count` counts only what the outer ReAct loop dispatched, so a sub-loop's
rows are visible in the stream but not in that total.

When the turn produces its final response (or ends on the max-iteration
fallback or the whole-turn wall-clock timeout) the loop calls `emit_turn(...)`.
Under `redacted` or `full`, that row carries a snapshot of the iteration-0
model input and the final assistant text; under `metadata` both fields are
`null`. Recent channel history shows up in that snapshot only if the model
explicitly called `get_channel_context`.

`emit_*` are no-ops unless the writer was started.
`KimiApplication.on_ready` calls `start_event_writer(...)` when
`TOOL_EVENT_LOG_ENABLED` is set, and application shutdown flushes via
`stop_event_writer()`.

The writer (`observability/events.py:EventWriter`) is non-blocking: `emit_*`
only enqueue onto an `asyncio.Queue`, and a single background task drains it,
appends one line, and flushes. The ReAct loop never touches the disk. If the
queue fills, the writer drops events with a one-time warning; if the path can't
be opened, the writer stays disabled; and an `OSError` during a write stops the
drain task with one warning, after which later events are silently dropped.
Whatever happens, logging never crashes the bot. On POSIX the live file and its
rotated backup are created mode `0600`, readable only by the owner, and if the
file cannot be locked down that way the writer never starts.

## Event schema

Every event is one JSON object on its own line. The current schema is `v: 2`,
and every row names the `content_mode` it was written under. Nothing rewrites
older `v: 1` rows, so a file that straddles the upgrade holds both shapes;
branch on `v` when you read one.

### `tool_call`

```jsonc
{
  "v": 2,
  "type": "tool_call",
  "content_mode": "metadata",
  "ts": "2026-05-31T19:04:22.118Z",  // ISO-8601 UTC, at completion
  "duration_ms": 412,                 // dispatch wall time
  "turn_id": "a1b2c3",                // groups all calls in one ReAct turn
  "iteration": 2,                      // loop index from the emitter
  "user_id": "12345",
  "user_name": "ExampleUser",
  "channel_id": "800000000000000001",
  "thread_id": null,
  "trust_tier": "staff",              // TrustTier value
  "tool": "edit_file",
  "model": "provider/chat-primary",   // model whose response issued this call ("" for child rows)
  "args": null,                       // the requested params object under redacted/full
  "args_truncated": false,
  "result": null,                     // the returned string under redacted/full
  "result_truncated": false,
  "ok": true,                          // derived: see below
  "error": null
}
```

### `turn`

```jsonc
{
  "v": 2, "type": "turn", "content_mode": "metadata",
  "ts": "2026-05-31T19:04:23.900Z", "turn_id": "a1b2c3",
  "user_name": "ExampleUser", "channel_id": "800000000000000001",
  "trigger": "unknown", "tool_count": 1, "duration_ms": 1840,
  // Model attribution: which backend actually answered, so a weird reply is
  // attributable to a specific model even under provider failover.
  "model": "provider/chat-fallback",       // served the FINAL provider call ("" if none completed)
  "models": ["provider/chat-primary", "provider/chat-fallback"], // every serving model, first-use order (compaction-summarizer calls included, so >1 is failover OR compaction)
  "primary_model": "provider/chat-primary", // configured/preferred model; model != primary → fallback-served
  "llm_calls": 3,                          // completed provider calls this turn, compaction calls included
  // Token usage: summed across the turn's provider calls, normalized by
  // usage/normalization.py (same shape the /usage ledger records).
  "usage": { "input_tokens": 5210, "cached_read_tokens": 4100, "cache_write_tokens": 0, "output_tokens": 380 },
  // Both bodies are null in metadata mode. Under redacted/full, `request` is the
  // iteration-0 model input (one entry per message, in the order the model sees
  // them) and `response` is the final assistant text.
  "request": null,
  "response": null
}
```

Under `redacted` and `full`, `request` is a **model-input** view rather than a
byte-for-byte provider request. It omits the full tool JSON schemas (only tool
**names** appear, in the `tools` section) and the generation parameters
(`temperature`, `max_tokens`, capability flags). The `system` and `tools` roles
are synthetic labels, since the system prompt and tool definitions are not
ordinary messages. Keep in mind that `browse_tools` can widen the tool set on
later iterations; this snapshot captures the iteration-0 set only.

Where it is present, `response` holds the final user-facing assistant text for
the turn. Narration from the earlier tool iterations shows up in provider
history and in activity updates, not in this field.

### `compaction`

```jsonc
{
  "v": 2,
  "type": "compaction",
  "content_mode": "metadata",
  "ts": "2026-06-02T01:02:03.004Z",
  "turn_id": "a1b2c3",
  "iteration": 2,
  "user_id": "12345",
  "user_name": "ExampleUser",
  "channel_id": "800000000000000001",
  "thread_id": null,
  "trust_tier": "staff",
  "reason": "threshold",              // "threshold" or "overflow"
  "before_messages": 9,
  "after_messages": 5,
  "kept_recent_iterations": 2,
  "note_chars": 1234,                 // summary-note length, not the note text
  "elided_tool_results": 0,
  "hard_truncated_tool_results": 1
}
```

The `compaction` event records that the in-flight ReAct request was compacted.
It does not include the generated summary text or any tool result body; it
logs only counts, the reason, and size metadata for JSONL consumers.

### `moderation`

```jsonc
{
  "v": 2,
  "type": "moderation",
  "content_mode": "metadata",
  "ts": "2026-06-29T12:00:00.000Z",
  "direction": "input",                  // "input" or "output"
  "matched_categories": ["violence"],     // OpenAI omni-moderation categories that matched
  "category_scores": { "violence": 0.91 }, // float score per returned category
  "user_id": "12345",
  "channel_id": "800000000000000001",
  "thread_id": null,
  "trust_tier": "staff"                    // TrustTier value
}
```

The `moderation` event is emitted by `moderation/service.py` (through
`emit_moderation`) only when the omni-moderation backend returns a real
category match that the policy blocks. Failure-path blocks, where a backend
error or timeout fails input open or output closed, are logged but do not emit
this event. `direction` is `"input"` for the inbound user message or
`"output"` for the bot's reply. The row carries no `turn_id` or `iteration`,
because moderation runs at the message boundary, outside the ReAct dispatch
loop.

### Rules

- **Model attribution:** `providers/failover.py:FailoverProvider` stamps every
  response it returns with the backend that actually served it
  (`ProviderResponse.model`); for a direct provider the core falls back to the
  configured `provider.model`. A turn's `models` list therefore names real
  serving backends, and `primary_model` lets any reader flag fallback-served
  turns without knowing `config/models.yaml`. The usage **ledger** (`/usage`)
  records the same per-call serving-model attribution (one row per LLM call,
  priced from the serving backend's `pricing_model`), so the two surfaces
  agree. Under `redacted` or `full` this stream also carries request and
  response content that the ledger never keeps.
- **Child tool calls:** a `tool_call` row may come from the outer ReAct loop or
  from a tool-owned private loop. Child rows share the outer turn's `turn_id`
  and carry an empty `model` unless the emitting loop names one, but the turn's
  `tool_count` is the outer loop's own dispatch count and does not include
  them. If you need the total, count child rows from the stream. No shipped
  tool emits child rows today.
- **`ok` / `error`** are derived bot-side from the original `result` string: if
  it JSON-decodes to an object containing an `"error"` key, `ok` is `false` and
  `error` carries the message; otherwise `ok` is `true`. A dispatch exception
  already returns `{"error": "Tool execution failed."}` from the registry, so
  it falls into the same path. `ok` is metadata and survives in every mode. The
  error text is content, though, so `metadata` writes `error: null` while the
  other two modes carry it under their own rules.
- **Truncation:** `result` (a string) is capped at
  `TOOL_EVENT_LOG_MAX_FIELD_BYTES` and flagged with `result_truncated`. `args`
  is normally the original object; if its serialized form exceeds the cap it
  degrades to a truncated **string** and `args_truncated` is set. This is what
  keeps a base64 image or a huge file read out of the log.
- **Redaction:** `redacted` walks args and results and replaces two things with
  `[REDACTED]`: any value sitting under a credential-looking key, and any
  non-empty `SecretStr` value from `Settings`, matched exactly. It runs before
  the size cap, so truncation can never cut a secret in half and leave the
  front of it behind. It is best effort, though. A secret the bot transformed
  on the way out, or one a user simply typed into a sentence, goes through
  untouched. `full` redacts nothing.
- **`trigger`** is `"timeout"` when the turn ended on the whole-turn wall-clock
  deadline, and `"unknown"` otherwise (normal completions and the
  max-iteration fallback). `run_conversation` does not yet receive the Discord
  trigger kind, so richer values can be wired later without a `v` bump.
- **`full` is the sensitive one.** It records tool arguments and results, the
  system prompt, transcript and backfill history, recalled memories, attachment
  context, user text, tool names, and the final assistant reply, all verbatim.
  Every mode's file is private instance data and belongs on the box that wrote
  it. Handle a `full` file the way you would handle the secrets inside it.

## Rotation

Rotation is size-based. When the live file would exceed ~50 MB the writer
rolls it to `events.jsonl.1` (a single backup) and starts a fresh file. Both
are ordinary JSONL files, and on POSIX both stay at mode `0600`.
