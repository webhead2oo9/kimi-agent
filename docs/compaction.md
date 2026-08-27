# ReAct-loop context compaction

When a single tool-heavy turn's projected request approaches the model window,
`agent/compaction.py:Compactor` summarizes the oldest whole iterations of the
in-loop history into one untrusted, user-role progress note. It keeps the
active user message pinned at the front and restates it at the tail (see
"Request re-anchor"), keeps a verbatim tail of recent whole iterations (as many
as fit in the `COMPACTION_KEEP_RECENT_TOKENS` budget, and never fewer than
`COMPACTION_KEEP_RECENT_ITERATIONS`), and touches only the in-flight request.
The persisted channel transcript is never compacted. If the same turn compacts
again, the existing note is folded into the replacement summary, so notes roll
forward rather than piling up.

- **Trigger:** compaction fires when projected input tokens reach
  `COMPACTION_TRIGGER_TOKENS`. The projection takes the provider's own measured
  input-token count from the last response and adds an estimate of the
  serialized delta appended since. Estimates divide characters by
  `_CHARS_PER_TOKEN` (3.5), which is under the roughly 4 that English prose
  runs at, because code and JSON tool output are denser. The estimate therefore
  overstates tokens and trips compaction slightly early, which is the safe side
  of overflowing the window. If a response carries no usable usage figures, the
  whole request is estimated that way instead.
- **Summarizer:** the summarizer is a swappable provider chosen via
  `roles.compaction` in `config/models.yaml` (with an optional
  `compaction_fallbacks` chain, like any other role; see
  [providers.md](providers.md)). The profile may name any key in the
  `api_key_env` closed set, `COMPACTION_API_KEY` among them, or leave it blank
  the way a Codex profile or a keyless gateway does. The prompt asks for a
  comprehensive structured handoff (facts with attribution, artifact paths,
  commands run and their outcomes, ruled-out approaches, remaining work) with a
  word target scaled to the amount of material being replaced (~1 word per 25
  prefix tokens, clamped 300-4000); the note itself is capped by
  `COMPACTION_MAX_TOKENS`.
- **Plan carry-over:** when the turn has a live `plan`-tool checklist
  (`MessageContext.plan`), both compactor entry points receive it and
  `note_message` re-appends it to the note verbatim, as the tool-echo JSON under
  a "Current checklist" header. That way the checklist survives summarization
  intact instead of coming out lossy. The summarizer is told to skip any
  checklist block already in the compacted note, since the live one is always
  re-appended. On the summarizer-failure fallback the elided prefix is followed
  by a checklist-bearing note of its own (elision could middle-cut the plan
  tool's echo), and a `split == 0` pass appends a small "nothing summarized this
  pass" checklist note for the same reason. Hard truncation only targets tool
  bodies and never touches a note.
- **Request re-anchor:** once a compaction actually summarizes something
  (`split > 0`), the turn's own triggering request is restated as the final
  message so it stays the most salient thing the model reads. Without this, the
  fresh note plus the verbatim tail would sit between the pinned front copy and
  the point of action and could bury the ask. `request_reminder_message`
  rebuilds it from the pinned user message: text only (attachments already ride
  their own rails) and head/tail-capped at ~4 KB. The cap matters because the
  user-role reminder is exempt from elision and hard truncation, so an uncapped
  copy of a command-path prompt (which can embed a whole transcript window)
  could pin the request permanently over budget. Its header grants no extra
  trust: command-path triggering prompts are dominated by third-party content,
  and the restatement keeps it exactly as untrusted as the original. The
  existing re-anchor is stripped before each compaction and re-appended (on the
  `split == 0` branch too), so exactly one copy rides the tail and the anchor
  survives every later pass. A `split == 0` pass summarizes nothing: it
  refreshes the checklist note (when a plan is live) and the re-anchor (when one
  exists), and otherwise leaves the turn as it is.
- **Fallbacks:** if the summarizer fails, the compactor falls back to in-place
  tool-body elision; if the request is still over budget after that, the
  largest remaining tool bodies are hard-truncated. Neither elision nor hard
  truncation ever drops a tool body whole: each keeps a head/tail slice (start
  + end), so large reads and logs retain their context.
- **Guards:** two further guards apply: a per-iteration tool-output budget, and
  heuristic context-overflow detection that performs one emergency compaction
  and retry. The budget is set in tokens by
  `COMPACTION_MAX_ITERATION_TOOL_OUTPUT_TOKENS` and enforced against a running
  character count, converting at 4 characters per token. Results in an
  iteration are kept whole until the budget runs out; the one that crosses it
  is head/tail-capped to whatever remains, and once too little remains to be
  worth slicing, the rest of that iteration's results collapse to a
  content-free stub naming the tool and the size dropped.
- **Applicability:** compaction is mandatory for every chat provider. The loop
  also carries a defensive branch for a provider declaring
  `SERVER_SIDE_CONTEXT`: if local compaction rewrites the client transcript,
  server-side continuation state is cleared so the next request rebases on the
  compacted transcript. No shipped provider declares that capability today, so
  the branch is currently unreachable. We keep it anyway; see the note in
  `agent/core.py`.
- **Capacity warning:** at startup, every reachable chat model's
  `context_window` from `config/models.yaml` is checked against
  `COMPACTION_TRIGGER_TOKENS + REACT_MAX_TOKENS`.
