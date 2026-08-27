# Image Generation Tool — Implementation Plan

Status: proposed. Delete this file once the work has landed and
`docs/image-generation.md` describes the shipped behavior.

## Goal

Replace Kimi's regex-inferred image generation with an explicit, model-invoked
`generate_image` tool. ChatGPT OAuth (the existing Codex token path) is the primary auth
mode; a plain API key is supported so the surface keeps working without OAuth. The backend
sits behind a seam so other image providers can be added later without touching the tool.

Decisions settled:

- Trust tier `REGULAR`; registered as a **core** tool, not searchable.
- v1 scope: generation **and** editing (reference images from the caller's workspace).
- Rollout: operator opt-in via `IMAGE_GEN_ENABLED`, default off.
- Model `gpt-image-2`.
- The OAuth path targets a first-party Codex surface billed to the ChatGPT plan. Accepted.
- Generated images are not screened by `moderation/`. Accepted; `REGULAR` is the control.
- The regex trigger is retired in the same change.

## Current state (audited, not assumed)

Kimi **already generates images over the Codex OAuth token**, via the Responses API's
built-in `image_generation` tool rather than the REST images endpoints:

```
agent/modalities.py:wants_image_output(text)   regex over the user's message
  -> agent/core.py:1252                        wants_output_image=...
  -> agent/core.py:1751                        adds ProviderCapability.IMAGE_OUTPUT
  -> providers/codex.py:93                     appends {"type": "image_generation", ...}
  -> providers/codex.py:228-244                image_generation_call -> GeneratedAsset
  -> providers/assets.py:write_generated_assets -> Discord attachment
```

`CODEX_IMAGE_QUALITY` / `CODEX_IMAGE_FORMAT` tune it. `providers/openrouter.py:59` has a
parallel path using `modalities: ["image", "text"]`, parsed by its own `_parse_images`
(`providers/openrouter.py:86-101`) rather than an `image_generation_call` handler. Documented
at `docs/providers-codex.md:85-91`.

Problems this plan fixes:

1. The trigger is a regex guess over user text; the model never decides. The code comment at
   `agent/core.py:1749` concedes the false-positive risk ("let's call it a draw").
2. No edit support, and no way to reference a workspace image.
3. Only works when the *chat* provider is codex or openrouter.
4. No tier gate, no per-turn cap, no per-guild config: quality/format are deployment-wide.

## Why direct REST calls, not the native built-in

The native `image_generation` tool is a flag on the *next* provider request, not something a
handler can invoke. A tool wrapping it would return "armed" and the image would appear as a
side effect of the following turn — a worse contract than the regex. It would also chain
image generation to whichever chat model is live. Calling `images/generations` directly keeps
the image backend independent of the chat provider, which matters because `config/models.yaml`
routes freely across providers.

Both approaches bill the same OAuth token against the ChatGPT plan.

## Audit of seams (verified signatures)

| Need | Actual seam |
| --- | --- |
| OAuth token + account id | `codex/auth.py:CodexAuthManager` — `is_available()`, `get_account_id()`, `async get_access_token()`. Reuse the **cached** instance from `providers/factory.py:_get_codex_auth_manager` (module-level cache keyed by resolved token path). `app/providers.py:297:codex_tokens_available` builds a throwaway instance for a boolean check only — do **not** copy that pattern: each manager owns its own `_refresh_lock`, so a second instance can race Codex chat refreshes. |
| Originator header value | `codex/transport.py:20:CODEX_ORIGINATOR = "codex_cli_rs"` — reuse, do not re-declare |
| Ephemeral output dir | `WorkspaceManager.generated_job_dir(context_key, job_id, owner_user_id=...)` |
| Queue an attachment | `tools/output_queue.py:enqueue_output_file(ctx, path, root, max_attachments=..., description=...)`, raises `AttachmentLimitError` |
| Reference image paths | `WorkspaceManager.resolve_user_file_path(workspace_key, user_path, must_exist=True)` — rejects absolute, `..`, symlink escapes |
| Per-workspace lock | `tools/workspace/common.py:workspace_activity(workspace_locks, ctx)` |
| Registration | `ToolRegistry.register(name, description, parameters, handler, min_tier, searchable, category, config_spec)` |
| Per-tool operator config | `tools/config_spec.py` `ToolConfigField` + `KIND_CHOICE`/`KIND_INT`; credential/URL/path field names are rejected by `validate_config_spec` |

Two corrections to earlier sketches of this work:

- `GeneratedAsset` is **not** the rail for a tool. It is scoped to assets parsed out of a
  provider response and drained in `agent/turn.py`. Tools queue attachments with
  `enqueue_output_file`, like `tools/visuals.py`.
- `MessageContext.edit_target_image` is unrelated — it carries the user's attached image on an
  edited Discord message, for moderation.

## Package layout

```
bot/image_gen/
  __init__.py
  types.py       ImageGenRequest, ImageEditRequest, ImageResult,
                 ImageGenError, ImageQuotaError
  backends.py    ImageBackend Protocol: name, available(),
                 generate(req) -> ImageResult, edit(req) -> ImageResult
  openai.py      OpenAIImageBackend — both auth modes in one class
  factory.py     build_image_backend(settings, auth_manager) -> ImageBackend | None
                 SUPPORTED_IMAGE_BACKENDS = {"openai"}
  service.py     concurrency semaphore, timeout, PNG verification, byte caps,
                 error normalization
bot/tools/image_gen.py   init_image_gen_tool(...) + handler
```

The handler only ever sees `ImageResult`. A future provider is a new module plus a factory
entry, mirroring `providers/factory.py:SUPPORTED_PROVIDER_NAMES`.

## Auth modes

| Mode | Base URL | Headers |
| --- | --- | --- |
| `oauth` | `https://chatgpt.com/backend-api/codex` | `Authorization: Bearer <get_access_token()>`, `ChatGPT-Account-ID: <get_account_id()>`, `originator` |
| `api_key` | `https://api.openai.com/v1` | `Authorization: Bearer <IMAGE_GEN_API_KEY>` |

`IMAGE_GEN_AUTH_MODE=auto` (default) prefers OAuth when `CodexAuthManager.is_available()`,
falls back to the API key, and leaves the tool unregistered when neither is present.

Endpoints `POST {base}/images/generations` and `POST {base}/images/edits`; response
`data[0].b64_json`. Edits send reference images as data URLs. Token refresh is delegated to
the existing manager; the token never reaches `config_spec`, tool arguments, or logs.

## Tool surface

- `generate_image`; `min_tier=TrustTier.REGULAR`; **core** (`searchable=False`);
  `category="Media"`.
- Parameters: `prompt` (required), `attachment_description` (required, Discord alt text),
  `reference_paths` (optional, <= configured max; presence selects edit mode).
- Guards in order: `ctx.context_key` present -> per-turn call cap -> attachment cap ->
  acquire `workspace_activity` lock -> re-check both caps inside the lock (mutable per-turn
  state; `tools/visuals.py` does the same).
- Output: PNG written into the job dir, verified, queued via `enqueue_output_file`, JSON
  summary returned to the model (filename, bytes, dimensions, `attached_to_reply: true`).
  Image bytes never enter the transcript.
- Cleanup: `keep_job` flag plus
  `asyncio.shield(asyncio.to_thread(shutil.rmtree, job_dir, True))` on failure.

### Reference-image safety

`resolve_user_file_path` gives containment only — it says nothing about size or content. The
edit path must additionally:

- Accept only PNG/JPEG/WebP, verified by sniffing magic bytes, not by extension.
- Enforce a per-file byte cap and an aggregate cap across all reference images.
- Read and base64-encode off the event loop (`asyncio.to_thread`); ruff's flake8-async rules
  forbid blocking I/O in `async def`, and five workspace files are a real memory amplifier.
- Ship tests for an invalid type, an oversized single file, and an exceeded aggregate cap.

## Retiring the regex

- Delete `bot/agent/modalities.py` and `bot/tests/test_modalities.py`.
- Remove the `wants_image_output` import (`agent/core.py:25`), the
  `wants_output_image=` argument (`agent/core.py:1252`), and the parameter plus its branch
  (`agent/core.py:1741`, `1751`). `wants_image_output` has exactly one production caller.
- Also update the regex-dependent tests: `tests/test_provider_capabilities.py:33-75` and
  `tests/test_core_smoke.py:2190-2216`.
- **Preserve** `codex/transport.py:22:WEBSOCKET_REPLAY_STRIP_KEYS`, which strips
  `image_generation_call.result` on replay. Historical replay runs through
  `providers/codex.py:119-127` (`raw_provider_data` -> `_replay_item`) and depends on this
  sanitizer regardless of what the tool does.
- **Decide explicitly** what happens to `ProviderCapability.IMAGE_OUTPUT`, the request-side
  branch at `providers/codex.py:93-99`, `_parse_generated_assets`
  (`providers/codex.py:228-244`), OpenRouter's `_parse_images`
  (`providers/openrouter.py:86-101`), and `CODEX_IMAGE_QUALITY` / `CODEX_IMAGE_FORMAT`.
  Once the regex is gone nothing requests the capability, so these become unreachable for new
  turns. An earlier draft of this plan justified keeping them on the grounds that replay needs
  them; **that reasoning was wrong** — replay uses `raw_provider_data` and the transport
  sanitizer, not `_parse_generated_assets`. Note also that OpenRouter never parsed
  `image_generation_call` at all; it reads `message.images`. Either delete the dead paths or
  record a real remaining caller. Leaving them undecided contradicts CLAUDE.md's rule against
  stale compatibility paths.
- Update `docs/providers-codex.md:85-91` and `docs/providers.md:659-663` (and the note at
  `docs/providers.md:302`) so they describe the tool as the trigger.

## Config split

`config/settings.py` **and** `.env.example` (`tests/test_env_example.py` enforces parity):

- `IMAGE_GEN_ENABLED: bool = False`
- `IMAGE_GEN_BACKEND: str = "openai"`
- `IMAGE_GEN_AUTH_MODE: str = "auto"` (`auto` | `oauth` | `api_key`)
- `IMAGE_GEN_API_KEY: SecretStr = SecretStr("")`
- `IMAGE_GEN_MAX_CONCURRENCY: int = 1`
- `IMAGE_GEN_TIMEOUT_SECONDS: int = 300`

`config_spec` -> `config/tools/image_gen.md` (safe per-call knobs only):

- `model` (`KIND_CHOICE`, default `gpt-image-2`)
- `size` (`KIND_CHOICE`: `auto`, `1024x1024`, `1024x1536`, `1536x1024`)
- `quality` (`KIND_CHOICE`: `auto`, `low`, `medium`, `high`)
- `background` (`KIND_CHOICE`: `auto`, `opaque`, `transparent`)
- `max_calls_per_turn` (`KIND_INT`, default 2, min 1, max 8)
- `max_reference_images` (`KIND_INT`, default 5, min 1, max 5)
- `max_attachments` (`KIND_INT`, default 5, min 1, max 10)

Every `KIND_INT` field declares explicit `minimum` and `maximum`. All seven names clear the
credential/endpoint/path suffix rejection in `tools/config_spec.py:153-188`.
`ToolRegistry.register` takes further parameters between those shown above, so call it with
keyword arguments only.

## Wiring checklist

1. `tools/registry.py`: add `image_gen_calls_this_turn: int = 0` to `MessageContext`,
   alongside `visual_renders_this_turn`.
2. `app/tools.py`: `_register_image_gen(...)` following `_register_video` — build the backend,
   log and return early when disabled or when no auth path exists. Inject the shared
   `CodexAuthManager` rather than constructing a new one.
3. `agent/activity.py`: `_TOOL_LABELS["generate_image"] = "Generating an image"`.
4. `tests/test_package_graph.py`: add `"image_gen"` to **both** the `tools` and the `app` edge
   sets (`app/tools.py` imports the package, creating `app -> image_gen`; `app` has no such
   edge today), plus a new `"image_gen": {...}` node. Declare **only** the targets actually
   imported — `test_declared_edges_still_exist` fails on a listed edge that does not exist, so
   do not pre-declare `utils` or `config` speculatively. `tools` genuinely has no `codex` edge
   today, and gains none: the token plumbing stays behind `image_gen`.
5. Startup validation: `app/providers.py:317-325,364-371` currently checks Codex tokens only
   when a Codex *chat* profile is reachable. Extend it so an OAuth-backed image tool is
   validated too, or the tool registers and fails on first use.
6. No dependency changes — `aiohttp` is already locked.

## Tests

- Backend: exact URL, headers, and JSON payload per auth mode with a stub auth manager and a
  fake HTTP session; assert `ChatGPT-Account-ID` present on OAuth, absent on API key.
- `auto` mode selection and fallback; tool unregistered when disabled or unauthenticated.
- Reference paths: traversal/absolute/symlink rejection; count cap.
- Attachment cap and per-turn call cap, including the inside-the-lock re-check.
- Quota/usage-limit response -> friendly message, no token or traceback leakage.
- PNG verification rejects a non-PNG or oversized body.
- Regression: a turn whose text matches the old regex no longer requests `IMAGE_OUTPUT`.
- Reference images: invalid type, oversized single file, exceeded aggregate cap.
- One forced-token-refresh retry after a 401, mirroring `codex/transport.py:328-350`.
- `tests/test_env_example.py` parity; `tests/test_config_sync.py` (**every** `Settings` field
  must appear in `docs/configuration.md` — `test_every_setting_is_in_configuration_doc`);
  `tests/test_docs_links.py` for the new doc.

Async tests need an explicit `@pytest.mark.asyncio`. Prefer `monkeypatch` and hand-written
`Fake*`/`Stub*` classes over `unittest.mock`.

## Docs

- New `docs/image-generation.md`: surface, auth modes, gating, config fields, limits, the
  OAuth billing note, and the moderation gap.
- Link from `docs/README.md` and `docs/architecture.md`; short subsection in `CLAUDE.md`.
- Update `docs/providers-codex.md` and `docs/providers.md` for the retired regex trigger.
- `.env.example` block for the new settings **and** a matching entry for every new setting in
  `docs/configuration.md` — enforced by `tests/test_config_sync.py`, not optional.
- Add `generate_image` to the tool catalog table in `docs/tools.md`, which documents the
  complete built-in tool set.

## Verification

From `bot/`:

```bash
uv --preview-features audit-command audit --locked
uv run ruff check .
uv run ruff format --check .
uv run mypy .
uv run python -m pytest -q
git diff --check
```

## Settled API contract (from `codex-rs/codex-api` serde types + `api_bridge.rs`)

Generation (`POST {base}/images/generations`) and edit (`POST {base}/images/edits`),
`None`-valued fields omitted:

```json
{"prompt": "...", "model": "gpt-image-2", "size": "auto"?,
 "quality": "auto|low|medium|high"?, "background": "auto|opaque|transparent"?,
 "images": [{"image_url": "data:image/png;base64,..."}]}
```

Success: `{"created": int, "data": [{"b64_json": "..."}], "background"?, "quality"?, "size"?}`
(plus ignored `output_format`/`usage`). Output is PNG.

Errors (mirror `codex/transport.py:328-350` and Codex CLI `api_bridge.rs:131-170`):

- `401` → OAuth: `refresh_tokens(force=True)` once, retry once; second 401 is fatal.
- `429` + `{"error": {"type": "usage_limit_reached", "resets_at": <unix seconds>, "plan_type"?}}`
  → `ImageQuotaError` (active limit id rides the `x-codex-active-limit` header, e.g.
  `image_gen`).
- `429` + `"usage_not_included"` → plan-limitation message.
- Other non-200 → concise `ImageGenError` with status; no tracebacks, no secrets.

OAuth headers exactly as `codex/transport.py:331-341`: `Authorization: Bearer`,
`originator: codex_cli_rs`, `ChatGPT-Account-Id`. An operator smoke test against the live
endpoint remains advisable but is no longer blocking: the shapes come from the shipping
client implementation.

## Open items

1. ~~Confirm live request/response shapes~~ Settled above from the Codex CLI source; an
   operator live smoke test is still worth one manual run after deploy.
2. Latency is 1-2 minutes per call, well under `REACT_TURN_TIMEOUT_SECONDS`; confirm the
   activity indicator keeps updating during the call.
3. Decide the fate of `CODEX_IMAGE_QUALITY` / `CODEX_IMAGE_FORMAT` (see "Retiring the regex").
   They feed the request-side branch at `providers/codex.py:93-99`, which becomes unreachable
   once nothing requests the capability — they do **not** affect replay. Removing them means
   deleting the fields from `Settings`, `.env.example`, `docs/configuration.md`,
   `docs/providers-codex.md`, `providers/factory.py`, and `config/model_config.py`.

## Execution order

Each phase ends green before the next begins.

1. **Settle the contract** (open item 1). Confirm live request/response shapes. Blocking.
2. **`bot/image_gen/` package + tests.** Pure logic and HTTP, no Discord, no registry.
   Fastest thing to get correct in isolation.
3. **`bot/tools/image_gen.py` + wiring.** Registry, `MessageContext` counter, `app/tools.py`,
   activity label, package-graph edges, startup validation.
4. **Retire the regex.** Deletions, `agent/core.py` edits, affected tests, and the explicit
   decision on the now-dead native paths.
5. **Docs.** New doc plus every file in the Docs checklist.
6. **Full CI sweep**, then delete this plan file in the same change that lands the work.
