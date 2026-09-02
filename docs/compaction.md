# ReAct-loop context compaction

When a tool-heavy turn runs long, the next request to the model can get close to the model's context window. Instead of failing or silently dropping history, Kimi runs an automatic compaction step that summarizes the oldest tool iterations into a single progress note. The turn keeps moving, and the important facts, file paths, and decisions stay in view.

Compaction only changes what is sent to the model for the current turn. The saved channel transcript is never touched. The same turn can compact more than once; each new summary folds the previous note into itself, so notes roll forward rather than piling up.

## Why it exists

A turn that reads files, runs tests, and fixes things for twenty iterations can outgrow even a generous context window. Without compaction, the turn would either hit a hard limit or have to throw away earlier work. Compaction hands the model a structured summary of what happened so far, keeps the original request visible, and leaves the most recent iterations untouched. The goal is to stay under budget without losing the context that matters.

## When compaction fires

Compaction fires when the estimated size of the next request reaches `COMPACTION_TRIGGER_TOKENS` (120,000 by default). The estimate starts from the input-token count the provider reported for the previous call and adds an estimate for everything appended since. That estimate is deliberately pessimistic (characters divided by 3.5 rather than the more usual 4), so compaction tends to fire a little early. Early is the safe side of overflowing the window.

**Tuning guidance:**

- Raise the trigger if you want fewer compaction calls and are confident your models can handle larger windows.
- Lower it if you see frequent hard truncations or if the summarizer is cheap and reliable.
- The trigger must leave enough room for `REACT_MAX_TOKENS` of new output. At startup Kimi checks that every reachable chat model's `context_window` satisfies `COMPACTION_TRIGGER_TOKENS + REACT_MAX_TOKENS`.

If the provider did not report usage figures, the whole request is estimated the same pessimistic way.

## The summarizer role

The summary is written by whichever model `roles.compaction` names in `config/models.yaml`. You can add `compaction_fallbacks` as for the other general LLM roles. Every referenced model must use a general provider; lifecycle-owned specialized profiles such as `gemini_interactions` cannot serve compaction. Authentication follows each profile: it can use a compatible supported key named in `api_key_env`, including the dedicated `COMPACTION_API_KEY`, or run without a key.

The prompt asks for a thorough, structured handoff: facts and who said them, file paths, commands run and what they returned, approaches ruled out, and what is left to do. The target length scales with how much material is being replaced (about 1 word per 25 tokens summarized, kept between 300 and 4,000 words). The note itself is capped by `COMPACTION_MAX_TOKENS` (32,768 by default).

**Practical notes on the summarizer:**

- Choose a model that is cheap enough to call frequently but capable enough to produce accurate, attributed summaries. A weak summarizer can lose important context.
- The summarizer only sees the material being compacted. It never receives the live user request or the recent tail.
- If the summarizer fails, Kimi falls back to tool-body elision and then hard truncation (see below).

## What survives compaction

Three things are deliberately protected:

- **The live plan checklist**: If the turn has an active `plan` tool checklist, it is re-appended verbatim under a "Current checklist" header after every compaction. The summarizer is told to skip any checklist block already present so the live version always wins.
- **The original request**: Once compaction has summarized something, the turn's triggering request is restated as the final message. This keeps the point of action salient instead of burying it between the new note and the recent tail. The restatement is text-only, head/tail-capped, and treated as untrusted input.
- **A recent tail of raw iterations**: The most recent whole iterations are kept word for word, up to `COMPACTION_KEEP_RECENT_TOKENS` (50,000 by default) and never fewer than `COMPACTION_KEEP_RECENT_ITERATIONS` (3 by default). This gives the model fresh, unsummarized context for the current step.

If a compaction pass finds nothing old enough to summarize, it still refreshes the checklist note and the restated request when they exist.

## Example: a turn before and after compaction

Here is a fictional but realistic sequence. A user asks the bot to refactor a small module. The bot reads, edits, runs tests, fixes a flaky assertion, runs again, and is now ready to send its next request after the 12th iteration.

The message list is simplified for readability. `system` is the system prompt with all its instruction fragments; `user(...)` and `assistant(...)` are real conversation messages; `tool(...)` is the result message from the named tool. Sizes are illustrative, not literal.

**Step 1: the compactor measures the projected request.** The provider's last reported input-token count was 110k for iteration 11. The compactor estimates the new request at 11k tokens (system + user ask + iterations 1–12, characters / 3.5; deliberately conservative). Projected total: 121k, which crosses the default `COMPACTION_TRIGGER_TOKENS` of 120k. Compaction fires.

**Before compaction**, the message list the compactor is about to rebuild:

```
[system,                                                 # 4k
 user(refactor_request),                                 # 200, the original ask
 iter_1:  assistant(read_file),   tool(read_file_result), # 12k
 iter_2:  assistant(edit),        tool(edit_result),      # 1k
 iter_3:  assistant(edit),        tool(edit_result),      # 1k
 iter_4:  assistant(run_tests),   tool(run_tests_result), # 18k
 iter_5:  assistant(edit),        tool(edit_result),      # 1k
 iter_6:  assistant(run_tests),   tool(run_tests_result), # 19k
 iter_7:  assistant(edit),        tool(edit_result),      # 1k
 iter_8:  assistant(run_tests),   tool(run_tests_result), # 18k
 iter_9:  assistant(view_image),  tool(view_image_result),# 22k
 iter_10: assistant(edit),        tool(edit_result),      # 1k
 iter_11: assistant(run_tests),   tool(run_tests_result), # 19k
 iter_12: assistant(plan_update), tool(plan_update_result)] # 1k
                                                            # total ~121k
```

**Step 2: split.** With `COMPACTION_KEEP_RECENT_ITERATIONS=3` and enough tail budget, the compactor keeps iterations 10–12 verbatim (21k) and sends iterations 1–9 (93k) to the summarizer.

**Step 3: summarize.** The summarizer is asked to compress iterations 1–9 into one structured progress note. The live plan is preserved verbatim and re-appended under a "Current checklist" header inside that note. The summarizer returns ~3k tokens.

**After compaction**, the message list actually sent to the provider:

```
[system,                                                 # 4k
 user(refactor_request),                                 # 200, the original ask
 assistant(note_message(
   "Earlier work on the refactor:
    - read original module.py (lines 1-80)…
    - applied 3 edits to add new helper…
    - tests pass on iter 8; flake on iter 11…
    - remaining: confirm coverage, run lint.
    Current checklist:
    - [x] read module.py
    - [x] apply refactor edits
    - [x] run tests
    - [ ] run coverage
    - [ ] run lint"
 )),                                                     # 3k
 iter_10: assistant(edit),        tool(edit_result),      # 1k
 iter_11: assistant(run_tests),   tool(run_tests_result), # 19k
 iter_12: assistant(plan_update), tool(plan_update_result),# 1k
 user(request_anchor)]                                   # 200, restated ask, head/tail-capped
                                                            # total ~28k
```

The projected request dropped from 121k to 28k, well below the trigger and with plenty of room for `REACT_MAX_TOKENS` of new output.

Three things to notice:

- The plan checklist survived in fresh form, not the summarizer's wording.
- The original request is restated as the final message, so the model still sees the user's goal after a long, compacted history. The anchor is text-only, head/tail-capped, and treated as untrusted input.
- The most recent three iterations are kept verbatim, not summarized, so the model has unmediated context for the current step.

**If the summarizer fails** (timeout, provider error, malformed output), the compactor falls back to tool-body elision on the same prefix. The shape becomes:

```
[system,
 user(refactor_request),
 assistant(note_message("(summary unavailable; earlier tool output was elided in place)")),
 elided_iter_1, elided_iter_2, …, elided_iter_9,
 iter_10, iter_11, iter_12,
 user(request_anchor)]
```

Each elided iteration still keeps a head/tail slice of every tool body, so large reads and logs retain their surrounding context. The plan still rides the note, because elision can middle-cut the plan tool's own echo. If the request is still over budget after elision, the largest remaining tool bodies are hard-truncated.

**If compaction fires again** on a later request (say iteration 18), the next pass summarizes the **previous** compaction note plus the iterations that came after it. Notes roll forward; they do not stack.

## Fallbacks when the summarizer fails

If the summarizer cannot produce a note, the compactor falls back to in-place tool-body elision. If the request is still over budget after that, the largest remaining tool bodies are hard-truncated. Neither step ever drops a tool body entirely; each keeps a head/tail slice so large reads and logs retain their surrounding context.

Hard truncation only targets tool bodies. It never touches an existing compaction note.

## Additional guards

Two further protections apply during the turn:

- A per-iteration tool-output budget (`COMPACTION_MAX_ITERATION_TOOL_OUTPUT_TOKENS`, 48,000 by default). Tool results in one iteration are kept whole until the budget runs out; the result that crosses the limit is cut to a head and tail slice, and once too little budget remains to be worth slicing, the remaining results collapse to a one-line stub naming the tool and how much was dropped.
- If the provider rejects a request as too large despite the estimate, Kimi recognises the error, runs one emergency compaction, and retries once.

## Observing and debugging compaction

Compaction is silent to the user unless something goes wrong. You can observe it through:

- The normal application logs (look for compaction events, summarizer calls, fallback paths, and token counts).
- The structured event log when `TOOL_EVENT_LOG_ENABLED` is on.
- Provider usage records: the compaction role will show its own spend separate from chat.

Common things to watch:

- Frequent compaction on the same turn may indicate the trigger is too low or the summarizer is producing notes that are still too large.
- Summarizer failures that fall back to elision or truncation can lose fidelity. Consider a stronger or more reliable compaction model if this happens often.
- If a provider declares the `SERVER_SIDE_CONTEXT` capability (none of the shipped providers do), compaction also clears its server-side continuation so the next request is rebuilt from the compacted transcript.

## Tuning checklist

When adjusting compaction behavior, consider these in order:

1. Start with the trigger threshold (`COMPACTION_TRIGGER_TOKENS`). This has the biggest effect on how often compaction runs.
2. Choose a summarizer model that balances cost and summary quality.
3. Tune the recent-tail budget (`COMPACTION_KEEP_RECENT_TOKENS` / `COMPACTION_KEEP_RECENT_ITERATIONS`) if you find the model is losing too much fresh context.
4. Only adjust the per-iteration tool-output budget or max note size if you have specific evidence that the defaults are causing problems.

Compaction is always on, for every chat provider. It is one of the things that lets Kimi handle long, tool-heavy turns without collapsing under its own history.
