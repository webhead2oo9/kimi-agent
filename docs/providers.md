# Providers

Kimi talks to several LLM backends through one internal interface. Nothing
above `providers/` knows whether a reply came from Anthropic, an
OpenAI-compatible gateway, or a Codex WebSocket; the agent builds a
`ProviderRequest`, gets a `ProviderResponse` back, and the differences stay
below that line.

This page covers the parts that are the same everywhere: how a turn picks a
backend, what you can declare in `config/models.yaml`, how failover and image
routing behave, and what happens when the file is wrong. Each backend that
needs real setup has its own page:

- [Z.AI GLM Coding Plan](providers-zai.md)
- [xAI Grok](providers-grok.md)
- [Claude subscription via ccflare](providers-ccflare.md)
- [Codex](providers-codex.md)
- [OpenAI and OpenRouter](providers-openai.md)

## How a request finds its backend

There are three layers, and it's worth holding them apart in your head because
they fail in different ways.

**Roles** are the jobs the bot needs done: chatting, compacting a long
conversation, compiling a persona, or running an optional durable coding task.
Code never names a model directly. It asks for a role, and
`config/models.yaml` decides what serves it.

**Model entries** are named routing targets. An entry says which upstream model
id to send, how big its context window is, what it can do, and what it costs. A
role points at a model entry.

**Provider profiles** are endpoints and credentials. A profile says which
transport to speak, what URL to call, which environment variable holds the key,
and any per-gateway knobs. A model entry points at a profile.

So a chat turn resolves like this:

```
roles.chat  ->  models.primary-chat  ->  providers.primary  ->  openai_compat
   (job)          (what to send)          (where to send it)     (how to speak)
```

Splitting profiles from model entries is what lets one credential serve many
models at different settings, and lets the same model id be reachable through
two different gateways. It also means a model's identity and a gateway's
identity fail separately: a wrong model id is a 404 from the right endpoint, a
wrong profile is the right model at an endpoint that will not serve it.

If you want to know which transports exist, `providers/factory.py` is the
runtime source of truth. If you want to know which ones this deployment
actually uses, that is `config/models.yaml`.

## Provider types

Each profile declares one `type`. The supported values are the ones listed in
`SUPPORTED_PROVIDER_NAMES`; if a type isn't wired into the factory, it isn't
supported:

| `type` | Transport | Supports |
|---|---|---|
| `openai_compat` | OpenAI-compatible Chat Completions, streamed | text, tool calling, image input on vision models |
| `openai_responses` | OpenAI-compatible Responses API over a configurable `base_url` | text, image input and output, function tools, reasoning with encrypted replay |
| `anthropic` | Anthropic Messages API via the native SDK, api.anthropic.com only | text, image input, client-side tool use |
| `anthropic_compat` | Minimal Anthropic Messages over plain HTTP, for compatible gateways | text, image input, tool use, prompt caching |
| `openrouter` | OpenRouter Chat Completions | text, multimodal input, tool calling, provider routing, image output |
| `codex` | ChatGPT Codex backend over WebSocket Responses | text, image input, function tools, native image generation |

A gateway that speaks more than one of these can appear as more than one
profile sharing a single credential. Declare the transport each model actually
supports on that gateway; the live `models.yaml` owns those routes, because
which models a gateway exposes on which path is a property of that deployment.

### Subscription-backed routes

Subscription routes such as [Z.AI's GLM Coding Plan](providers-zai.md),
[Codex](providers-codex.md), and the [ccflare Claude
route](providers-ccflare.md) are different from ordinary metered APIs. Their
usage counts against a subscription rather than a per-token API balance.

They suit personal deployments and small trusted servers where the operator
holds the subscription. Keep three things in mind:

- **The quota is your personal quota**, shared with your own use of the same
  account. A busy bot and a busy terminal compete for it.
- **No spend is attributed.** These model entries carry no `pricing`, so their
  turns contribute nothing to `/usage`. That is accurate, since there is no
  per-token charge, but it means the ledger cannot show you what the bot is
  consuming.
- **Whether a multi-user bot fits the subscription's terms is your call to
  make.** Check the provider's terms before pointing a public instance at one.
  This is a scope question, not a security one.

None of that makes them bad choices; a personal instance is exactly what they
suit. It just means "point the bot at my Claude subscription" is a different
decision from "add another API key," and the metered routes are the
straightforward answer for an instance serving people you do not know.

### How `openai_compat` streams

`openai_compat` always streams (`stream=true` with
`stream_options.include_usage`) and reassembles the chunks into the same
`ProviderResponse` a non-streaming call would produce. Nothing is shown to
Discord incrementally, so this buys no visible typing effect.

What it buys is liveness you can observe and enforce. A non-streaming call is
opaque: a backend that has silently wedged looks exactly like one that is
thinking hard, and you find out at the turn deadline. Streaming turns that into
a signal. The log records time-to-first-chunk, prints a progress line every 15
seconds while a call is in flight (at WARNING once the stream has gone that long
without a chunk), and on failure or cancellation writes a post-mortem counting
chunks, reasoning characters, content characters, and tool-argument characters.

That signal is then wired to a timeout. A stream that goes silent for
`PROVIDER_STREAM_STALL_TIMEOUT_SECONDS` (default 90), including one that never
answers the initial request at all, is aborted with a `TimeoutError`. That
counts as a transient availability error, so it triggers failover. The rule is
about silence, not duration: a stream that keeps producing chunks is never
aborted by this watchdog no matter how long it runs, and is bounded only by the
whole-turn `REACT_TURN_TIMEOUT_SECONDS` ceiling. A slow answer is not a broken
one.

Two related behaviors are worth knowing about:

- SDK-internal retries are off (`max_retries=0`). Retrying is the failover
  chain's job, and two retry layers stacked on each other multiply the worst
  case instead of improving it.
- A backend that rejects the streaming request outright with a 400, before any
  chunk arrives, is retried once without streaming. That downgrade lasts only
  for that one request. Provider instances are shared across turns, and a
  rejection may be specific to one payload or one route, so making the
  downgrade sticky would quietly demote every later call on that provider. The
  next call streams again.

## The model catalog

Routing lives in `config/models.yaml`, which strictly means
`<CONFIG_DIR>/models.yaml`.

**That file is untracked instance state.** It describes which backends,
subscriptions, and proxies one particular deployment has, so it is gitignored
alongside `settings.md` and `.env`. The tracked artifact is
`config/models.example.yaml`, which uses non-routable placeholders. When you
start a new instance, copy it:

```bash
cp config/models.example.yaml config/models.yaml
```

If `models.yaml` is missing, startup fails and names both the expected
destination and the template path. It never quietly falls back to the template,
because booting onto backends the operator did not choose is worse than not
booting at all. The bot would come up looking healthy while talking to the
wrong vendor on someone else's key.

A pleasant side effect of the file being untracked is that swapping a model
locally to try another LLM never shows up as a dirty working tree waiting to be
committed.

### The shape of the file

```yaml
providers:
  primary:
    type: openai_compat
    base_url: https://gateway.example.invalid/v1
    api_key_env: MODEL_API_KEY
    models_endpoint: https://gateway.example.invalid/v1/models
    request_id_header: X-Client-Request-Id
    max_output_tokens: 32768
  anthropic-native:
    type: anthropic
    api_key_env: ANTHROPIC_API_KEY
    timeout_seconds: 2400
  oauth-backend:
    type: codex
    reasoning_effort: low

models:
  primary-chat:
    provider: primary
    model: provider/chat-model
    context_window: 200000
    capabilities: [text, tool_calling, image_input]
    pricing: { input: 1.00, output: 2.00, cached_read: 0.10 }
  compact:
    provider: primary
    model: provider/compact-model
    context_window: 200000
    capabilities: [text, tool_calling]
  oauth-chat:
    provider: oauth-backend
    model: oauth-model-id
    context_window: 200000
    capabilities: [text, tool_calling, image_input]
    reasoning_after_tools:
      medium: [discord_text_search, get_channel_context]
      high: [read_file, list_workspace, grep_workspace, glob_workspace,
             edit_file, multi_edit, write_file, move_file, delete_file,
             zip, extract_archive]

roles:
  chat: primary-chat
  chat_fallbacks: [oauth-chat]
  chat_images: primary-chat
  chat_images_fallbacks: [oauth-chat]
  compaction: compact
  compaction_fallbacks: []
  persona: compact
  persona_fallbacks: []
  # Optional background worker; requires text + tool_calling.
  coding: oauth-chat
  coding_fallbacks: []

# Candidate models offered to the bot owner by /models (maximum 120).
selectable_chat_models: [primary-chat, oauth-chat]

overrides:
  channels: {}
  guilds: {}
  users: {}
  commands: {}
```

Secrets never appear in this file. Profiles refer to them by environment
variable name, and the values themselves stay in `.env`.

### Provider profile fields

Unknown fields are rejected outright, so a stale knob left over from the `.env`
era cannot sit in the file looking effective while doing nothing.

| Field | Default | Applies to | Meaning |
|---|---|---|---|
| `type` | required | all | Which transport to speak. |
| `base_url` | `""` | all but `anthropic` | Endpoint to call. Required for `keyless` profiles. |
| `api_key_env` | `""` | all | Name of the env var holding the key. Must be one of the supported names below. |
| `keyless` | `false` | gateways | The endpoint injects its own upstream credentials, so no key is read. |
| `models_endpoint` | `""` | OpenAI-compatible | A `/v1/models` URL used to filter selectable candidates at startup. |
| `prompt_caching` | `true` | `anthropic_compat` | Send a rolling prompt-cache breakpoint. |
| `provider_routing` | `{}` | `openrouter` | Structured OpenRouter routing object, serialized into the request. |
| `app_name` | `BOT_NAME` | OpenAI-compatible | Optional provider-facing identity override. By default the configured bot name becomes the `User-Agent`; OpenRouter also receives it as its attribution title. |
| `app_url` | `""` | `openrouter` | Attribution header. |
| `service_tier` | `""` | OpenAI only | Service tier such as `flex`. Dropped on non-OpenAI endpoints. |
| `timeout_seconds` | `900` | `anthropic`, `anthropic_compat`, `openai_compat`, `openai_responses` | SDK transport timeout. Ignored by `openrouter`. |
| `max_output_tokens` | unset | all | Hard output-token ceiling for every model on this gateway. |
| `request_id_header` | `""` | OpenAI-compatible | Per-request tracing header name. |
| `reasoning_effort` | `""` | `codex`, `openai_responses`, `anthropic_compat`, `openai_compat` | Default effort for models routed through this profile. OpenAI-compatible profiles send it as `reasoning_effort`; DeepSeek targets additionally receive `thinking.type=enabled`. |

`max_output_tokens` lives on the profile rather than the model entry on
purpose. It expresses a limit the *gateway* imposes, so it applies to
everything routed through that gateway without lowering the global limit for
anyone else.

`service_tier` is an OpenAI-only kwarg. It is sent only when the profile's
`base_url` is `https://api.openai.com` or is unset (meaning the SDK default
endpoint). On any other gateway it is silently dropped, for both
`openai_compat` and `openai_responses`, because forwarding it would be a
guaranteed 400 from a gateway that has never heard of it.

Two fields exist for gateways that hold their own upstream credentials.
`keyless: true` declares that the endpoint injects them, so no API key is read
and the startup credential gate (`_has_active_llm_credentials`) is satisfied
without one. A keyless profile must set `base_url` and must not set
`api_key_env`; that combination would mean calling a vendor endpoint
unauthenticated, which is a configuration mistake worth failing on rather than
discovering as a 401 mid-conversation.

#### Supported `api_key_env` values

The set is closed (`SUPPORTED_API_KEY_ENVS`), so a typo shows up as a startup
error rather than as an empty key at request time:

`MODEL_API_KEY`, `ANTHROPIC_API_KEY`, `OPENCODE_GO_API_KEY`,
`RUNINFRA_GATEWAY_KEY`, `COMPACTION_API_KEY`, `GROK_API_KEY`,
`FIREWORKS_API_KEY`, `ZAI_API_KEY`, `KIMI_CODING_API_KEY`.

`MODEL_API_KEY` is the neutral one for any other OpenAI-compatible profile.
Codex profiles authenticate from `CODEX_TOKEN_FILE` and leave `api_key_env`
blank.

### Model entry fields

| Field | Default | Meaning |
|---|---|---|
| `provider` | required | Name of the profile in `providers:` that serves this model. |
| `model` | required | The upstream model id, sent verbatim. |
| `context_window` | `0` | Conservative token capacity. `0` disables the capacity warning for this model. |
| `capabilities` | `[]` | Declared abilities. Three are consulted: `image_input`, plus `text` and `tool_calling` on selectable models. |
| `pricing` | unset | Rates per million tokens: `input`, `output`, `cached_read`, `cache_write`. |
| `reasoning_after_tools` | `{}` | Effort to tool names that escalate the rest of the turn. |

Capabilities are declarations, not detections. The agent shapes each request
around what a model claims here, so an entry claiming `image_input` for a model
without vision produces a provider error rather than a graceful downgrade. Be
accurate, and when in doubt be conservative.

The list is not a closed vocabulary, and only three strings currently change
behavior: `image_input` drives image routing and the `chat_images` validation,
while `text` and `tool_calling` are required of anything in
`selectable_chat_models`. Notably, listing `image_output` on a model entry does
nothing, because image generation is gated on the *provider's* declared
`ProviderCapability`, not on this list. Declaring the rest is still worth doing
as documentation of intent, but don't expect an undeclared capability to be
enforced here.

If you omit `pricing`, turns on that model contribute no cost to `/usage`.
That is correct for subscription-covered backends and wrong for metered ones,
where it silently under-reports spend.

A `context_window` left at `0` suppresses the compaction capacity warning for
that model, so an unset window is silent rather than safe.

## Roles and routing

| Role | Required | Serves |
|---|---|---|
| `chat` | yes | Normal conversation turns. |
| `chat_images` | no | Image-bearing turns when `chat` lacks `image_input`. |
| `compaction` | yes | In-turn context compaction ([compaction.md](compaction.md)). |
| `persona` | no | Compiling user persona overrides ([persona.md](persona.md)). |
| `coding` | no | Durable background coding tasks ([coding-agent.md](coding-agent.md)); requires `text` and `tool_calling`. |

Every role may declare an ordered `<role>_fallbacks` list. Unknown role keys are
rejected, so a misspelled role is a startup failure rather than a silently
ignored line.

Reachable roles must have their referenced secret available, unless the provider
is Codex. "Reachable" is doing real work in that sentence, and
[what is checked at startup](#what-is-checked-at-startup) below spells out
exactly what it covers.

### Scope overrides

A chat turn resolves its model in this order, first match winning:

1. `overrides.commands`: the invoking command
2. `overrides.channels`: the channel
3. `overrides.users`: the author
4. `overrides.guilds`: the guild
5. `roles.chat`: the default

Provider instances are cached by model entry name, so several overrides that
land on the same entry share one instance.

### Choosing a chat model at runtime

After startup the bot owner can run `/models` and switch the global chat primary
without a restart. The choice applies to new turns in every conversation, old
and new, and survives restarts in SQLite. Choosing **Default** removes the
override and restores normal role and scope routing. In-flight turns keep the
provider they already resolved.

Only models listed in `selectable_chat_models` are offered, and each must
declare at least `text` and `tool_calling`; a chat model that cannot call
tools would be a dead end. The list allows at most 120 entries. Because Discord
caps one select menu at 25 options, `/models` renders them in pages of 24 plus
**Default** on the first page, up to five menus.

Editing provider or model declarations, or `selectable_chat_models` itself,
still requires a restart, because `models.yaml` is validated and loaded once at
process startup.

`roles.coding` is resolved independently for durable background coding tasks.
It is never inherited from `roles.chat`, so leaving it unset cleanly disables
the feature even if `CODING_TASKS_ENABLED=true`. The primary and every
`coding_fallbacks` entry must declare `text` and `tool_calling`. Coding tasks use
the ordinary failover rules but keep their own total and per-provider-call
deadlines.

### Catalog filtering

A profile may set `models_endpoint` to an OpenAI-compatible `/v1/models` URL. At
startup each unique endpoint-and-credential pair is fetched once, and that
profile's configured candidates are filtered down to the model ids actually
present in the response. If the fetch fails, those candidates are hidden rather
than offered, since their availability is then unknown and offering a model that
404s is worse than offering nothing.

Models still have to be declared statically. `/v1/models` reports ids, but not
which transport they need or what they can do, and those are exactly the facts
that routing depends on.

## Failover

Every resolved role uses the same provider-chain wrapper, including roles with
only one model. This gives fallback and circuit-breaker behavior one consistent
entry point. Chains are deduped first, and image turns filter out non-vision
fallbacks.

```yaml
roles:
  chat: primary-chat
  chat_fallbacks: [fallback-chat]
```

Connection errors, timeouts, server failures, and rate limits without a
`Retry-After` receive one retry before the chain advances. Rate limits with a
`Retry-After` and unambiguous account/model failures advance immediately. Once a fallback succeeds, later tool iterations in that logical
turn remain on it rather than retrying earlier links.

The `openai_compat` stall abort falls in this class. A stream silent for
`PROVIDER_STREAM_STALL_TIMEOUT_SECONDS` raises `TimeoutError`, so a wedged
backend gets one fresh attempt and then hands off, instead of consuming the
entire turn deadline. A stream still producing chunks is never aborted and may
legitimately run all the way to the `REACT_TURN_TIMEOUT_SECONDS` ceiling.

Persistent circuits skip unhealthy links across turns and restarts, with one
half-open recovery probe after cooldown. Deterministic request failures do not
open a circuit or fail over. See [Provider resilience](provider-resilience.md)
for scopes, cooldown precedence, configuration, and owner controls.

The wrapper's `capabilities` are the **intersection** across the chain. The
agent core shapes requests up front, so if it shaped one around an ability only
the primary has, a mid-turn failover would produce a request the fallback cannot
honor. Taking the intersection means the turn is built for whichever backend
ends up serving it.

Every response is stamped with the backend that actually served it
(`ProviderResponse.model`, preserving a more specific served model if a backend
already reported one). Downstream consumers can therefore attribute a reply to
the real model even after a mid-turn switch. Today those are the observability
stream's `tool_call.model` and per-turn `model`/`models` fields; see
[observability.md](observability.md). The usage ledger does the same: `model`
and `pricing_model` name the serving backend and each row is priced from it, so
cost during an outage lands on the fallback that served the traffic rather than
on the role's primary.

### What is checked at startup

Not every configured model is verified before boot, and the split is
intentional.

Chat, chat-image, selectable, and scope-override names (with their fallbacks)
are part of `reachable_model_names`, so their secrets are checked at startup. A
missing key there would break ordinary conversation, which is worth refusing to
boot over.

When `CODING_TASKS_ENABLED=true`, the configured `coding` primary and fallbacks
join that reachable set as well. A missing coding credential therefore stops
normal startup rather than accepting background work that cannot run.

`compaction_fallbacks` entries and the entire `persona` chain are not checked;
a missing credential there surfaces on first use instead. Persona sits outside
the gate because it is optional, and an optional feature must not be able to
abort boot.

The compaction capacity warning uses every reachable chat model's
`context_window` plus `COMPACTION_TRIGGER_TOKENS + REACT_MAX_TOKENS`. It covers
chat-role chains only, so a `compaction_fallbacks` entry with an undersized
context window produces no startup warning.

## Image routing

The `chat` role names one model, but some turns carry an image and that model
may be text-only. The optional `chat_images` role provides a second, image-only
chain used only for those turns:

```yaml
roles:
  chat: primary-text             # text-only turns (no image_input)
  chat_fallbacks: [fallback-text]
  chat_images: vision-chat       # turns that surface an image
  chat_images_fallbacks: []
```

`config/model_config.py:chat_model_name` routes a turn like this:

1. Resolve the scope-applicable chat model (command, channel, user, guild, then
   `roles.chat`), exactly as for any chat turn.
2. **If that model already has `image_input`, image routing is suppressed** and
   the turn uses it whether or not an image is present. A scope override that
   pins a vision model therefore keeps both text and image turns on that one
   model. In this case the turn's failover chain keeps only the image-capable
   entries of `chat_fallbacks`, because the chain exposes its backends'
   capability intersection: a single text-only fallback would strip
   `image_input` from the whole chain, and the turn would fail at the capability
   gate before ever reaching the perfectly capable primary.
3. Otherwise, if `chat_images` is set **and** the turn surfaces an image, the
   turn resolves to `chat_images` with its own `chat_images_fallbacks` chain as a
   separate `FailoverProvider`. Text-only turns keep using `chat` and
   `chat_fallbacks`.

Redirection is decided from the pre-routing model's capability, never from name
equality with `chat_images`. A scope override that happens to pin the same model
`chat_images` names is a suppressed redirect, not a redirect.

"Surfaces an image" begins with a cheap, provider-independent check made before
any provider is chosen (`agent/attachments.py:turn_has_image_input`). It reads
the same two surfaces `collect_turn_images` and `collect_reply_context` already
use: images on the triggering message, and images on a same-channel non-bot
reply target. Once the rooted SQLite transcript is loaded, stored image content
in that history can also promote the turn to the image chain before execution,
which is what makes a follow-up inside a managed thread route correctly.

Ambient images from recent channel history are **not** a routing trigger. They
are gathered only once `images_supported` is known, and that depends on the
model chosen here, so letting them influence the choice would be circular.

`chat_images` and every entry in `chat_images_fallbacks` must declare
`image_input`; the validator rejects a non-vision model in either place. These
models are part of `chat_model_names()`, so their secrets are checked at startup
and their context windows feed the capacity warning.

When `chat_images` is unset, behavior is exactly as if the feature did not
exist: the single `chat` model serves every turn, and an image turn against a
non-vision chat model fails at the capability gate in `agent/core.py`.

## Reasoning effort

The reasoning rail is provider-neutral. `agent/core.py` sets
`ProviderRequest.reasoning_effort`, and each provider maps a supported value
onto whatever its API calls that concept:

| Provider type | Sent as |
|---|---|
| `codex` | `reasoning.effort` |
| `openai_responses` | `reasoning.effort` |
| `anthropic_compat` | `output_config.effort` |
| `openai_compat` | `reasoning_effort`; DeepSeek targets also receive `thinking.type=enabled` |

A profile with no `reasoning_effort` sends no effort field at all unless the
provider supports and receives a request-level effort override.

### The `openai_compat` mapping

`reasoning_effort` is an OpenAI Chat Completions parameter. On a
reasoning-enabled turn, `openai_compat` sends the turn's value when present,
otherwise the profile's configured default. A profile with no default and no
request-level override sends no effort field.

DeepSeek targets need one additional compatibility knob. They are detected in
`providers/openai_chat.py:_is_deepseek_target` by `deepseek` appearing in the
`base_url`, or a model id starting with `deepseek-`. Those requests also carry
`extra_body: {"thinking": {"type": "enabled"}}`; when neither the turn nor the
profile supplies an effort, DeepSeek defaults to `high` as before.

The separate `openrouter` provider does not inherit this request field. It uses
OpenRouter's own request extensions rather than the generic OpenAI-compatible
effort mapping.

The Anthropic transports have a narrower ladder to keep in mind.

Anthropic's ladder (`ANTHROPIC_EFFORT_LEVELS`) is narrower than the agent's
internal `REASONING_EFFORT_ORDER`: only `low`, `medium`, `high`, `xhigh`, and
`max` are accepted. A value outside it is rejected at config load rather than
becoming a deterministic 400 in the middle of a conversation. An escalation that
would land outside the ladder is dropped instead of forwarded, for the same
reason.

`openai_responses` adds one rule of its own. A profile with no
`reasoning_effort`, on a turn that escalates none, sends no `reasoning`
parameter whatsoever, so compat gateways that reject unknown fields see exactly
the request shape they saw before reasoning existed. Once reasoning is in play
the provider also sends `include: ["reasoning.encrypted_content"]`. That is not
optional: production construction always sends `store=false`, so there is no
server-side reasoning state, and continuity across tool-call rounds exists only
because those encrypted items come back and are replayed in the next request's
input. A reasoning-disabled turn (compaction, finalizers) pins the cheapest
effort rather than the profile baseline, the same floor `codex` uses.

### Escalating after a tool call

A model entry may declare `reasoning_after_tools`, supported for `codex`,
`anthropic_compat`, and `openai_responses` models:

```yaml
reasoning_after_tools:
  medium: [discord_text_search, get_channel_context]
  high: [read_file, grep_workspace, edit_file]
```

When the turn calls a listed tool, the subsequent ReAct iterations run at least
at that effort for the rest of the turn. The idea is that a turn which has just
read a file is doing harder work than the one-line question that started it.

Escalation is **monotonic**: a later `medium` match cannot lower a turn already
raised to `high`. The next Discord message starts fresh at the profile's default
effort.

The native `anthropic` provider is separate from this rail. It streams
internally and returns the accumulated final message, so Discord output is still
sent only when the turn completes. On reasoning-enabled turns it enables
adaptive thinking at high effort for Claude Fable 5, Mythos 5 and Preview, Opus
4.6 through 4.8, and Sonnet 4.6. Thinking blocks are preserved in raw assistant
history so tool-use continuations can echo provider state back unchanged.

## Images

Discord image attachments are stored under `ATTACHMENT_STORE_DIR` and enter the
conversation as image content parts. `IMAGE_DETAIL` accepts `low`, `high`,
`original`, or `auto`, set in `.env` or the `<CONFIG_DIR>/settings.md` overlay;
an unknown value falls back to `auto`.

### When the chat model cannot see

If the selected chat model lacks `image_input` and `roles.chat_images` resolves
to a vision model, Kimi does not simply hand the turn to the vision model. It
first asks that model for a durable visual *transcription*: scene facts, OCR,
stated uncertainty, spatial relationships, and approximate boxes for salient and
OCR regions on a normalized 0-1000 `[left, top, right, bottom]` grid. The
transcription is then given to the selected text model, and the original images
are never sent to it.

The point of this is that the user's chosen model still writes the reply. The
vision model contributes eyes, not voice.

A message's own images are transcribed as it arrives, before the message is
written to the transcript, and the description is stored as a text part on that
same row. That is what makes it durable: a conversation keeps only its ten most
recent images and evicts the oldest, so without a stored description the visual
context of an older image would be lost outright. The description is labeled so
the model reads it as machine output rather than as something the user typed,
and it is never written to the message's plain `content` text.

An image that already carries a description is not described again, so the
per-turn pass only handles what the ingest pass did not: images on a replied-to
message, rows written before descriptions were stored, and messages whose ingest
transcription failed. When nothing is left to describe, the turn still drops the
image parts and runs on the text model rather than falling back to the vision
route.

Descriptions are cached per conversation, image set, vision model, and prompt
version. Transcript retention removes them through the conversation foreign key,
and per-user privacy deletion invalidates affected shared-conversation caches
before scrubbing that participant's messages. A failed or empty transcription is
never cached. Ingest transcription is best effort and bounded well inside the
turn deadline: if it fails the message is still stored, without a description,
and the per-turn pass handles those images instead.

Setting `MAX_TURN_IMAGES` to `0` strips stored descriptions along with the image
parts, so the kill switch stays complete.

### Capability gates

Image input stays capability-gated in `agent/core.py`: a provider without
`ProviderCapability.IMAGE_INPUT` returns a Discord-safe capability error.
Provider-native image output remains available to direct `ProviderRequest`
callers that explicitly request `ProviderCapability.IMAGE_OUTPUT`; normal
Discord turns never infer it from message text. Their image-creation surface is
the provider-independent [`generate_image` tool](image-generation.md). Native
provider assets are written under `WORKSPACE_DIR/generated/` and attached
through `discord_adapter.io.send_response`; tool-generated images instead live
under the caller's reusable workspace `generated_images/` path.

## When routing is misconfigured

`config/models.yaml` is validated once, at startup, and a bad file stops the
process rather than degrading into something that half works. If you hit one of
these messages, they come from `config/model_config.py`:

| Message | Cause |
|---|---|
| `config/models.yaml must contain a YAML mapping` | The file parsed to a list, a scalar, or nothing. |
| `unknown provider type '<x>'; expected one of: ...` | A `providers:` entry names a type outside `SUPPORTED_PROVIDER_NAMES`. |
| `unsupported api_key_env '<X>'; expected one of: ...` | The profile names an env var outside the closed set. Retired names such as `DEEP_RESEARCH_API_KEY` land here. |
| `keyless profiles must not set api_key_env` / `keyless profiles must set base_url` | A `keyless: true` profile contradicts itself, or has no endpoint to call. |
| `unsupported reasoning_effort '<x>'; expected one of: ...` | An effort outside the accepted ladder, or outside Anthropic's narrower one on an `anthropic_compat` profile. |
| `reasoning_effort is only supported for provider types: ...` | The field is set on a profile type that cannot carry it. |
| `models.<name>.provider references unknown provider '<x>'` | A model entry names a profile absent from `providers:`. |
| `models.<name>.reasoning_after_tools is only supported for provider types: ...` | The mapping is set on a model whose provider type cannot escalate. |
| `models.<name>.reasoning_after_tools has efforts [...] unsupported by provider type 'anthropic_compat'` | An escalation target outside Anthropic's ladder, which would be a mid-turn 400. |
| `unsupported reasoning_after_tools effort '<x>'` / `contains duplicate effort` / `must contain non-empty tool names` / `contains duplicate tool names` | A malformed escalation mapping. |
| `<path> references unknown model '<name>'` | A role, a fallback chain, or a scope override names a model absent from `models:`. |
| `<path> references model '<name>' which lacks the 'image_input' capability` | A non-vision model in `chat_images` or its fallbacks. |
| `selectable chat model '<name>' is missing capabilities ...` | A `/models` candidate without `text` and `tool_calling`. |
| `selectable_chat_models entries must not be blank` / `must be unique` / `supports at most 120 entries` | A malformed candidate list. |
| `context_window must be >= 0` / `pricing rates must be >= 0` | Negative numbers in a model entry. |

The credential gate runs after parsing, and exits rather than booting onto a
backend with no key. See [setup.md](setup.md).

Two other conditions are logged and survived rather than treated as fatal:

- `Could not refresh model catalog <endpoint> (<error>)`: the optional catalog
  probe failed. Routing is unaffected; those candidates are hidden.
- `Chat model '<name>' is not operator-selectable`: `/models` named a model
  outside `selectable_chat_models`. The invoker sees the error and the bot is
  fine.

## Adding a provider

If you need a transport that isn't in the table above, here is the shape of the
work:

1. Add a provider class under `providers/` implementing
   `LLMProvider.run_turn(ProviderRequest) -> ProviderResponse`.
2. Normalize provider-specific content, tool calls, usage, generated assets, and
   continuation data into the shared shapes in `providers/types.py`. Nothing
   provider-shaped may escape upward.
3. Declare accurate `ProviderCapability` values, and add capability tests.
4. Wire it into `providers/factory.py` and add its type to
   `SUPPORTED_PROVIDER_NAMES`.
5. Add only genuinely required secret or operational settings to
   `config/settings.py` and `.env.example`. Model and profile fields belong in
   `config/models.yaml`, not in the environment.
6. Update this page, add a backend guide beside it if the provider needs real
   setup, and add focused tests for request shape, response parsing, provider
   state, tool calls, and provider-specific errors.
