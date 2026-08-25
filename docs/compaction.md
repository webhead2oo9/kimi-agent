# ReAct-loop context compaction

When a single tool-heavy turn's projected request approaches the model window,
`agent/compaction.py:Compactor` summarizes the oldest whole iterations of the in-loop
history into one untrusted, user-role progress note. It keeps the active user message
pinned at the front and restates it at the tail (see "Request re-anchor"), keeps a
verbatim tail of recent whole iterations (as many as fit in the
`COMPACTION_KEEP_RECENT_TOKENS` budget, never fewer than
`COMPACTION_KEEP_RECENT_ITERATIONS`), and touches only the in-flight request. The
persisted channel transcript is not compacted. A later compaction in the same turn
folds the previous note into the new summary, so notes roll forward.

- **Trigger:** projected input tokens reach `COMPACTION_TRIGGER_TOKENS`. The
  projection adds the provider's own measured input-token count from the last
  response to an estimate of the serialized delta appended since. Estimates divide
  characters by `_CHARS_PER_TOKEN` (3.5), under the roughly 4 that English prose
  runs at, because code and JSON tool output are denser; the estimate therefore
  overstates tokens and trips compaction slightly early instead of overflowing the
  window. A response carrying no usable usage figures falls back to estimating the
  whole request that way.
- **Summarizer:** swappable provider via `roles.compaction` in
  `config/models.yaml` (with an optional `compaction_fallbacks` chain, like any
  other role; see [providers.md](providers.md)). The profile may name any key in
  the `api_key_env` closed set, `COMPACTION_API_KEY` among them, or leave it
  blank the way a Codex profile or a keyless gateway does. The prompt asks for a
  comprehensive structured handoff (facts with attribution, artifact paths,
  commands run and outcomes, ruled-out approaches, remaining work) with a word
  target scaled to the amount of material replaced (~1 word per 25 prefix tokens,
  clamped 300-4000); the note is capped by `COMPACTION_MAX_TOKENS`.
- **Plan carry-over:** when the turn has a live `plan`-tool checklist
  (`MessageContext.plan`), both compactor entry points receive it and
  `note_message` re-appends it to the note verbatim as the tool-echo JSON under a
  "Current checklist" header, so the checklist survives summarization instead of
  coming out lossy. The summarizer is told to skip any checklist block found in a
  prior note (the fresh one is always re-appended). On the summarizer-failure
  fallback the elided prefix is followed by a checklist-bearing note of its own
  (elision could middle-cut the plan tool's echo), and a `split == 0` pass appends
  a small "nothing summarized this pass" checklist note for the same reason; hard
  truncation only targets tool bodies and never touches a note.
- **Request re-anchor:** once a compaction summarizes (`split > 0`), the turn's own
  triggering request is restated as the final message so it stays the most salient
  thing the model reads. Otherwise the fresh note plus the verbatim tail sit
  between the pinned front copy and the point of action, and can bury the ask.
  `request_reminder_message` rebuilds it from the pinned user message: text only
  (attachments already ride their own rails) and head/tail-capped at ~4 KB. The
  user-role reminder is exempt from elision and hard truncation, so an uncapped copy
  of a command-path prompt (which can embed a whole transcript window) could pin the
  request permanently over budget. Its header grants no extra trust: command-path
  triggering prompts are dominated by third-party content, and the restatement keeps
  it exactly as untrusted as the original. A prior re-anchor is stripped before the
  next compaction and re-appended fresh (on the `split == 0` branch too), so exactly
  one copy rides the tail and the anchor survives every later pass. A `split == 0`
  pass summarizes nothing: it refreshes the checklist note (when a plan is live) and
  the re-anchor (when one exists), and otherwise leaves the turn as-is.
- **Fallbacks:** summarizer failure falls back to in-place tool-body elision; if the
  request is still over budget, the largest remaining tool bodies are hard-truncated.
  Elision and hard-truncation never drop a tool body whole: they keep a head/tail
  slice of each one (start + end), so large reads/logs retain context.
- **Guards:** a per-iteration tool-output budget and heuristic context-overflow
  detection, which performs one emergency compaction and retry. The budget is set
  in tokens by `COMPACTION_MAX_ITERATION_TOOL_OUTPUT_TOKENS` and enforced against a
  running character count, converting at 4 characters per token. Results in an
  iteration are kept whole until the budget runs out; the one that crosses it is
  head/tail-capped to whatever remains, and once too little remains to be worth
  slicing, the rest of that iteration's results collapse to a content-free stub
  naming the tool and the size dropped.
- **Applicability:** compaction is mandatory for every chat provider. The loop also
  carries a defensive branch for a provider declaring `SERVER_SIDE_CONTEXT`: if
  local compaction rewrites the client transcript, server-side continuation state
  is cleared so the next request rebases on the compacted transcript. No shipped
  provider declares that capability today, so the branch is currently unreachable.
  It is kept anyway; see the note in `agent/core.py`.
- **Capacity warning:** startup checks every reachable chat model's
  `context_window` from `config/models.yaml` against
  `COMPACTION_TRIGGER_TOKENS + REACT_MAX_TOKENS`.
