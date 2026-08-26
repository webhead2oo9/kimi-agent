# Evals

There are two offline runners, and they share one harness: `evals/harness.py`
drives the production `run_conversation` with the real registry, the real
compactor, and a stub Discord gateway. They answer different questions:

- **Model qualification** (`evals.run`) pits a candidate model against an
  operator-selected baseline and has a blind judge score both. This is the
  original go/no-go flow.
- **Harness eval** (`evals.harness_run`) runs one operator-selected model
  repeatedly and scores it with deterministic mechanical rules. This is the
  regression loop you reach for after changing the harness itself: prompts,
  tool descriptions, error text.

Live eval routing belongs in the ignored `evals/models.yaml`; the public
`evals/models.example.yaml` contains placeholders only. Copy the example, then
fill in the baseline, candidates, judge, endpoints, and environment-variable
names for your deployment. Eval API keys resolve from the shell environment
first, then from the ignored repo `.env` (`evals/models.py:resolve_api_key`).

## Harness eval

```bash
# See the plan first
cp evals/models.example.yaml evals/models.yaml
uv run python -m evals.harness_run --model candidate-example --repeat 3 --dry-run

# First run: hybrid cassette mode records live tool results per scenario
uv run python -m evals.harness_run --model candidate-example --repeat 3

# Compare a variant working tree against a reference run
uv run python -m evals.compare evals/runs/harness/<A>/summary.json evals/runs/harness/<B>/summary.json
```

You can point the runner elsewhere with `--models` (default
`evals/models.yaml`), `--scenarios` (`evals/scenarios`), `--cassettes`
(`evals/cassettes`), and `--out` (`evals/runs/harness`), which override the
catalog, scenario directory, tape directory, and output root respectively.

Each run writes an immutable `evals/runs/harness/<utc>-<git-sha>/` directory
containing `summary.json` (a stable schema that `evals.compare` reads),
`report.md`, and `transcripts.jsonl` (per-rep transcripts, useful when you're
triaging a failure). The run fails fast if a scenario expects a tool that the
current `.env` didn't register, and `summary.json` records the registered-tool
surface so that two runs can be compared on equal footing.

Every live run gets an isolated temporary writable-state root. Workspace files,
attachments, personal skills, the browser profile, and the eval database never
use the deployment's corresponding directories, and the temporary root is removed
only after the eval registry closes the browser worker and other composed runtime
owners. The visible fixture author remains `webhead`, but
the caller id is a 20-digit synthetic value above Discord's unsigned 64-bit
snowflake range, derived from SHA-256 over a per-run nonce, model arm, scenario id,
and repetition. Repetitions and qualification arms therefore cannot see one
another's workspace files or accidentally reuse a real user's storage identity.
`summary.json` and `transcripts.jsonl` record the nonce
and derived identity inputs so a stored result remains auditable. The in-memory
coding-control stub is keyed by the same caller id, so task numbering and captured
control actions also start from clean state for every arm and repetition. The
conversation context key includes the full identity digest, keeping generated
artifacts and headless-browser sessions in the same per-arm, per-repetition boundary.

`<git-sha>` is HEAD's short sha. When tracked *source* differs from it, the
identity is suffixed **`-dirty-<diff-hash>`**, where `<diff-hash>` is the first
12 hexadecimal characters of SHA-256 over a configuration-independent binary
diff from HEAD. A run against an edited working tree previously claimed a commit it never
executed, and a later bare `-dirty` marker made every edited tree look
identical. The hash makes equal labels mean equal tracked source bytes in the
working tree. Untracked files and index-only changes are naturally absent from
the worktree diff; if a staged edit has been restored to HEAD in the working
tree, the eval executes the clean HEAD bytes and receives the clean sha. Also
excluded is everything under the
run's tape directory (`harness_run.run_data_paths`, excluded by pathspec). The
default tape tree is gitignored, so it never reaches the check at all; the
pathspec is there for the run that points `--cassettes` at a tracked path. A `replay` run records
misses and promotes baseline entries into that tree as part of doing its job, so
without the exclusion it changes its own diff hash and two runs
of identical code report different trees. Deriving the pathspec from
`--cassettes` rather than fixing it at the default keeps it on the tree the run
actually writes, instead of excusing one the run never touched.

Failure is cautious, not clean. No git at all gives **`nogit`**; a `rev-parse`
that succeeds followed by a diff that does not (the 10s timeout or a permissions
error) gives **`-unknown`**. That is the ambiguous
case, and answering it with a bare sha would stamp a possibly-edited tree with a
commit it may never have executed, which is what the marker exists to prevent.

`--repeat 1` logs a warning. One sample per scenario is not a measurement: we
watched a scenario swing 100 → 35 between identical reps purely from tool
choice, which is why the default is 3.

`--model` resolves a candidate name (or the baseline label) from the ignored
`evals/models.yaml`. The tracked example has nothing to do with production.

`--max-tokens` is the per-provider-call output cap and defaults to 65,536,
matching the production ReAct budget. It is written into `summary.json` and the
report header so two arms cannot silently run under different output budgets.
Note this is separate from the number of ReAct calls a scenario may make. A
model spec may declare `max_output_tokens` when a hosted deployment enforces a
lower ceiling; the harness clamps to that ceiling, logs it, and records both the
requested and effective values rather than sending a run of requests the
provider will reject.

An OpenAI-compatible model spec may also declare `request_id_header`. The
provider then sends that header with a fresh UUID on every model call, which
supports gateways that expose caller request IDs without hard-coding a vendor
into the shared provider.

### Cassettes (record/replay)

Tool calls are recorded and replayed at the eval registry's dispatch chokepoint
(`evals/cassette.py`), one JSON file per scenario, laid out as

```
evals/cassettes/<scenario-id>.json              # shared baseline, read-only
evals/cassettes/<model-key>/<scenario-id>.json  # this arm's tape (the only file a run writes)
```

`<model-key>` is `ModelSpec.label` slugged (`cassette_model_key`). It comes
from the label rather than `model` (which carries provider path segments) or
the `--model` argument (which can be a second spelling of the same arm). Two
arms whose labels slug to the same key abort the run instead of sharing a tape,
because the key encodes neither provider nor reasoning effort. The entire
cassette tree is ignored private eval data, since it contains recorded tool
arguments and results. If you need reproducibility across machines, back it up
or transfer it through an access-controlled artifact store. `evals/cassettes/`
is created by the first recording run; until then `--dry-run` reports `none`
for every scenario.

The `--cassette` modes are:

- `replay` (default, hybrid): replay recorded `(tool, args)` results, fall
  through live on a miss and record it. The first run records, and later runs
  replay recorded source calls deterministically without paying for them.
  Tools intentionally kept outside cassettes, such as `fetch_url`, remain live.
- `record`: wipe and re-record the scenario's cassette live, ignoring the shared
  baseline. This mode exists to produce a tape this arm recorded itself.
- `strict`: a miss returns an error result instead of going live. This is for
  CI determinism; a miss usually means the model's args drifted.
- `off`: fully live.

**Why model-keyed.** Cassette keys are `(tool, canonical args)` and the args are
model-generated, so one model's recordings match another's calls only by
coincidence. `replay` is also a *write* mode. A run therefore writes only into
`<model-key>/`, so one model's run can never rewrite another model's tape.

**The flat tree is a read-only baseline** of mixed, unrecorded provenance,
layered *underneath* the per-model tape (own store first, then baseline) unless
you pass `--no-shared-cassettes`. Two consequences follow:

- A per-model tape starts life as a **diff** over that baseline, not a
  standalone recording. Deleting or regenerating the flat tree, or renaming a
  scenario id, orphans those entries, and the next run dispatches them live and
  spends money. To bound that, `save()` promotes every baseline entry the run
  actually replayed into the arm's own tape, so a tape converges on
  self-sufficiency. Promoted entries are **marked** (`from_base` in the tape
  file) and stay marked. Without that, promotion would launder provenance: two
  arms that each promoted the same baseline recordings would both report
  `model` while replaying identical bytes, and the LOW CONFIDENCE guard below
  would stop firing precisely once the correlation became permanent.
- Each arm pays for its own misses instead of inheriting another arm's
  recordings, so the first run of a new model pays full price. That
  is the trade we chose: a correct measurement instead of a cheap wrong one.

`--dry-run` prints per-scenario tape provenance (`model` / `shared` / `none`)
before anything is spent. `summary.json` records `cassette_dir`,
`cassette_model_key` and a per-scenario `cassette_tapes` map, and the report
header carries the split, so a statement like "this run was 95% live" is
legible rather than inferred. `cassette_tapes` carries a fourth value the dry
run cannot know in advance: **`promoted`**, meaning an own tape whose replayed
results came out of the baseline (`evals/cassette.py:tape_provenance`, keyed on
entries actually served this run, since a promoted entry nothing called again
correlates nothing).

Replay returns the recorded result without invoking the handler. What it
measures is tool-*selection* behavior against a frozen tool surface, which is
exactly the layer harness changes touch. Repeated identical calls replay in
recording order (then repeat the last result), so loops stay deterministic.
`internet_search` recordings also carry the number of backend calls consumed
by the live handler. Replay reapplies that count and enforces the current
per-turn limit, so a cassette cannot give a model more searches than production.
Legacy search entries without this metadata are treated as misses and refreshed
live in `replay` mode rather than assigned a guessed cost.

Only the read-only source tools `discord_text_search`, `internet_search`, and
the Hindsight reads (`recall_user`, `reflect_user`, `lookup_memory_source`) are
recorded by the core allowlist in `evals/cassette.py`; plugins may add
network-backed read-only tools through the `eval_record` surface in
`app/tool_surfaces.py`. Every other tool dispatches live, because local handlers
may mutate context or files. In particular, `fetch_url` stays live so replay
cannot skip its workspace write and outgoing-attachment side effects. Since
unlisted means live, new tools are correct by default.

### Workspace files

A scenario that asks the model to inspect or edit an existing text file must
declare the file instead of relying on an empty workspace:

```yaml
workspace_files:
  notes.md: |
    teh release checklist
    - run the test suite
```

Before turn one, the harness writes each fixture through the production
`write_file` boundary with attachments disabled. Setup bypasses eval capture,
cassettes, and scripted faults because it is not a model action. The synthetic
eval identity places every repetition in a separate temporary workspace, which
is removed with the rest of the eval registry state after the run.

### Images (vision scenarios)

A turn is normally a plain string. The mapping form attaches images, on either
of the two rails production uses:

```yaml
turns:
  - "plain text turn"
  - text: "compare these two"
    images: [checker-yellow.png]        # attached to the user's own message
    reply_images: [bands-rgb.png]       # attached to the message being replied to
    reply_author: Ana
    reply_text: "here's the pattern I mentioned"
```

`images` becomes `input_parts`; `reply_images` becomes
`reply_context.image_parts`. Keeping them distinct is the whole point: they are
separate rails in the runtime, and a change that reads only one of them would
pass a test built on the other. Both name files in `evals/fixtures/images/`,
loaded as base64 data URLs with a sniffed media type, exactly as a Discord
attachment arrives.

Fixtures are generated rather than photographed, so each one has content a
grader can assert on in words, and the source is reviewable as code:

```bash
uv run python evals/fixtures/make_images.py
```

Each eval model spec declares `capabilities:` (mirroring `config/models.yaml`),
and that declaration is load-bearing. A provider class only advertises what its
*transport* carries: `openai_compat` may carry images even when one particular
hosted model rejects them. The check is therefore **fail-closed on the spec**:
an arm that does not declare `image_input` is treated as unable to see images,
no matter what its provider claims.

Because the image scenarios live in the default scenario tree, both runners
**skip** them for a blind arm and log what was dropped, rather than refusing
the run (refusing would block every ordinary run on a text-only model). A run
whose selected scenarios are *all* image scenarios still exits non-zero, since
there is nothing left to do. `evals.run` additionally requires **both** arms to
see images, because one model reading the picture while the other reads only
the caption is not a model comparison.

```bash
# Vision arm
uv run python -m evals.harness_run --model vision-example \
  --scenarios evals/scenarios/vision --repeat 3
```

Note that the harness runs **one** provider per arm, so it does not exercise
the `chat` → `chat_images` role handoff (that lives in `ProviderManager`, which
evals bypass). What these scenarios cover is everything downstream of routing:
that image bytes reach the model, on both rails, in the documented order.

### Faults (error-recovery scenarios)

A scenario may inject failures at the same dispatch layer:

```yaml
faults:
  - { tool: discord_text_search, message: "Discord search unavailable", times: 1 }
```

The first `times` calls to that tool fail with the message, and the run then
grades whether the model recovers.

### Cost and per-tool token accounting

`summary.json` carries real per-bucket provider usage (`input`, `cached_read`,
`cache_write`, `output`); `report.md` shows the totals (tokens, completion
time, cost, recorded tool calls) without the per-bucket split. Both carry an
optional USD estimate, priced through the same `usage/pricing.py:estimate_cost`
that the bot's `/usage` command uses. Rates come from an optional `pricing:`
block on the model spec, with the same shape as `config/models.yaml`:

```yaml
  candidate-example:
    pricing: {input: 1.00, cached_read: 0.10, output: 2.00, cache_write: 0.0}
```

An arm with no `pricing:` reports **`unpriced`**, never `$0.00`. If any token
bucket used by a rep lacks a rate, that rep and the containing aggregate stay
unpriced rather than reporting a partial bill. If a bucket such as
`cache_write` is genuinely free, set it to `0.0` explicitly.

The report also breaks context cost down per tool:

| Column | Meaning |
| --- | --- |
| `result_tokens` | Size of the tool's results, once (~4 chars/token estimate) |
| `context_tokens` | `result_tokens` × the provider calls that re-sent them afterwards |
| `live_calls` | Calls that reached the real handler (the rest replayed, faulted, or missed in strict mode) |

`context_tokens` is the number worth optimizing. A tool result joins the
context and is re-sent on every later provider call in the same turn, so the
same result costs more when it lands early in a loop. Result sizes are a
heuristic rather than a provider-family tokenizer; per-scenario token usage, by
contrast, is the exact provider-reported figure.

### Mechanical score

`evals/mechanical.py` computes a 0–100 composite per rep. Penalties: missing
expected tool 25, unexpected tool 10, live tool error 5, unrecovered error 15,
repeated call (identical args after an identical success; retries after errors
are free) 5, failed `reply_must_match` regex 15, raw-JSON reply 15, over
`max_tool_calls` budget 10, expected attachment never queued
(`expect.attaches_file`) 20, and each turn that terminates anywhere other than
`completed` 25. Scripted fault errors cost nothing unless unrecovered. A rep
*passes* when every hard expectation held; a timeout, provider failure, or
`max_iterations` fallback is therefore non-passing even if a later turn looks
good. Recovered live tool errors and repeated successful calls lower the score
and are flagged in the report but do not fail on their own. `evals.compare`
diffs per-scenario score means between two runs and exits non-zero when the
overall mean regresses beyond `--epsilon` (default 2.0), so a driver loop can
gate on it.

### Completion timing

Timing is informational and never changes the mechanical score. Each rep
records two values: end-to-end wall time for the complete scenario (all turns,
model calls, memory recall, and tool execution), and summed provider-call
latency. The report shows wall/provider means per scenario plus both run
totals. The gap between them is local harness and tool time; cassette hits make
that gap smaller than a fully live production turn would, so only compare arms
that use equivalent cassette provenance.

The summary and report count **user turns** (scenario messages handled)
separately from **model turns** (provider calls, including ReAct iterations
after tools). Effective output throughput is normalized output tokens divided
by provider-call latency. It includes time-to-first-token and request overhead,
so it measures user-observed provider speed rather than a generation-only
streaming rate. Missing output usage or zero provider latency is reported as
`n/a`, never as a misleading zero. Turns and throughput are diagnostic only
and do not change the mechanical score.

Under the delta table the report also splits the failures, because a score
delta cannot tell "this model failed" apart from "nothing passes this
scenario", and two runs can report an identical overall pass rate while
disagreeing about *which* scenarios failed:

- **`Failed in both runs`**: pass rate 0.0 in both arms. It is labelled
  *harness-suspect* only when the two arms are **different models**; two runs
  of one model are just one model's result, and the line says so instead. The
  suspect line is further marked **LOW CONFIDENCE** when either run used
  `--repeat 1` or the two runs replayed the same cassette *recording* for a
  listed scenario, since a correlated observation is not two observations.
  "Same recording" means the same `cassette_model_key` with both sides served
  out of a tape file (`model` **or** `promoted`, in any combination; the same
  key is the same file), or baseline-derived provenance (`shared` **or**
  `promoted`) on both sides. Separate tape files are not evidence of separate
  recordings once `save()` has promoted the same baseline entries into each
  arm's own tape.

  A run dir predating the `cassette_tapes` key is caveated too, by run id.
  Absent provenance counts as unknown, and it is the case where correlation is
  *guaranteed*, since every run before that key replayed the one shared
  flat-tree tape. This is a caution guard, so failing to fire is the damaging
  direction.
- **`Flipped`**: pass rate exactly `1.0` in one run and exactly `0.0` in the
  other. This is the model-differentiating set. A partial rate (e.g. `1.0` →
  `0.33`) lands in neither split, so check the delta table for those.

The failure-split helpers read their keys with `.get`, and a scenario whose
summary carries neither `aggregate.pass_rate` nor `reps` is skipped instead of
being counted as a failure. The delta table, by contrast, requires
`aggregate.score_mean` and top-level `run_id`/`model`, and a summary missing
those raises instead of comparing.

## Model qualification

```bash
# See the run matrix (models, scenarios, live-call count) before spending anything
uv run python -m evals.run --candidate candidate-example --dry-run

# Full run (live tools, real network; run sparingly)
uv run python -m evals.run --candidate candidate-example

# Optional alternate rubric file
uv run python -m evals.run --candidate candidate-example --rubric path/to/rubric.yaml
```

`--models` (default `evals/models.yaml`), `--scenarios` (`evals/scenarios`),
and `--out` (`evals/runs/latest`) narrow or relocate the run.

Outputs land in `evals/runs/latest/report.md` and `raw.jsonl`. A few things to
know about how it runs. Each candidate and baseline row in `raw.jsonl` includes
its complete `eval_identity` metadata (run nonce, arm, scenario, repetition,
digest, synthetic user id, and context key), so the stored comparison retains
the isolation provenance used during execution.

- Candidate and baseline run the same scenarios (`evals/scenarios/**/*.yaml`,
  loaded recursively from the category subdirectories) through the live ReAct
  loop, and the turns mirror production wiring (real `bot_name`, the
  production-mandatory compactor). Tools dispatch live except for the standing
  safety stubs: the same eval registry write-stubs `teach`,
  `remember_user_memory`, and `block_user`, and Discord tools run against the
  stub gateway (see "Known partial coverage" below).
- `load_models` rejects an `openai_compat` spec with an empty `base_url`, so an
  unfilled template fails before any tokens are spent.
- A blind rubric judge (`evals/judge.py`) loads `evals/rubric.yaml` and scores
  both models on its anchored dimensions; `report.md` is the head-to-head.
- Scenario `faults:` are ignored here (`evals.run` never arms them), so a
  fault-bearing scenario runs as a plain tooling scenario.
- Nothing replays: every tool dispatches live against both arms. Use this
  runner sparingly, because configured network-backed tools and Hindsight reads
  are live. Token cost is not priced here; the qualification report counts
  tokens per arm and leaves USD estimates to the harness runner's `pricing:`
  block.

## Known partial coverage

- The Discord-bound tools (`get_channel_context`, `lookup_member`) use stub
  fixtures, so they are graded on selection and args rather than true
  end-to-end.
- `teach` and `remember_user_memory` are write-stubbed so a run never pollutes
  the community bank or accumulates per-user memories in the live Hindsight
  backend (otherwise repeated harness runs would recall their own prior runs).
  Plugins add their own production-writing tools to the same stub list by
  declaring the `eval_stub` surface.
- `block_user` is registered against an in-memory stub store, so safety
  scenarios can present (and grade misuse of) the real tool without blocking
  anyone.
- `lookup_memory_source` needs a persisted transcript, and seeded history is
  in-memory only.
- Cassette replay bypasses the live tier/activation gates in `dispatch`. The
  recording was made under the same scenario config, so gating already shaped
  what got recorded.
- Thread handoff registers against a null manager, so `move_to_thread` is fully
  graded and the three lifecycle tools are gradeable on selection only. The
  harness also passes `THREAD_HANDOFF_SUGGEST_AFTER_TOOL_CALLS` into the same
  ReAct advisory path as production, so a proactive-handoff scenario is prompted
  only after the configured amount of substantive work.
- The chat-side coding controls register against an in-memory stub, so
  `start_coding_task` grades the delegation decision without queueing a job.
  The coding agent's own inner loop (`build_coding_registry`) is a separate
  registry and is not exercised here.
- `browser` and `run_code` scenarios declare `requires_tools:` and sit out on a
  host without a Linux sandbox, listed under **Skipped** in `report.md`. This
  is not the same as the expected-tool check: an unregistered tool in
  `expect.should_use_tools` still aborts the run, because that is a real gap.
  `requires_tools:` is the declaration that this host was never expected to
  have it.
