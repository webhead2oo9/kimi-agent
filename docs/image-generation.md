# Image generation

`generate_image` is a REGULAR-tier **core** tool for generating and editing
images through OpenAI. The model calls it deliberately, like any other tool;
nothing in the bot watches the user's wording for verbs such as "draw" or
"render" and turns them into an image request.

The image backend is independent of the chat provider. A Claude, GLM, Kimi, or
Codex chat turn can all call the same OpenAI-backed image tool. OpenAI is the
only backend shipped today; adding another means implementing the
`ImageBackend` protocol under `bot/image_gen/` and adding one factory entry.

## Availability and trust

Registration requires all of the following:

- `IMAGE_GEN_ENABLED=true`;
- the supported `IMAGE_GEN_BACKEND` value, `openai`;
- usable credentials for the selected auth mode; and
- a REGULAR or STAFF caller at dispatch.

If the credentials are missing, the bot logs that image generation is
unavailable and does not register the tool. The trust tier is checked again
when the tool is called, and a member below REGULAR is told `Unknown tool`, so
they cannot learn the tool exists.

The tool is core rather than searchable, so the model sees it from the first
turn and can answer "draw me..." straight away, without a `browse_tools` step.

## Authentication

`IMAGE_GEN_AUTH_MODE` accepts three values:

| Mode | Behavior |
|---|---|
| `auto` | Prefer the existing Codex OAuth token when present; otherwise use `IMAGE_GEN_API_KEY`; otherwise leave the tool absent. |
| `oauth` | Require the Codex OAuth token file. |
| `api_key` | Require `IMAGE_GEN_API_KEY`, an OpenAI platform key dedicated to this tool. |

OAuth reuses the same `CodexAuthManager` as the Codex chat provider
(`providers/factory.py:get_codex_auth_manager`), so image requests and Codex
chat share one token and one refresh lock instead of racing each other. Log in
with the same helper the Codex provider uses:

```bash
cd bot
.venv/bin/python scripts/codex_auth.py --token-file secrets/codex-auth.json
```

OAuth requests go to `https://chatgpt.com/backend-api/codex` with the bearer
token, the `ChatGPT-Account-Id` header, and a fixed `originator: codex_cli_rs`
header. A 401 triggers one token refresh and one retry. A stale token never
stops the bot from starting; the first image call simply returns a short
"please log in again" error. API-key requests go to
`https://api.openai.com/v1` with only the platform key.

Both modes use JSON for `images/generations`. OAuth edits use the Codex
backend's JSON data-URL contract. Public API-key edits use multipart form data
with repeated binary `image[]` parts, as required by OpenAI's Images API.

## Tool contract

`generate_image` takes:

- `prompt` — required generation/editing instructions, capped at 10,000
  characters;
- `attachment_description` — required Discord accessibility text, capped at
  1,000 characters; and
- `reference_paths` — optional workspace-relative PNG, JPEG, or WebP paths.
  Omitting it generates a new image; providing one to five paths edits those
  images.

Current-message Discord attachments are not implicit edit targets. The model
must first call `import_attachment`, then pass the resulting workspace path.
This keeps every model-supplied path behind
`WorkspaceManager.resolve_user_file_path`, which rejects absolute paths,
traversal, and symlink chains.

A successful call saves a PNG as `generated_images/image-<uuid>.png` in the
caller's workspace. It counts against the normal workspace quota, is queued
for the final Discord reply with its accessibility description, and stays
available for a later edit through `reference_paths`. The model gets back only
metadata and the relative path; image bytes never enter the conversation
transcript.

## Operator controls

Deployment-wide controls live in `.env` / `Settings`:

| Setting | Default | Purpose |
|---|---:|---|
| `IMAGE_GEN_ENABLED` | `false` | Opt-in registration gate. |
| `IMAGE_GEN_BACKEND` | `openai` | Backend factory name. |
| `IMAGE_GEN_AUTH_MODE` | `auto` | OAuth/API-key selection. |
| `IMAGE_GEN_API_KEY` | empty | Environment-only platform credential. |
| `IMAGE_GEN_MAX_CONCURRENCY` | `1` | Process-wide billable request cap (1–8). |
| `IMAGE_GEN_TIMEOUT_SECONDS` | `300` | Whole HTTP request timeout (30–900 seconds). |

Safe per-call behavior is read fresh each turn from
`config/tools/generate_image.md`:

```yaml
---
model: gpt-image-2
size: auto
quality: auto
background: auto
max_calls_per_turn: 2
max_reference_images: 5
max_attachments: 5
---
```

Allowed sizes are `auto`, `1024x1024`, `1024x1536`, and `1536x1024`.
Quality is `auto`, `low`, `medium`, or `high`; background is `auto`, `opaque`,
or `transparent`. Tool config never accepts credentials, endpoints, or paths.

## Resource and safety boundaries

- Model: fixed to `gpt-image-2`.
- Logical calls: default two per outer turn, configurable 1–8. Failed
  provider calls count once; invalid local references fail before the billable
  counter increments.
- Global concurrency: one by default, configurable 1–8. While a response is
  being decoded, a request can briefly hold several copies of a roughly 14 MiB
  JSON/base64 body, so running eight at once can use a few hundred MiB of
  memory.
- References: at most five; each is bounded to 10 MiB and the aggregate to
  25 MiB. Reads stop after the cap rather than loading an arbitrarily large
  workspace file.
- Formats: source bytes are sniffed for PNG, JPEG, or WebP signatures rather
  than trusted by filename.
- Output: base64 must decode, fit the 10 MiB Discord default file limit, and
  pass the same full-decode PNG validation as provider-native image assets
  (`utils/image_types.py:decoded_image_media_type`) before it is written. A
  bare PNG signature, a CRC-corrupt chunk, or a truncated file is rejected.
- Reading references, calling the provider, and writing the output all hold
  the same per-workspace lock as the file tools, so nothing can change or
  delete the workspace mid-call. A slow call can therefore hold off the sweeper
  or a privacy deletion for up to `IMAGE_GEN_TIMEOUT_SECONDS` (at most 900
  seconds).
- A `usage_limit_reached` 429 becomes a concise tool error with the provider's
  reset timestamp when present. Tokens, response headers, and tracebacks are
  never returned to Discord.

Moderation, when enabled, screens the user's text, the final reply, and the
attachment description. It does **not** look at the generated PNG itself, the
same as for any other workspace file the bot attaches. The controls on image
content are the REGULAR-tier gate and OpenAI's own policy.

## Provider-native image output

Some chat providers can return images directly in a response.
`ProviderCapability.IMAGE_OUTPUT`, `GeneratedAsset`, and the Codex/OpenRouter
response parsers support that as part of the provider contract, for code that
builds a `ProviderRequest` itself. Normal Discord turns never ask for it; they
create images only through `generate_image`.
