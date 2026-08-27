# Image generation

Kimi exposes `generate_image` as a REGULAR-tier **core** tool for OpenAI image
generation and editing. It is an explicit model tool, not a regex over the
user's wording: the normal Discord turn never infers provider image-output
capabilities from verbs such as "draw" or "render".

The tool is provider-neutral at its boundary. OpenAI is the only shipped image
backend; adding a future backend means implementing the `ImageBackend`
protocol under `bot/image_gen/` and adding one factory entry. The active chat
provider is irrelevant: a Claude, GLM, Kimi, or Codex chat turn can all call the
same OpenAI-backed image tool.

## Availability and trust

Registration requires all of the following:

- `IMAGE_GEN_ENABLED=true`;
- the supported `IMAGE_GEN_BACKEND` value, `openai`;
- usable credentials for the selected auth mode; and
- a REGULAR or STAFF caller at dispatch.

Missing credentials fail closed: the bot logs the unavailable capability and
leaves the tool unregistered. Dispatch independently rechecks the trust tier
and masks the tool as `Unknown tool` for members who cannot use it.

The tool is core rather than searchable, so an eligible model sees it on the
first turn and can satisfy a direct "draw me..." request without first loading
a catalog entry.

## Authentication

`IMAGE_GEN_AUTH_MODE` accepts three values:

| Mode | Behavior |
|---|---|
| `auto` | Prefer the existing Codex OAuth token when present; otherwise use `IMAGE_GEN_API_KEY`; otherwise leave the tool absent. |
| `oauth` | Require the Codex OAuth token file. |
| `api_key` | Require `IMAGE_GEN_API_KEY`, an OpenAI platform key dedicated to this tool. |

OAuth reuses the process-wide `CodexAuthManager` from
`providers/factory.py:get_codex_auth_manager`. Image requests and Codex chat
therefore share one token snapshot and refresh lock rather than racing two
manager instances. Configure the token with the same helper used by the Codex
provider:

```bash
cd bot
uv run python scripts/codex_auth.py --token-file secrets/codex-auth.json
```

OAuth requests target `https://chatgpt.com/backend-api/codex`, sending the
bearer token, `ChatGPT-Account-Id`, and the code-owned
`originator: codex_cli_rs` header. A 401 forces one guarded token refresh and
one retry. A stale image-only token never aborts bot startup; its first image
call returns a concise re-authentication error. API-key requests target
`https://api.openai.com/v1` and send only the platform bearer key.

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

Successful output is a PNG saved under a collision-resistant
`generated_images/image-<uuid>.png` path in the caller's per-guild workspace.
It counts against normal workspace quota, is automatically queued for the
final Discord reply with its accessibility description, and remains available
for a later edit through `reference_paths`. The tool returns only metadata and
the reusable relative path to the model; image bytes never enter the
conversation transcript.

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
- Global concurrency: one by default, configurable 1–8. Each request can
  transiently hold several copies of a bounded ~14 MiB JSON/base64 response;
  raising concurrency to eight can therefore consume a few hundred MiB.
- References: at most five; each is bounded to 10 MiB and the aggregate to
  25 MiB. Reads stop after the cap rather than loading an arbitrarily large
  workspace file.
- Formats: source bytes are sniffed for PNG, JPEG, or WebP signatures rather
  than trusted by filename.
- Output: base64 must decode, carry a PNG signature, and fit the 10 MiB Discord
  default file limit before it is written.
- Workspace reads, writes, and the complete provider call hold the same
  per-workspace activity lease as the file tools, preventing mutation races and
  sweeper/privacy deletion. A slow call can therefore delay maintenance for up
  to `IMAGE_GEN_TIMEOUT_SECONDS` (maximum 900 seconds).
- A `usage_limit_reached` 429 becomes a concise tool error with the provider's
  reset timestamp when present. Tokens, response headers, and tracebacks are
  never returned to Discord.

The ordinary moderation service screens the user's input text and the final
reply plus queued attachment description. Like other generic queued workspace
files, it does **not** inspect the generated PNG bytes. REGULAR-tier access and
OpenAI's provider-side policy are the controls for image content.

## Provider-native image output

`ProviderCapability.IMAGE_OUTPUT`, `GeneratedAsset`, and the Codex/OpenRouter
response parsers are part of the provider contract. Direct `ProviderRequest`
callers may explicitly request native image output, and provider-emitted assets
use the generated-asset moderation and delivery rail. Normal Discord turns use
`generate_image` as their explicit image-creation surface.
