# Evals

There are two offline runners that share one harness. `evals/harness.py` drives the production `run_conversation` with the real registry, the real compactor, and a stub Discord gateway. They answer different questions:

- **Model qualification** (`evals.run`) pits a candidate model against an operator-selected baseline and has a blind judge score both. This is the original go/no-go flow.
- **Harness eval** (`evals.harness_run`) runs one operator-selected model repeatedly and scores it with deterministic mechanical rules. This is the regression loop you reach for after changing the harness itself: prompts, tool descriptions, error text.

Live eval routing belongs in the ignored `evals/models.yaml`; the public `evals/models.example.yaml` contains placeholders only. Copy the example, then fill in the baseline, candidates, judge, endpoints, and environment-variable names for your deployment. Eval API keys resolve from the shell environment first, then from the ignored repo `.env` (`evals/models.py:resolve_api_key`). Run the commands below from `bot/` after creating the standard developer environment in [development.md](development.md).

## Contents

- [Quick reference](#quick-reference)
- [Harness eval](#harness-eval)
- [Cassettes (record/replay)](#cassettes-recordreplay)
- [Workspace files](#workspace-files)
- [Images (vision scenarios)](#images-vision-scenarios)
- [Faults (error-recovery scenarios)](#faults-error-recovery-scenarios)
- [Cost and per-tool token accounting](#cost-and-per-tool-token-accounting)
- [Mechanical score](#mechanical-score)
- [Completion timing](#completion-timing)
- [Model qualification](#model-qualification)
- [Known partial coverage](#known-partial-coverage)

## Quick reference

If you've never run an eval before, the path is short:

1. Copy `evals/models.example.yaml` to `evals/models.yaml` and fill in at least one model spec with the right provider and a real API key in `.env` or your shell.
2. Dry-run to see the plan:
   ```bash
   .venv/bin/python -m evals.harness_run --model <name> --repeat 3 --dry-run
   ```
3. Run for real:
   ```bash
   .venv/bin/python -m evals.harness_run --model <name> --repeat 3
   ```
4. Compare two runs to detect regressions:
   ```bash
   .venv/bin/python -m evals.compare \
     evals/runs/harness/<model-a>/<run-a>/summary.json \
     evals/runs/harness/<model-b>/<run-b>/summary.json
   ```

Everything else on this page covers how it works and what the output means.

## Harness eval

```bash
# See the plan first
cp evals/models.example.yaml evals/models.yaml
.venv/bin/python -m evals.harness_run --model candidate-example --repeat 3 --dry-run

# First run: hybrid cassette mode records live tool results per scenario
.venv/bin/python -m evals.harness_run --model candidate-example --repeat 3

# A small qualification slice before spending on the complete suite
.venv/bin/python -m evals.harness_run --model candidate-example --repeat 1 \
  --scenario run-code-arithmetic --scenario internet-search-official-docs

# Compare a variant working tree against a reference run
.venv/bin/python -m evals.compare \
  evals/runs/harness/<model-a>/<run-a>/summary.json \
  evals/runs/harness/<model-b>/<run-b>/summary.json
```

You can point the runner elsewhere with `--models` (default `evals/models.yaml`), `--scenarios` (`evals/scenarios`), `--cassettes` (`evals/cassettes`), `--captions` (`evals/captions`), and `--out` (`evals/runs/harness`). These override the catalog, scenario directory, tool tape directory, shared image-caption cache, and output root. Use repeatable `--scenario <id>` flags to select a qualification subset without copying scenario files into a temporary directory.

Experimental gateways can set `timeout_seconds` and `min_request_interval_seconds` on their model spec. The latter spaces request starts: `6.1` stays below a ten-requests-per-minute limit. Pacing waits count toward end-to-end wall time but not provider latency or output tokens per second, so reports distinguish queueing from inference speed. The eval wrapper enforces `timeout_seconds` as a total wall-clock deadline around each model request, on top of the provider transport's own timeout behavior.

### Run outputs

Each run writes an immutable `evals/runs/harness/<model-key>/<utc>-<git-sha>/` directory containing three files:

- **`summary.json`** is the stable schema that `evals.compare` reads. It records the registered-tool surface, roster label, provider type, credential-free endpoint origin, exact model ID, and vision mode, so the same model served by two gateways can't be mistaken for one arm.
- **`report.md`** is the human-readable summary with totals, per-scenario scores, and the failure-split table.
- **`transcripts.jsonl`** is one record per rep, useful when you're triaging a specific failure.

The run fails fast if a scenario expects a tool that the current `.env` didn't register.

### Run identity

Every live run gets an isolated temporary writable-state root. Workspace files, attachments, personal skills, the browser profile, and the eval database never use the deployment's corresponding directories, and the temporary root is removed only after the eval registry closes the browser worker and other composed runtime owners. The visible fixture author remains `webhead`, but the caller id is a 20-digit synthetic value above Discord's unsigned 64-bit snowflake range, derived from SHA-256 over a per-run nonce, model arm, scenario id, and repetition. Repetitions and qualification arms can't see one another's workspace files or accidentally reuse a real user's storage identity. `summary.json` and `transcripts.jsonl` record the nonce and derived identity inputs, so a stored result remains auditable.

The in-memory coding-control stub is keyed by the same caller id, so task numbering and captured control actions start from clean state for every arm and repetition. The conversation context key includes the full identity digest, keeping generated artifacts and headless-browser sessions in the same per-arm, per-repetition boundary.

### Git identity

`<git-sha>` is HEAD's short sha. When tracked *source* differs from it, the identity is suffixed **`-dirty-<diff-hash>`**, where `<diff-hash>` is the first 12 hexadecimal characters of SHA-256 over a configuration-independent binary diff from HEAD. Equal labels therefore mean equal tracked source bytes in the working tree.

Failure is cautious, not clean:

- No git at all gives **`nogit`**.
- A `rev-parse` that succeeds followed by a diff that doesn't (10s timeout or a permissions error) gives **`-unknown`**.

The `-unknown` marker exists because answering it with a bare sha would stamp a possibly-edited tree with a commit it may never have executed.

Also excluded from the diff is everything under the run's tape directory (`harness_run.run_data_paths`). The default tape tree is gitignored, so it never reaches the check at all; the pathspec is there for a run that points `--cassettes` at a tracked path. A `replay` run records misses and promotes baseline entries into that tree as part of doing its job, so without the exclusion it would change its own diff hash and two runs of identical code would report different trees.

### Other flags worth knowing

- `--repeat` defaults to 3. `--repeat 1` logs a warning. One sample per scenario is not a measurement: we watched a scenario swing 100 → 35 between identical reps purely from tool choice, which is why the default is 3.
- `--model` resolves a candidate name (or the baseline label) from the ignored `evals/models.yaml`. The tracked example has nothing to do with production.
- `--max-tokens` is the per-provider-call output cap and defaults to 65,536, matching the production ReAct budget. It's written into `summary.json` and the report header so two arms can't silently run under different output budgets. This is separate from the number of ReAct calls a scenario may make.
- A model spec may declare `max_output_tokens` when a hosted deployment enforces a lower ceiling. The harness clamps to that ceiling, logs it, and records both the requested and effective values rather than sending a run of requests the provider will reject.
- An OpenAI-compatible model spec may declare `request_id_header`. The provider then sends that header with a fresh UUID on every model call, supporting gateways that expose caller request IDs without hard-coding a vendor into the shared provider.

## Cassettes (record/replay)

Tool calls are recorded and replayed at the eval registry's dispatch chokepoint (`evals/cassette.py`), one JSON file per scenario, laid out as:

```
evals/cassettes/<scenario-id>.json              # shared baseline, read-only
evals/cassettes/<model-key>/<scenario-id>.json  # this arm's tape (the only file a run writes)
```

`<model-key>` is `ModelSpec.label` slugged (`cassette_model_key`). It comes from the label rather than `model` (which carries provider path segments) or the `--model` argument (which can be a second spelling of the same arm). Two arms whose labels slug to the same key abort the run instead of sharing a tape, because the key encodes neither provider nor reasoning effort.

The entire cassette tree is ignored private eval data, since it contains recorded tool arguments and results. If you need reproducibility across machines, back it up or transfer it through an access-controlled artifact store. `evals/cassettes/` is created by the first recording run; until then `--dry-run` reports `none` for every scenario.

### Cassette modes

The `--cassette` modes are:

- **`replay` (default, hybrid)**: replay recorded `(tool, args)` results, fall through live on a miss and record it. The first run records, and later runs replay recorded source calls deterministically without paying for them. Tools intentionally kept outside cassettes, such as `fetch_url`, remain live.
- **`record`**: wipe and re-record the scenario's cassette live, ignoring the shared baseline. This mode exists to produce a tape this arm recorded itself.
- **`strict`**: a miss returns an error result instead of going live. This is for CI determinism; a miss usually means the model's args drifted.
- **`off`**: fully live.

### Why model-keyed

Cassette keys are `(tool, canonical args)` and the args are model-generated, so one model's recordings match another's calls only by coincidence. `replay` is also a *write* mode. A run therefore writes only into `<model-key>/`, so one model's run can never rewrite another model's tape.

### Why a flat baseline tree

The flat tree is a read-only baseline of mixed, unrecorded provenance, layered *underneath* the per-model tape (own store first, then baseline) unless you pass `--no-shared-cassettes`. Two consequences follow:

- A per-model tape starts life as a **diff** over that baseline, not a standalone recording. Deleting or regenerating the flat tree, or renaming a scenario id, orphans those entries, and the next run dispatches them live and spends money. To bound that, `save()` promotes every baseline entry the run actually replayed into the arm's own tape, so a tape converges on self-sufficiency. Promoted entries are **marked** (`from_base` in the tape file) and stay marked. Without that, promotion would launder provenance: two arms that each promoted the same baseline recordings would both report `model` while replaying identical bytes, and the LOW CONFIDENCE guard below would stop firing precisely once the correlation became permanent.
- Each arm pays for its own misses instead of inheriting another arm's recordings, so the first run of a new model pays full price. This favors a correct measurement over a cheaper, misleading one.

`--dry-run` prints per-scenario tape provenance (`model` / `shared` / `none`) before anything is spent. `summary.json` records `cassette_dir`, `cassette_model_key`, and a per-scenario `cassette_tapes` map, and the report header carries the split, so a statement like "this run was 95% live" is legible rather than inferred. `cassette_tapes` carries a fourth value the dry run can't know in advance: **`promoted`**, meaning an own tape whose replayed results came out of the baseline (`evals/cassette.py:tape_provenance`, keyed on entries actually served this run, since a promoted entry nothing called again correlates nothing).

### Replay semantics

Replay returns the recorded result without invoking the handler. What it measures is tool-*selection* behavior against a frozen tool surface, which is exactly the layer harness changes touch. Repeated identical calls replay in recording order (then repeat the last result), so loops stay deterministic. `internet_search` recordings also carry the number of backend calls consumed by the live handler. Replay reapplies that count and enforces the current per-turn limit, so a cassette can't give a model more searches than production. Search entries without this metadata are treated as misses and refreshed live in `replay` mode rather than assigned a guessed cost.

### What's recorded

Only the read-only source tools `discord_text_search`, `internet_search`, and the Hindsight reads (`recall_user`, `reflect_user`) are recorded by the core allowlist in `evals/cassette.py`. Plugins may add network-backed read-only tools through the `eval_record` surface in `app/tool_surfaces.py`. Every other tool dispatches live, because local handlers may mutate context or files. In particular, `fetch_url` stays live so replay can't skip its workspace write and outgoing-attachment side effects. Since unlisted means live, new tools are correct by default.

## Workspace files

A scenario that asks the model to inspect or edit an existing text file must declare the file instead of relying on an empty workspace:

```yaml
workspace_files:
  notes.md: |
    teh release checklist
    - run the test suite
```

Before turn one, the harness writes each fixture through the production `write_file` boundary with attachments disabled. Setup bypasses eval capture, cassettes, and scripted faults because it's not a model action. The synthetic eval identity places every repetition in a separate temporary workspace, which is removed with the rest of the eval registry state after the run.

## Images (vision scenarios)

A turn is normally a plain string. The mapping form attaches images, on either of the two rails production uses:

```yaml
turns:
  - "plain text turn"
  - text: "compare these two"
    images: [checker-yellow.png]        # attached to the user's own message
    reply_images: [bands-rgb.png]       # attached to the message being replied to
    reply_author: Ana
    reply_text: "here's the pattern I mentioned"
```

`images` becomes `input_parts`; `reply_images` becomes `reply_context.image_parts`. Keeping them distinct is the whole point: they're separate rails in the runtime, and a change that reads only one of them would pass a test built on the other. Both name files in `evals/fixtures/images/`, loaded as base64 data URLs with a sniffed media type, exactly as a Discord attachment arrives.

Fixtures are generated rather than photographed, so each one has content a grader can assert on in words, and the source is reviewable as code:

```bash
.venv/bin/python evals/fixtures/make_images.py
```

### Capabilities

Each eval model spec declares `capabilities:` (mirroring `config/models.yaml`), and that declaration is load-bearing. A provider class only advertises what its *transport* carries: `openai_compat` may carry images even when one particular hosted model rejects them. The check is therefore **fail-closed on the spec**: an arm that doesn't declare `image_input` is treated as unable to see images, no matter what its provider claims.

### Caption mode

Set top-level `image_captioner` in `evals/models.yaml` to a model that declares `image_input`. Both runners then send each ordered image roster to that model once, cache the description under `evals/captions/`, and pass the same production-format caption to every evaluated model, including models with native vision. Current-message images remain ordered before replied-message images and the caption labels their sources. Reports and summaries identify the captioner and caption-assisted scenarios.

This mode deliberately measures how each chat model reasons over fixed visual evidence; it doesn't compare native vision quality. The cache key includes the caption prompt version, captioner model, source labels, and image-byte hashes, so later models see exactly the same caption. Delete the ignored cache (or bump the prompt version in code) to intentionally regenerate it.

### Choosing the path

Choose the image path explicitly with `--vision-mode caption` or `--vision-mode native`. Caption mode requires `image_captioner` and measures reasoning over identical evidence. Native mode sends the fixture images directly and runs them only for an arm declaring `image_input`. The default `auto` uses the captioner when configured and native vision otherwise. Reports state the resolved mode. Ineligible image scenarios are skipped loudly.

```bash
# Caption-assisted visual run (when image_captioner is configured)
.venv/bin/python -m evals.harness_run --model vision-example \
  --scenarios evals/scenarios/vision --vision-mode caption --repeat 3

# Measure the candidate's own image path instead
.venv/bin/python -m evals.harness_run --model vision-example \
  --scenarios evals/scenarios/vision --vision-mode native --repeat 3
```

The caption request uses the same marker, prompt, token ceiling, and image order as production image distillation. Caption calls are setup work and aren't included in candidate token, latency, turn, or cost metrics; the cached caption is the common fixture being evaluated.

## Faults (error-recovery scenarios)

A scenario may inject failures at the same dispatch layer:

```yaml
faults:
  - { tool: discord_text_search, message: "Discord search unavailable", times: 1 }
```

The first `times` calls to that tool fail with the message, and the run then grades whether the model recovers.

## Cost and per-tool token accounting

`summary.json` carries real per-bucket provider usage (`input`, `cached_read`, `cache_write`, `output`); `report.md` shows the totals (tokens, completion time, cost, recorded tool calls) without the per-bucket split. Both carry an optional USD estimate, priced through the same `usage/pricing.py:estimate_cost` that the bot's `/usage` command uses. Rates come from an optional `pricing:` block on the model spec, with the same shape as `config/models.yaml`:

```yaml
  candidate-example:
    pricing: {input: 1.00, cached_read: 0.10, output: 2.00, cache_write: 0.0}
```

An arm with no `pricing:` reports **`unpriced`**, never `$0.00`. If any token bucket used by a rep lacks a rate, that rep and the containing aggregate stay unpriced rather than reporting a partial bill. If a bucket such as `cache_write` is genuinely free, set it to `0.0` explicitly.

The report also breaks context cost down per tool:

| Column | Meaning |
| --- | --- |
| `result_tokens` | Size of the tool's results, once (~4 chars/token estimate) |
| `context_tokens` | `result_tokens` × the provider calls that re-sent them afterwards |
| `live_calls` | Calls that reached the real handler (the rest replayed, faulted, or missed in strict mode) |

`context_tokens` is the number worth optimizing. A tool result joins the context and is re-sent on every later provider call in the same turn, so the same result costs more when it lands early in a loop. Result sizes are a heuristic rather than a provider-family tokenizer; per-scenario token usage, by contrast, is the exact provider-reported figure.

## Mechanical score

`evals/mechanical.py` computes a 0–100 composite per rep. Penalties:

| Issue | Penalty |
|---|---|
| Missing expected tool | 25 |
| Unexpected tool | 10 |
| Live tool error | 5 |
| Unrecovered error | 15 |
| Repeated call (identical args after an identical success; retries after errors are free) | 5 |
| Failed `reply_must_match` regex | 15 |
| Raw-JSON reply | 15 |
| Over `max_tool_calls` budget | 10 |
| Expected attachment never queued (`expect.attaches_file`) | 20 |
| Each turn that terminates anywhere other than `completed` | 25 |

Scripted fault errors cost nothing unless unrecovered. A rep *passes* when every hard expectation held; a timeout, provider failure, or `max_iterations` fallback is therefore non-passing even if a later turn looks good. Recovered live tool errors and repeated successful calls lower the score and are flagged in the report but don't fail on their own. `evals.compare` diffs per-scenario score means between two runs and exits non-zero when the overall mean regresses beyond `--epsilon` (default 2.0), so a driver loop can gate on it.

## Completion timing

Timing is informational and never changes the mechanical score. Each rep records two values: end-to-end wall time for the complete scenario (all turns, model calls, memory recall, and tool execution), and summed provider-call latency. The report shows wall/provider means per scenario plus both run totals. The gap between them is local harness and tool time; cassette hits make that gap smaller than a fully live production turn would, so only compare arms that use equivalent cassette provenance.

The summary and report count **user turns** (scenario messages handled) separately from **model turns** (provider calls, including ReAct iterations after tools). Effective output throughput is normalized output tokens divided by provider-call latency. It includes time-to-first-token and request overhead, so it measures user-observed provider speed rather than a generation-only streaming rate. Missing output usage or zero provider latency is reported as `n/a`, never as a misleading zero. Turns and throughput are diagnostic only and don't change the mechanical score.

### Failure split

Under the delta table the report also splits the failures, because a score delta can't tell "this model failed" apart from "nothing passes this scenario", and two runs can report an identical overall pass rate while disagreeing about *which* scenarios failed:

- **`Failed in both runs`**: pass rate 0.0 in both arms. It's labelled *harness-suspect* only when the two arms are **different models**; two runs of one model are just one model's result, and the line says so instead. The suspect line is further marked **LOW CONFIDENCE** when either run used `--repeat 1` or the two runs replayed the same cassette *recording* for a listed scenario, since a correlated observation is not two observations. "Same recording" means the same `cassette_model_key` with both sides served out of a tape file (`model` **or** `promoted`, in any combination; the same key is the same file), or baseline-derived provenance (`shared` **or** `promoted`) on both sides. Separate tape files aren't evidence of separate recordings once `save()` has promoted the same baseline entries into each arm's own tape.

  A run dir predating the `cassette_tapes` key is caveated too, by run id. Absent provenance counts as unknown, and it's the case where correlation is *guaranteed*, since every run before that key replayed the one shared flat-tree tape. This is a caution guard, so failing to fire is the damaging direction.

- **`Flipped`**: pass rate exactly `1.0` in one run and exactly `0.0` in the other. This is the model-differentiating set. A partial rate (e.g. `1.0` → `0.33`) lands in neither split, so check the delta table for those.

The failure-split helpers read their keys with `.get`, and a scenario whose summary carries neither `aggregate.pass_rate` nor `reps` is skipped instead of being counted as a failure. The delta table, by contrast, requires `aggregate.score_mean` and top-level `run_id`/`model`, and a summary missing those raises instead of comparing.

## Model qualification

```bash
# See the run matrix (models, scenarios, live-call count) before spending anything
.venv/bin/python -m evals.run --candidate candidate-example --dry-run

# Full run (live tools, real network; run sparingly)
.venv/bin/python -m evals.run --candidate candidate-example

# Optional alternate rubric file
.venv/bin/python -m evals.run --candidate candidate-example --rubric path/to/rubric.yaml
```

`--models` (default `evals/models.yaml`), `--scenarios` (`evals/scenarios`), and `--out` (`evals/runs/latest`) narrow or relocate the run.

Outputs land in `evals/runs/latest/report.md` and `raw.jsonl`. Each candidate and baseline row in `raw.jsonl` includes its complete `eval_identity` metadata (run nonce, arm, scenario, repetition, digest, synthetic user id, and context key), so the stored comparison retains the isolation provenance used during execution.

How this runner differs from the harness eval:

- Candidate and baseline run the same scenarios (`evals/scenarios/**/*.yaml`, loaded recursively from the category subdirectories) through the live ReAct loop, and the turns mirror production wiring (real `bot_name`, the production-mandatory compactor). Tools dispatch live except for the standing safety stubs: the same eval registry write-stubs `teach`, `remember_user_memory`, and `block_user`, and Discord tools run against the stub gateway (see [Known partial coverage](#known-partial-coverage)).
- `load_models` rejects an `openai_compat` spec with an empty `base_url`, so an unfilled template fails before any tokens are spent.
- A blind rubric judge (`evals/judge.py`) loads `evals/rubric.yaml` and scores both models on its anchored dimensions; `report.md` is the head-to-head.
- Scenario `faults:` are ignored here (`evals.run` never arms them), so a fault-bearing scenario runs as a plain tooling scenario.
- Nothing replays: every tool dispatches live against both arms. Use this runner sparingly, because configured network-backed tools and Hindsight reads are live. Token cost isn't priced here; the qualification report counts tokens per arm and leaves USD estimates to the harness runner's `pricing:` block.

## Known partial coverage

- The Discord-bound tools (`get_channel_context`, `lookup_member`) use stub fixtures, so they're graded on selection and args rather than true end-to-end.
- `teach` and `remember_user_memory` are write-stubbed so a run never pollutes the community bank or accumulates per-user memories in the live Hindsight backend. Harness repetitions therefore can't recall each other's writes. Plugins add their own production-writing tools to the same stub list by declaring the `eval_stub` surface.
- `block_user` is registered against an in-memory stub store, so safety scenarios can present (and grade misuse of) the real tool without blocking anyone.
- Cassette replay bypasses the live tier/activation gates in `dispatch`. The recording was made under the same scenario config, so gating already shaped what got recorded.
- Thread handoff registers against a null manager, so `move_to_thread` is fully graded and the three lifecycle tools are gradeable on selection only. The harness also passes `THREAD_HANDOFF_SUGGEST_AFTER_TOOL_CALLS` into the same ReAct advisory path as production, so a proactive-handoff scenario is prompted only after the configured amount of substantive work.
- The chat-side coding controls register against an in-memory stub, so `start_coding_task` grades the delegation decision without queueing a job. The coding agent's own inner loop (`build_coding_registry`) is a separate registry and isn't exercised here.
- `browser` and `run_code` scenarios declare `requires_tools:` and sit out on a host without a Linux sandbox, listed under **Skipped** in `report.md`. This isn't the same as the expected-tool check: an unregistered tool in `expect.should_use_tools` still aborts the run, because that's a real gap. `requires_tools:` is the declaration that this host was never expected to have it.