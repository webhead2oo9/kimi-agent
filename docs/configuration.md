# Configuration reference

> This page documents every `Settings` field the bot reads, the `.env.example`
> keys read elsewhere, and the model catalog in `config/models.yaml`. The model
> catalog is deployment-specific; create it by copying
> [`bot/config/models.example.yaml`](../bot/config/models.example.yaml).
> Typed settings are defined in
> [`bot/config/settings.py`](../bot/config/settings.py). Update this page and
> `.env.example` whenever those settings change. The defaults below are the
> **code defaults**; a deployment's `.env` may override them.

## Quick start

For a new operator, the minimum viable path is:

1. Copy `bot/.env.example` to `.env` and set at least the secrets and allowlists it flags (Discord token, provider keys, allowed channels/guilds).
2. Copy `bot/config/models.example.yaml` to `config/models.yaml` and fill in the `chat` and `compaction` roles with provider profiles that match your `.env` secrets.
3. Optionally drop a `<CONFIG_DIR>/settings.md` to keep supported scalar overrides in one place; changes take effect after restart.
4. Restart the bot. The startup probe reports missing files, invalid values, and gated features that didn't enable.

Most settings in this reference are optional or gated off by default. The first half of the page covers the load order and where values come from. The second half lists every setting grouped by feature.

## Contents

- [Quick start](#quick-start)
- [How configuration loading works](#how-configuration-loading-works)
  - [Operator overlay](#operator-overlay)
  - [Plugins](#plugins)
  - [Modules](#modules)
  - [Per-tool configuration](#per-tool-configuration)
  - [Model routing](#model-routing)
  - [Secrets](#secrets)
  - [Derived values](#derived-values)
  - [Validation at startup](#validation-at-startup)
  - [Adding a new setting](#adding-a-new-setting)
  - [Common patterns](#common-patterns)
- [Choosing a dotenv file](#choosing-a-dotenv-file)
- [Discord & access control](#discord--access-control)
- [Model routing and LLM execution](#model-routing-and-llm-execution)
  - [Per-model pricing (cost tracking)](#per-model-pricing-cost-tracking)
  - [Durable coding tasks](#durable-coding-tasks)
  - [Codex operational settings](#codex-operational-settings)
- [Context compaction](#context-compaction)
- [Content moderation (gated)](#content-moderation-gated)
- [Discord text search (gated)](#discord-text-search-gated)
- [Internet search (gated)](#internet-search-gated)
- [Wolfram|Alpha (gated)](#wolframalpha-gated)
- [Video understanding (gated)](#video-understanding-gated)
- [Image generation (gated)](#image-generation-gated)
- [Attachments & image input](#attachments--image-input)
- [Hindsight (memory backend)](#hindsight-memory-backend)
- [User memory](#user-memory)
- [Privacy consent gate (disabled by default)](#privacy-consent-gate-disabled-by-default)
- [Thread handoff (gated)](#thread-handoff-gated)
- [Instruction fragments (channel and thread scopes)](#instruction-fragments-channel-and-thread-scopes)
- [Instance layout](#instance-layout)
- [Storage](#storage)
- [Observability](#observability)
- [Secrets file](#secrets-file)
- [Script execution (skills)](#script-execution-skills)
- [Code execution](#code-execution)
- [Persistent browser](#persistent-browser)
- [Workspaces (per-user file sandbox)](#workspaces-per-user-file-sandbox)
- [Validators & startup behavior](#validators--startup-behavior)
- [External env keys outside `Settings`](#external-env-keys-outside-settings)
- [Primary consumers](#primary-consumers)
- [Deployment notes](#deployment-notes)

## How configuration loading works

Kimi loads configuration from multiple layers so operators can keep secrets and deployment-specific values out of the repository while still having safe, auditable overrides. The core `Settings` model comes from the environment (or a chosen `.env` file). An optional operator settings file can then layer on top of it. Per-tool config, guild and channel fragments, and the model routing file are read at different times and have different lifecycles.

The main `Settings` object is instantiated once as a module-level singleton. Everything else imports this single instance. Plugins and application modules may define their own separate settings models.

Values are read from OS environment variables first, then from the dotenv file named by `ENV_FILE` (default `.env`). Core, plugin, and module settings all use the same selector, so a development process can't accidentally mix files. Unknown environment keys are ignored (`extra = "ignore"`).

Environment variable names match the setting names in upper case (for example `react_max_tokens` maps to `REACT_MAX_TOKENS`). The mapping is case-insensitive.

### Operator overlay

Once the core `Settings` object exists, `build_app` applies an overlay from `<CONFIG_DIR>/settings.md`. This file can modify any plain `bool`, `int`, `float`, or `str` field except those that define deployment boundaries:

- No secrets.
- No paths, binaries, or files (`*_dir`, `*_path`, `*_bin`, `*_file`).
- No service URLs (`*_url`, `*_base`).
- No `database_*` settings.
- Neither `plugin_modules` nor `kimi_modules`.

The `*_ids` allowlists are written as YAML lists in the operator file rather than the comma-separated strings used in `.env`.

The operator file **takes precedence over the environment**. Any field not mentioned in the file keeps its environment or default value. An absent or empty file changes nothing.

The overlay is read once inside `build_app`, so edits only take effect on the next restart. A present file must be wholly valid. An unreadable file, a malformed file, or an unknown or out-of-range value raises `OperatorSettingsError` and aborts startup before any fields are applied. A half-applied overlay is never allowed.

### Plugins

An enabled plugin may declare a safe, operator-editable subset of its own settings. These overrides live in `<CONFIG_DIR>/plugins/<plugin_name>.md` and never share the core `settings.md` namespace. The plugin must classify every field as either explicitly exposed or environment-only. Leaving a field unclassified is an error, and the system rejects any attempt to expose secrets, endpoints, or paths.

Every plugin setting change requires a restart. Discovery validates and applies the complete overlay before the plugin registers. An absent file inherits the environment or default values. A present file is validated atomically. See [plugins.md](plugins.md) for the plugin entry point, loading, and file-overlay contract.

### Modules

Lifecycle-aware modules follow the same declaration rules, but their safe overrides live at `<CONFIG_DIR>/modules/<module_name>.md`. Module settings are required when the module is configured, so invalid environment or overlay data aborts startup. Module-owned variables are documented in the module package rather than in this core catalog. See [modules.md](modules.md).

### Per-tool configuration

A tool may declare its own typed knobs. Their operator values live in `<CONFIG_DIR>/tools/<tool_name>.md` and are read **fresh every turn** instead of at boot, so a tool config change does not require a restart. More on this below.

### Model routing

Provider profiles, model IDs, role assignments, context windows, and chat overrides live in `config/models.yaml`, not in `.env`.

### Secrets

Secret fields use `SecretStr` (for example `OPENCODE_GO_API_KEY`). Read them with `.get_secret_value()`. They never appear in `repr()` or logs. `validate_assignment=True` lets tests assign plain strings and still have them coerced to `SecretStr`.

### Derived values

Some settings expose `@property` accessors that parse a raw string into a richer type (comma-separated → `set`/`list`/`tuple`). These are documented inline next to their source fields.

### Validation at startup

A few fields validate eagerly so that a typo fails fast with a clear message rather than lazily in the middle of a request. `ALLOWED_CHANNEL_IDS` must be numeric, for example.

### Adding a new setting
1. Add the field (with a default + a short comment) to the right group in `config/settings.py`.
2. Add it to `.env.example` with a representative value/comment.
3. Document it in this file under the matching section.
4. If it's a list/set, add a parsing `@property` and document the derived accessor.
5. If an env var is **not** a `Settings` field, document it under
   "External env keys outside `Settings`" and name the consumer.
6. A plain scalar field joins `SETTINGS_SPEC` automatically. Add its validation
   metadata beside it in `config/operator_settings.py`: an `int`/`float` **must**
   declare a `_MINIMUMS` floor (a test enforces it). Add a `_CHOICES` entry only
   if the application already enforces the vocabulary.

If the setting belongs to a plugin, do **not** add it to the core catalog. Keep
it on the plugin's `BaseSettings` model, and add an explicit plugin-settings
declaration only when the value is safe to keep in an operator-data file. The
plugin opts in with a module-level `PLUGIN_SETTINGS = PluginSettingsDefinition(...)`
from `config/plugin_settings.py`, and every model field must be classified in
exactly one of the definition's `exposed` or `environment_only` sets. Core
derives the scalar kind and nullability from the Pydantic field; each exposed
`PluginSetting` adds a label and help text, a numeric floor, closed choices, or
multiline presentation where that makes sense. Credentials, URLs and endpoints,
paths, and compound values that could embed any of those deployment boundaries
stay environment-only. Plugin declarations fail closed, and every exposed field
requires a restart.

### Common patterns
- **Model roles.** `roles.chat` and `roles.compaction` in
  `config/models.yaml` choose the defaults; `roles.chat_images` and
  `roles.persona` are optional. `roles.coding` and its optional
  `coding_fallbacks` independently route the durable coding worker and never
  inherit from `roles.chat`. `selectable_chat_models` supplies the candidates
  for the owner-only `/models` menu, and profiles with `models_endpoint` filter
  those candidates against the live `/v1/models` response at startup. Selection
  is live and global, but catalog edits still need a restart.
- **Provider resilience.** Provider profiles may set `failure_adapter` (default
  `generic`) and positive `circuit_breaker.outage_cooldown_seconds` (default
  300), `quota_cooldown_seconds` (default 1800), and
  `rate_limit_cooldown_seconds` (default 60) values. Recognized rate-limit and
  availability responses use an exact `Retry-After` when their failure policy
  supports it; other classifications use their configured mapping. See
  [Provider resilience](provider-resilience.md).
- **Feature gating.** Several tools register only when the thing they depend on
  is configured (a key present, a file valid, and so on). When the dependency is
  missing the tool is simply absent and the bot still runs. Gated tools are
  marked **(gated)** below.
- **Trust tiers.** `*_min_tier` values are one of `member` < `regular` < `staff`.

### Per-tool config fragments (`<CONFIG_DIR>/tools/<tool_name>.md`)

Per-tool configuration is a separate surface from the core and plugin startup
settings. A tool declares a typed spec when it registers
(`registry.register(..., config_spec=…)` over
`tools/config_spec.py:ToolConfigField`), and your values for it live in one
frontmatter-only fragment per tool:

```markdown
---
max_results: 20
---
```

Use this surface for safe, per-call behavior that one tool owns. Keep the
shipped default and any absolute maximum beside the tool's registration: the
bound is reviewed with the code, which lets you tune behavior without a
convenience knob turning into a way around a safety limit. Credentials,
endpoints, enablement gates, access scope, and client transport stay
deployment settings.

A fragment is loaded only while its tool is registered. If the registration
goes away the file just sits dormant, and registering the tool again picks the
existing values back up.

`config/fragments/tool_config.py` reads every spec'd tool's fragment on
**every turn**, resolves it over that tool's declared defaults, and
`prepare_turn` stashes the result on `MessageContext.tool_configs` (keyed by
tool name) next to the `blocked_tools` denylist it mirrors. A handler reads
`ctx.tool_configs.get("<tool>") or {}` and never merges defaults itself. An
edit therefore applies on the **next message, with no restart**, which is the
same reason the denylist lives in a fragment rather than an env var.

Field kinds are `int`, `float`, `bool`, `text`, and `choice` (a closed value
set). Numeric fields can declare both a minimum and a maximum, and runtime
loading enforces both.

The tool-owned behavior that exists today:

| Tool | Field | Default | Range | Fragment |
|---|---|---|---|---|
| `discord_text_search` | `max_results` | `25` | 1–25 | `config/tools/discord_text_search.md` |
| `internet_search` | `strategy` | `blend` | `blend` or `failover` | `config/tools/internet_search.md` |
| `browser` | `max_code_chars` | `12000` | 1–12,000 | `config/tools/browser.md` |
| `browser` | `max_calls_per_turn` | `16` | 1–16 | `config/tools/browser.md` |
| `browser` | `max_output_chars` | `28000` | 128–28,000 | `config/tools/browser.md` |
| `browser` | `max_screenshots_per_turn` | `4` | 0–4 | `config/tools/browser.md` |
| `generate_image` | `model` | `gpt-image-2` | closed choice | `config/tools/generate_image.md` |
| `generate_image` | `size` | `auto` | `auto`, `1024x1024`, `1024x1536`, `1536x1024` | `config/tools/generate_image.md` |
| `generate_image` | `quality` | `auto` | `auto`, `low`, `medium`, `high` | `config/tools/generate_image.md` |
| `generate_image` | `background` | `auto` | `auto`, `opaque`, `transparent` | `config/tools/generate_image.md` |
| `generate_image` | `max_calls_per_turn` | `2` | 1–8 | `config/tools/generate_image.md` |
| `generate_image` | `max_reference_images` | `5` | 1–5 | `config/tools/generate_image.md` |
| `generate_image` | `max_attachments` | `5` | 1–10 | `config/tools/generate_image.md` |
| `video` | `model` | `gemini-3.7-flash` | closed choice | `config/tools/video.md` |
| `video` | `thinking_level` | `low` | `low`, `medium`, `high` | `config/tools/video.md` |
| `video` | `max_output_tokens` | `8192` | 1,024–32,768 | `config/tools/video.md` |
| `video` | `max_calls_per_turn` | `4` | 1–8 | `config/tools/video.md` |
| `video` | `max_session_interactions` | `20` | 2–50 | `config/tools/video.md` |
| `video` | `session_ttl_minutes` | `1440` | 5–1,440 | `config/tools/video.md` |

The path is relative to `bot/`, the default `CONFIG_DIR`, and the fragment is
active only while the tool is registered.

**Fail direction: open, the opposite of the denylist.**
`config/tools.md` raises rather than risk silently *granting* a tool. A tool
config fragment, by contrast, only tunes behavior you already opted into, and
the defaults are the shipped behavior, so nothing here ever raises:

| On disk | Result |
|---|---|
| No file (the normal case) | Every field at the tool's declared default. |
| Valid file | Its keys override; unmentioned fields stay at their defaults. |
| Unknown key | Warned in the log, ignored. |
| One uncoercible value | That field falls back to its default; the rest apply. |
| Unreadable / malformed file | That path's last-known-good values, or defaults if there are none. Logged as an error. |

Secrets and endpoints are **never** tool config. An API key stays an
environment-only `SecretStr` on `Settings`, and a fragment can only choose
between backends the deployment already holds credentials for. That is
*enforced*, not merely conventional: at registration, `validate_config_spec`
rejects any field name whose final underscore-separated word is a credential,
endpoint, or path word (`key`, `token`, `secret`, `password`, `auth`,
`credential(s)`, `url`, `uri`, `base`, `endpoint`, `host`, `dir`, `directory`,
`path`, `file`, `bin`), so a spec that would put an API token or a base URL
into a plaintext fragment fails at boot, where its author sees it. It is the
tool-spec mirror of the `_EXCLUDED_SUFFIXES` guard on the operator Settings
catalog described above.

---

## Choosing a dotenv file

| Env var | Default | Description |
|---|---|---|
| `ENV_FILE` | `.env` | Which dotenv file every core, plugin, and application-module `BaseSettings` class loads through `config/environment.py`. `ENV_FILE=.env.dev .venv/bin/python bot.py` boots a second instance with its own token, database, extension credentials, and paths against a test guild. A path that does not exist raises at import instead of silently loading nothing. See [development.md](development.md). |

This one is read from the process environment only. It can't be set *inside* a
dotenv file, since it is the thing that selects which file to read.

## Discord & access control

The bot token, role-based and user-based trust lists, channel and guild allowlists, optional Discord user-install surface, and gateway intent flags live here. The numeric ID lists are validated at startup so a typo fails fast.

| Env var | Type | Default | Description |
|---|---|---|---|
| `DISCORD_BOT_TOKEN` | secret | (none) | Bot token used to connect to Discord. Required to run. |
| `STAFF_ROLE_IDS` | csv(int) | "" | Discord role IDs mapped to the `STAFF` trust tier. |
| `REGULAR_ROLE_IDS` | csv(int) | "" | Discord role IDs mapped to the `REGULAR` tier. |
| `STAFF_USER_IDS` | csv | "" | Discord user IDs always treated as `STAFF`, regardless of roles. |
| `OWNER_USER_ID` | str | "" | The bot owner's Discord user id. Gates `owner_only` tools at dispatch; empty fails closed. Distinct from staff. |
| `ALLOWED_CHANNEL_IDS` | csv(int) | "" | If set, the bot only responds in these channel IDs. Empty = all allowed. Validated at startup (must be numeric). |
| `ALLOWED_GUILD_IDS` | csv(int) | "" | Optional boot-time approvals. A readable, non-symlinked, strictly validated `config/servers/<guild_id>.md` containing `bot_active: true` is the other activation source. A validated `bot_active: false` is a negative override: it preserves the setup while deactivating even an environment-approved guild. Missing, unreadable, symlinked, invalid, or keyless setup cannot activate by file. Inactive guilds stay connected, but responding turns fail closed. Validated at startup (must be numeric). |
| `BOT_NAME` | str | `Kimi` | Runtime/persona name, substituted into `config/persona.md` via `<bot_name>` and used for startup logs, text invocation, the `Teach <name>` context menu, and provider identity unless a model profile overrides `app_name`. It does not rename the visible Discord account; update the existing application/bot identity in the Developer Portal for a complete rename. Runtime instructions live in `config/prompt.md`. |
| `MESSAGE_CONTENT_INTENT` | bool | `true` | Whether to request the privileged Message Content intent at connect. `false` runs degraded: @mentions and pinged replies still work; the "hey <bot_name>" text trigger, thread auto-reply, and `discord_text_search` do not (keep `THREAD_HANDOFF_ENABLED` off while degraded). |
| `MEMBERS_INTENT` | bool | `false` | Whether to request the privileged Server Members intent at connect. Ordinary operation does not need it: roles come from the message author and member lookups use on-demand fetch/query. Optional modules may require member lifecycle events. Turning it on also requires enabling Server Members in the Discord Developer Portal, or the gateway rejects the identify. |
| `USER_APP_CHAT_ENABLED` | bool | `false` | Explicitly register the Discord User Install `/chat` and `/chat-reset` surface and promote `/privacy`, `/memory`, and `/stop` to both install types. Off means no personal chat commands are exposed. |
| `USER_APP_MEMBER_IDS` | csv(int) | `""` | Discord user IDs granted Member-tier `/chat` access, independent from guild roles. |
| `USER_APP_REGULAR_IDS` | csv(int) | `""` | Discord user IDs granted Regular-tier `/chat` access. Highest tier wins on overlap. |
| `USER_APP_STAFF_IDS` | csv(int) | `""` | Discord user IDs granted Staff-tier `/chat` access. `OWNER_USER_ID` is added automatically. |
| `USER_APP_CHAT_TIMEOUT_SECONDS` | float | `840.0` | Whole personal-chat turn deadline, validated from 1 through 840 seconds so delivery remains below Discord's 15-minute interaction-token lifetime. |
| `USER_APP_DM_ENABLED` | bool | `false` | Also answer ordinary direct messages, as a second entry point onto the same `userchat:<user_id>` conversation, workspace, and root lock as `/chat`. Requires `USER_APP_CHAT_ENABLED` (which owns the self-service `/privacy`, `/chat-reset`, `/memory`, and `/stop` commands) and uses the same `USER_APP_*` access lists; a DM from anyone not on them is ignored without a reply. Guild messages are unaffected. |

**Derived:** `staff_role_id_set`/`regular_role_id_set` (sets of numeric strings), `staff_ids` (set),
`allowed_channels`, and the environment-backed `allowed_guilds` (sets of ints). At runtime,
`allowed_guilds` is merged with the cached, validated Server setup decisions, and an explicit
file deactivation wins. The trust tier itself is resolved in `trust/resolver.py` from role IDs
plus the staff-ID allowlist. That resolved tier is treated as sensitive: the `lookup_member`
tool includes a member's `trust_tier` only when the requesting user is `STAFF`, because
otherwise it would reveal `STAFF_USER_IDS` allowlist membership (staff who hold no role) to
any member. Roles are public in Discord anyway and stay visible to everyone.

The user-app allowlists are a separate trust surface and require a restart when
changed. Enabling user-app chat without at least one listed ID or
`OWNER_USER_ID` fails startup. See [Discord user-app personal chat](user-app.md)
for installation, scope, prompt customization, visibility, and reset behavior.

The `STAFF_*`/`REGULAR_*` env settings above are **global**: they apply in every guild. If
you run the bot in several guilds, a guild can add its own staff and regular lists in the
frontmatter of `config/servers/<guild_id>.md` (`staff_user_ids`/`staff_role_ids`/
`regular_role_ids`; see `config/fragments/guild_config.py`). Per-guild lists are *additive*:
`trust/resolver.py` ORs them with the global allowlists, so a guild can grant local standing
but can never take it away from a globally trusted user. The same fragment's `pinned_tools:`
frontmatter pins searchable tools guild-wide, forming the base set that a channel's own
`pinned_tools` unions onto. Its mirror, `blocked_tools:`, is a guild-wide **denylist** (channel
`blocked_tools` union onto it) and the only way to *remove* a globally registered tool from a
guild or channel, since `guild_ids` only ever scopes a tool *to* guilds. Blocked names are
hidden from the model's tool list and the `browse_tools` catalog, and masked as "Unknown tool"
at dispatch, in that scope only. All of these are read fresh each turn, so an edit takes
effect on the next turn without a restart. The body of the file is still the
`<server_instructions>` slot.

The same fragment carries two tri-state thread switches, each of which a channel fragment
can override and each of which defaults to on: `thread_handoff:` (may the bot open threads
here at all?) and `thread_auto_respond:` (does a thread opened here answer every message, or
only when addressed?). Only literal YAML booleans count, so a typo falls back to the wider
scope instead of flipping the whole guild. See
[`docs/thread-handoff.md`](thread-handoff.md).

`learn_log_channel_id:` is the audit feed for staff-taught knowledge. Whenever a Staff
member teaches the bot something shared, whether a fact into community memory with `teach`
or a procedure into a skill with `skill_create`/`skill_edit`, the bot posts a card there
saying who taught it, what it stored, and linking back to the source message. This matters
most for the **Teach Kimi** message context menu (the name follows `BOT_NAME`), because its
confirmation is ephemeral: with no log channel configured, nobody but the acting staff
member ever sees that the bot learned something. It fails closed, so an absent or malformed
value means no learn logging at all (and, like the other id keys, a malformed value blocks
`bot_active` activation instead of being silently ignored). A logging failure never fails
the teaching itself.

`proposal_channel_id:` is the staff review channel for configuration proposals
created by application modules. When absent, the bot
uses the channel where the proposal was invoked. In either case core resolves
the channel and requires it to belong to this guild before posting. A malformed
numeric value invalidates `bot_active` activation rather than silently routing
a configuration card somewhere else.

A third thread key exists at **guild scope only**: `thread_targets:`, a list of numeric
channel ids the bot may open a thread in *other than the one it was asked in* ("take this
to #bot-spam"). Absent or empty means the capability is off in that guild; each community
opts in. Listing a channel is not a permission grant and overrides nothing: forums and
announcement channels are refused outright, `ALLOWED_CHANNEL_IDS` still binds, a channel
that turned handoff off (either with `thread_handoff: false` or by blocking `move_to_thread`)
stays off the list, and both the asker and the bot must be able to post there. Configure
the numeric ids directly in the guild fragment.

Two caveats about `blocked_tools`. First, it is **curation, not a privilege gate**: it only
ever subtracts visible tools, so `min_tier`/`owner_only`/`guild_ids` remain the real
security boundary. It applies to every responding turn. Second, deployment-wide, guild, and
channel denylists parse strictly: a malformed or unreadable policy at cold load fails closed, while
a missing, empty, body-only, or failed live reload keeps that exact path's last-known-good
denylist. Set `blocked_tools: []` explicitly to clear it. `pinned_tools` stays lenient because
losing a pin can't widen access.

---

## Model routing and LLM execution

`config/models.yaml` selects provider profiles, model IDs, role assignments, and
chat overrides. `providers/factory.py` is the source of truth for which
provider `type` values are supported.

The YAML owns the provider `type`, `base_url`, the OpenAI service and timeout
fields, the OpenRouter routing and attribution fields, model IDs,
`context_window`, and the role and override assignments. Secrets stay in `.env`
and the YAML refers to them by name through `api_key_env`.

`api_key_env` is a closed set, and a profile naming anything else is rejected
when the file is parsed at startup. The accepted names, each backed by a
`Settings` field, are `MODEL_API_KEY`, `OPENCODE_GO_API_KEY`,
`RUNINFRA_GATEWAY_KEY`, `ANTHROPIC_API_KEY`, `GROK_API_KEY`,
`FIREWORKS_API_KEY`, `ZAI_API_KEY`, `KIMI_CODING_API_KEY`, and
`COMPACTION_API_KEY`.

To support another key name, add its `Settings` field and an entry in
`config/model_config.py:_API_KEY_SETTINGS_FIELDS`; the allowlist derives from
that map. A profile whose endpoint holds its own credentials, such as a local
proxy or OAuth transport, sets `keyless: true` and does not name an env var.

### Per-model pricing (cost tracking)

Each `config/models.yaml` model entry may carry an optional `pricing` block with
USD-per-1M-token rates: `input`, `output`, `cached_read`, and `cache_write`.
Those rates feed the per-user `usage_ledger` and the `/usage` command. Leave
the block out and tokens are still recorded, just without a dollar estimate
(`est_cost_usd` is `NULL`). If a provider returns nonzero cached-token buckets,
include the cached rates too; otherwise that turn's cost is left unpriced
rather than being counted as free.

OpenRouter is a handy reference for the models it lists: its Models API
(`https://openrouter.ai/api/v1/models`) returns `pricing.prompt`,
`pricing.completion`, `pricing.input_cache_read`, and `pricing.input_cache_write`
as USD-per-token strings. To convert them to the USD-per-1M-token values used
here, multiply by 1,000,000 and map them to `input`, `output`, `cached_read`,
and `cache_write`. Those numbers are a reasonable seed when the OpenRouter
listing is effectively the official provider price for the route (a model with
a single MiniMax provider, for example). If OpenRouter omits a bucket such as
`input_cache_write`, leave that rate unset rather than inventing one; any
future nonzero tokens in that bucket will then stay explicitly unpriced. The
ledger always prices turns from the explicit `config/models.yaml` entries, so
rate changes stay reviewable.

Separately billed tool providers don't use model pricing. Their positive
per-backend charges go to `paid_usage_ledger`, and `/usage` breaks that spend
out into its own column while including it in the estimated cost for the window.

| Env var | Type | Default | Description |
|---|---|---|---|
| `MODEL_API_KEY` | secret | (none) | Neutral key for generic OpenAI-compatible profiles, including the placeholder profile in `models.example.yaml`. |
| `ANTHROPIC_API_KEY` | secret | (none) | Anthropic API key for native `anthropic` profiles. |
| `OPENCODE_GO_API_KEY` | secret | (none) | OpenCode Go subscription key for every `opencode.ai/zen/go/v1` profile (`openai_compat` /chat/completions for Kimi/GLM; `anthropic_compat` /messages for MiniMax). |
| `RUNINFRA_GATEWAY_KEY` | secret | (none) | RunInfra gateway key for OpenAI-compatible routes such as DeepSeek V4 Flash at `api.runinfra.ai`. |
| `GROK_API_KEY` | secret | (none) | xAI Grok key (`openai_compat` profile pointing at `https://api.x.ai/v1`). |
| `FIREWORKS_API_KEY` | secret | (none) | Fireworks AI key (`openai_compat` profile pointing at `https://api.fireworks.ai/inference/v1`). |
| `ZAI_API_KEY` | secret | (none) | Z.AI key for GLM Coding Plan profiles using the dedicated `https://api.z.ai/api/coding/paas/v4` Chat Completions endpoint; see [providers-zai.md](providers-zai.md). |
| `KIMI_CODING_API_KEY` | secret | (none) | Kimi Code membership coding-plan key (`anthropic_compat` profile pointing at `https://api.kimi.com/coding/v1`); separate product from the pay-as-you-go Kimi Open Platform. |
| `COMPACTION_API_KEY` | secret | (none) | Optional key for profiles assigned to `roles.compaction`. |
| `REACT_MAX_ITERATIONS` | int | `200` | Max tool-use iterations per turn before the loop stops. |
| `NEW_USER_ONBOARDING_TURNS` | int | `5` | Inject the `<onboarding>` system-prompt note while a user has fewer than this many stored messages with the bot (model is told they're new and may `block_user`, or use a server-provided reporting tool, on clear abuse). `0` disables. Mention-path turns only. |
| `REACT_MAX_TOKENS` | int | `65536` | Max output tokens per model call. |
| `REACT_TURN_TIMEOUT_SECONDS` | float | `3600.0` | Absolute wall-clock budget starting at response-turn entry. Preparation, attachment reads, input moderation, memory/persistence, provider calls, every tool, output moderation, and finalization all consume the same budget. Timed-out mutable child work releases the conversation root but keeps its privacy activity lease until it exits. |
| `PROVIDER_STREAM_STALL_TIMEOUT_SECONDS` | float | `90.0` | Abort a streamed provider request (`openai_compat`) when its stream produces no data (headers or chunks) for this long. The abort is a transient availability error, so a failover chain tries the next backend. |
| `LLM_MAX_CONCURRENCY` | int | `8` | Max concurrent in-flight LLM provider calls across all users/channels (a shared semaphore). |
| `TURN_MAX_CONCURRENCY` | positive int | `16` | Maximum admitted responding turns. It covers preparation, tools, delivery, and persistence; excess work is rejected immediately, never queued. |
| `TURN_MAX_CONCURRENCY_PER_USER` | positive int | `2` | Maximum admitted turns for one user. |
| `REACT_TEMPERATURE` | float\|blank | `1.0` | Sampling temperature for chat providers (`openai_compat`/`openrouter`). Blank -> omit the param (endpoint default). Other providers ignore it. |

### Durable coding tasks

The background coding service is an optional consumer of the same code-execution sandbox. `roles.coding` and its optional `coding_fallbacks` live in `config/models.yaml`; every entry must support `text` and `tool_calling`. See [coding-agent.md](coding-agent.md) for the full lifecycle, input handling, and recovery behavior.

| Env var | Type | Default | Description |
|---|---|---:|---|
| `CODING_TASKS_ENABLED` | bool | `false` | Request the durable coding service. It remains unavailable unless `roles.coding` exists and the code sandbox passes startup. |
| `CODING_TASK_MAX_CONCURRENCY` | positive int | `2` | Maximum concurrently active coding workers; one writer per workspace is enforced separately. |
| `CODING_TASK_MAX_QUEUED_PER_WORKSPACE` | positive int | `3` | Maximum queued tasks for one scoped workspace (per-user/guild or personal user-app). |
| `CODING_TASK_MAX_QUEUED_PER_USER` | positive int | `5` | Maximum queued tasks attributed to one member. |
| `CODING_TASK_MAX_SECONDS` | positive float | `7200` | Total wall-clock lifetime, including queue/recovery time. |
| `CODING_PROVIDER_CALL_TIMEOUT_SECONDS` | positive float | `600` | Ceiling for one coding-model request inside the total deadline. |
| `CODING_JOB_MAX_SECONDS` | positive float | `2700` | Managed command-job wall-clock ceiling. |
| `CODING_JOB_MAX_CPU_SECONDS` | positive int | `2400` | Per-process CPU rlimit for managed jobs. |
| `CODING_WORKER_STALL_SECONDS` | positive float | `120` | Worker liveness interval used to size durable heartbeat cadence. |
| `CODING_STATUS_MIN_INTERVAL_SECONDS` | positive float | `10` | Minimum interval between nonterminal Discord status edits. |
| `CODING_STOP_CLEANUP_WAIT_SECONDS` | positive float | `10` | Grace period `/stop` gives cancelled foreground work to finish teardown. |
| `CODING_TASK_MAX_ITERATIONS` | positive int | `80` | Maximum ReAct iterations for one coding task or recovery run. |

### Codex operational settings

Codex provider profiles live in `config/models.yaml`, while the operational
settings for the transport itself stay in `.env`.

| Env var | Type | Default | Description |
|---|---|---|---|
| `CODEX_TOKEN_FILE` | path | `secrets/codex-auth.json` | Codex auth file (WebSocket Responses transport). Codex auth is validated at startup when reachable. |
| `CODEX_MODEL` | str | `gpt-5.5` | Default model passed to the Codex transport when a caller does not supply one; bot chat routing uses the YAML model entry. |
| `CODEX_REASONING_EFFORT` | str | `high` | Codex reasoning effort. |
| `CODEX_IMAGE_QUALITY` | str | `auto` | Quality for provider-native Codex image output. |
| `CODEX_IMAGE_FORMAT` | str | `png` | Format for provider-native Codex image output. |
| `CODEX_WS_IDLE_TIMEOUT` | int | `3000` | Codex WebSocket idle timeout (s). |
| `CODEX_WS_READ_TIMEOUT` | float | `120.0` | Codex WebSocket read timeout (s). |
| `CODEX_VERBOSE` | bool | `false` | Verbose Codex transport logging. |

---

## Context compaction

Within-turn ReAct-loop compaction prevents a long, tool-heavy turn from overflowing the model window by summarizing older iterations into one progress note. It costs nothing on a normal turn. See [compaction.md](compaction.md) for tuning guidance, what survives summarization, and fallback behavior. The summarizer model is `roles.compaction` in `config/models.yaml`. If that
provider profile references `COMPACTION_API_KEY`, set the secret in `.env`;
otherwise it can share another supported secret env var, or use Codex auth.

| Env var | Type | Default | Description |
|---|---|---|---|
| `COMPACTION_TRIGGER_TOKENS` | int | `120000` | Projected input-token threshold that triggers compaction before the next request. Keep this plus `REACT_MAX_TOKENS` under the deployed model window. |
| `COMPACTION_KEEP_RECENT_ITERATIONS` | int | `3` | Minimum number of most recent assistant/tool iterations kept verbatim after compaction. Additional recent whole iterations are retained while `COMPACTION_KEEP_RECENT_TOKENS` allows; the remaining prefix is summarized or elided. |
| `COMPACTION_KEEP_RECENT_TOKENS` | int | `50000` | Token budget for the verbatim tail: whole recent iterations are kept until it is spent, never fewer than `COMPACTION_KEEP_RECENT_ITERATIONS`. Keep it well under `COMPACTION_TRIGGER_TOKENS`. |
| `COMPACTION_MAX_TOKENS` | int | `32768` | Max output tokens for the summarizer's progress note. |
| `COMPACTION_MAX_ITERATION_TOOL_OUTPUT_TOKENS` | int | `48000` | Max cumulative tool-output tokens appended within one ReAct iteration before later results are head/tail-truncated for the model-facing transcript. |
| `COMPACTION_API_KEY` | secret | (none) | Optional summarizer key for provider profiles that reference `COMPACTION_API_KEY`. |

The startup capacity warning, which is non-fatal, checks these values against
each reachable chat model entry's `context_window` from `config/models.yaml`.

---

## Content moderation (gated)

This is programmatic input and output moderation through the OpenAI
omni-moderation endpoint. `app/moderation.py` builds the service, and the
backend and policy live in `moderation/`. It runs only when both the flag and
the key are set. Use a dedicated OpenAI project key for it, not a chat provider
key.

Failure behavior is deliberately asymmetric. On a backend error or timeout,
input moderation **fails open**: availability wins, so the unscreened message
still reaches the provider and can drive tool side effects. Output moderation
**fails closed**: the bot never emits an unchecked reply, and during an outage
every reply is replaced with `MODERATION_ERROR_REFUSAL`. Output blocks on that
failure path are logged but don't emit the observability moderation event,
which fires only on a real category match.

Non-image files go through the shared 256 KiB UTF-8 extraction boundary in
`moderation/files.py`. Ambient Discord text attachments are read once and
screened, and the checked bytes are cached for `import_attachment`; binary,
non-UTF-8, and oversized inputs stay visible as metadata but their content is
withheld from tools. On output, moderation covers the assistant's reply, embed
text and owned embed image, and native model-generated images. Generic workspace
file bodies queued for Discord delivery are not moderation inputs, regardless
of file type. Optional Discord attachment descriptions are text sent by the bot,
so they are screened with the reply; this includes visual-rendering alt text.

Before delivery, each queued file is checked against the target guild's current
Discord upload limit (`guild.filesize_limit`), with a conservative 10 MiB
fallback when no guild limit is available. Files within the limit are still
sent; only oversized files are omitted. The first response chunk gets a short
plain-text notice naming each omission. That delivered notice is persisted in
the assistant transcript, so later turns know the file was not attached. A
durable coding task also checkpoints the limit and omission notice before send,
making crash recovery idempotent without retrying a known-oversized upload.

| Env var | Type | Default | Description |
|---|---|---|---|
| `MODERATION_ENABLED` | bool | `false` | Master switch. With the flag set but no key, moderation stays disabled. |
| `MODERATION_API_KEY` | secret | (none) | OpenAI key for the moderation endpoint. **Empty disables moderation.** |
| `MODERATION_BASE_URL` | str | "" | Override base URL for the moderation endpoint. Empty uses the OpenAI default. |
| `MODERATION_MODEL` | str | `omni-moderation-latest` | Moderation model name. |
| `MODERATION_TIMEOUT_SECONDS` | float | `5.0` | Per-request timeout for moderation calls. |
| `MODERATION_INPUT_IMAGES` | bool | `true` | Also send user-supplied images for input moderation. |
| `MODERATION_OUTPUT_IMAGES` | bool | `true` | Also send generated images for output moderation. |
| `MODERATION_OUTPUT_EXEMPT_TIER` | tier\|blank | "" | If set, users at or above this tier skip final response/activity output moderation. Input moderation still applies. |
| `MODERATION_INPUT_REFUSAL` | str | `That message didn't pass my content filter, so I didn't read it. Try rewording it.` | Reply sent when input moderation flags the member's own message. |
| `MODERATION_OUTPUT_REFUSAL` | str | `I wrote a reply, but it didn't pass my content filter, so I'm not posting it. Nothing's wrong on your end; try asking a different way.` | Reply sent when output moderation flags the bot's generated response. The member's message was fine. |
| `MODERATION_ERROR_REFUSAL` | str | `I can't run my content check right now, so I'm holding this one back. Try again in a minute.` | Reply sent when the check could not run at all, such as a moderation backend outage. Distinct from the two above so an outage does not read as a refusal to answer. |

---

## Discord text search (gated)

The hidden, searchable `discord_text_search` tool is enabled by default when Message Content
intent is enabled. `member` tier users can use it once `browse_tools` has activated it. Every
call builds a positive scope containing only channels both the requesting member and bot can
view and whose history they can read. Operator exclusions are removed before the Discord
search request is made, so excluded content is never fetched and filtered afterward.

If a tool call leaves out `channels`, it searches every eligible channel in the current
guild, including accessible active and archived threads. Passing one numeric channel ID or
a comma-separated string of IDs narrows the search to exactly those channels. Any excluded,
inaccessible, nonexistent, or cross-guild ID rejects the complete call without revealing
which check failed. A parent-channel exclusion also excludes all of its threads.

| Env var | Type | Default | Description |
|---|---|---|---|
| `DISCORD_TEXT_SEARCH_ENABLED` | bool | `true` | Whether to register the searchable tool. Message Content intent must also be enabled. |
| `DISCORD_SEARCH_EXCLUDED_CHANNELS` | csv(id) | "" | Channel IDs the tool must never search. Empty excludes nothing. Parent exclusions include child threads. |
| `DISCORD_SEARCH_TIMEOUT_SECONDS` | float | `30.0` | Timeout applied separately to channel-scope resolution and the Discord search request. |

The result limit is live tool behavior: edit `max_results` (default and
maximum `25`, minimum `1`) in `config/tools/discord_text_search.md`.

Channel instruction fragments are a separate configuration surface. The files under
`config/channels/`, `config/channel_threads/`, and `config/threads/` tell the model how to
behave in those scopes, but their presence neither grants nor denies message-history
access. Use Discord permissions for member visibility and
`DISCORD_SEARCH_EXCLUDED_CHANNELS` for channels that must stay outside the search surface.

`DISCORD_SEARCH_CHANNELS` is unsupported. A non-empty value aborts startup with
guidance instead of silently reversing the deployment's policy.

---

## Internet search (gated)

The member-tier core `internet_search` tool registers at startup when at least
one provider key is present. TinyFish is the highest-ranked provider because
its search and fetch endpoints are free at any wallet balance; Exa ranks next
and also does both search and page reads; Brave searches through its LLM
Context endpoint and can't read pages at all. With several keys set, a search
calls every configured provider and merges the results by default.

Think of the per-turn allowance in provider calls rather than tool calls. One
blended search across two providers spends two of the ten, so a turn gets five
of them before `internet_search` starts refusing, while a single-provider or
failover call spends one per provider it actually reaches. The bounded retry
inside one provider call doesn't count a second time, and neither does the
internal batching TinyFish does when reading more than ten pages at once.

| Env var | Type | Default | Description |
|---|---|---|---|
| `TINYFISH_API_KEY` | secret | "" | Enables TinyFish search and page reads. Free at any balance, but the key is still required. |
| `TINYFISH_SEARCH_URL` | URL | `https://api.search.tinyfish.ai` | TinyFish Search endpoint; normally leave unchanged. |
| `TINYFISH_FETCH_URL` | URL | `https://api.fetch.tinyfish.ai` | TinyFish Fetch endpoint; normally leave unchanged. |
| `EXA_API_KEY` | secret | "" | Enables Exa search and page reads. |
| `EXA_API_BASE` | URL | `https://api.exa.ai` | Exa API base; normally leave unchanged. |
| `BRAVE_API_KEY` | secret | "" | Enables Brave LLM Context search. |
| `BRAVE_CONTEXT_URL` | URL | `https://api.search.brave.com/res/v1/llm/context` | Brave LLM Context endpoint; normally leave unchanged. |
| `INTERNET_SEARCH_BACKEND_TIMEOUT_SECONDS` | float | `30.0` | Deadline for one provider request. |
| `INTERNET_SEARCH_TIMEOUT_SECONDS` | float | `45.0` | Whole tool-call deadline. |
| `INTERNET_SEARCH_MAX_RESULTS` | int | `10` | Hard maximum and default combined result count. |
| `INTERNET_SEARCH_MAX_BACKEND_CALLS_PER_TURN` | int | `10` | Provider calls the tool may make in one user turn. The bounded retry inside a call does not count again. |
| `INTERNET_SEARCH_MAX_OUTPUT_CHARS` | int | `24000` | Combined content budget, split evenly across the returned results; longer content is truncated. |
| `INTERNET_SEARCH_SAFESEARCH` | choice | `moderate` | Brave filtering: `off`, `moderate`, or `strict`. Brave is the only provider with a safesearch parameter. |
| `EXA_SEARCH_COST_USD` | nullable float | unset | Per-call fallback when Exa search does not report cost. |
| `EXA_CONTENTS_COST_USD` | nullable float | unset | Per-call fallback when Exa page reading does not report cost. |
| `BRAVE_SEARCH_COST_USD` | nullable float | unset | Per-call fallback when Brave does not report cost. |

TinyFish search and page reads are free and unmetered, so they have no fallback
price settings and write no paid-usage ledger rows.

`BRAVE_API_KEY` has to belong to a Brave plan that includes the LLM Context
endpoint. A key without it still authenticates, but every search fails with
HTTP 400 `OPTION_NOT_IN_PLAN`. That is a deterministic error, so it isn't
retried, and if Exa is also configured a blended search simply returns Exa's
results: the deployment looks like it's working while you pay for half a search
chain. Check a new Brave key against one real search before you trust it.

Routing is live tool config. Edit `strategy` in
`config/tools/internet_search.md`: `blend` (the default) calls every eligible
provider concurrently, while `failover` tries TinyFish, then Exa, then Brave,
and stops at the first successful response. Keys and endpoints stay
environment-only.

Ranking TinyFish first does not by itself avoid spending: under `blend` every
configured provider is called on every search, paid ones included. A deployment
that wants the free provider to actually displace the paid ones has to set
`strategy` to `failover` as well.

See [Internet search](internet-search.md) for request behavior, output shape,
deduplication, and cost accounting.

---

## Wolfram|Alpha (gated)

The searchable member-tier `wolfram_alpha` tool registers when its dedicated
AppID is present. It calls Wolfram|Alpha's LLM API with a bounded single-line
query and returns bounded text as untrusted context. The AppID is an
environment-only `SecretStr`; it is excluded from operator settings and tool
configuration.

| Env var | Type | Default | Description |
|---|---|---|---|
| `WOLFRAM_ALPHA_APP_ID` | secret | `""` | Dedicated Wolfram|Alpha AppID. A blank value leaves the tool unregistered. |
| `WOLFRAM_ALPHA_TIMEOUT_SECONDS` | float | `30.0` | Whole logical request deadline. Must be positive. |
| `WOLFRAM_ALPHA_MAX_CALLS_PER_TURN` | int | `3` | Logical Wolfram|Alpha requests allowed in one user turn, from 1 to 10. |
| `WOLFRAM_ALPHA_MAX_OUTPUT_CHARS` | int | `6800` | Provider and local result-text cap, from 500 to 20,000 characters. |
| `WOLFRAM_ALPHA_CALL_COST_USD` | float or null | null | Optional deployment-known price per logical call, including its possible bounded retry, for the local paid-tool ledger. Attempted calls record it even when the provider returns an error. |

The host sends `metric` or `nonmetric` units only when the model explicitly
selects one. The AppID travels in an HTTPS bearer header rather than the query
URL. Provider image URLs remain text and are not fetched. Transient transport
and HTTP failures receive one bounded retry inside the same logical allowance.

See [Wolfram|Alpha](wolfram-alpha.md) for setup, behavior, licensing, and the
provider-output boundary.

---

## Video understanding (gated)

The searchable member-tier `video` tool registers only when the feature flag
and its dedicated Gemini secret are both present. One flag enables public
YouTube plus streamed Discord/workspace video sources; uploaded files retain
code-owned 500 MiB/one-hour ceilings. The key is independent of
`config/models.yaml`; chat routing never supplies or inherits it.

| Env var | Type | Default | Description |
|---|---|---|---|
| `VIDEO_UNDERSTANDING_ENABLED` | bool | `false` | Requests registration of the stateful YouTube/uploaded-video tool. With a missing key, startup continues and leaves the tool absent. |
| `GEMINI_API_KEY` | secret | `""` | Dedicated Google Gemini API key. Environment-only and never written to a tool fragment. |
| `VIDEO_UNDERSTANDING_MAX_CONCURRENCY` | int | `4` | Process-wide interactive Gemini request cap, 1–32. Slot waits fail busy after 30 seconds; provider deletion uses a separate bounded pool. |

The fixed client connects only to Google's Gemini API. Safe live behavior is
owned by `config/tools/video.md` and listed in the per-tool table above. The
shipped defaults use `gemini-3.7-flash`, low thinking, four calls per outer
turn, twenty total interactions per session, and a 24-hour idle lifetime.

See [Video understanding](video-understanding.md) for source streaming, formats,
hard file/duration limits, root/user/guild scope, SQLite-backed crash recovery,
provider retention/deletion, caching, and prompt-injection posture.

---

## Image generation (gated)

The REGULAR-tier core `generate_image` tool is operator opt-in. ChatGPT OAuth
through the shared Codex token manager is primary; a dedicated OpenAI platform
key is the fallback. Missing credentials fail closed by leaving the tool
unregistered. Safe per-call behavior lives in
`config/tools/generate_image.md`.

| Env var | Type | Default | Description |
|---|---|---|---|
| `IMAGE_GEN_ENABLED` | bool | `false` | Requests registration of the image generation and editing tool. |
| `IMAGE_GEN_BACKEND` | str | `openai` | Image backend name. The supported value is `openai`. |
| `IMAGE_GEN_AUTH_MODE` | str | `auto` | `auto` prefers Codex OAuth and falls back to `IMAGE_GEN_API_KEY`; `oauth` and `api_key` select one path explicitly. |
| `IMAGE_GEN_API_KEY` | secret | `""` | Dedicated OpenAI platform key for API-key mode. Environment-only and never written to a tool fragment. |
| `IMAGE_GEN_MAX_CONCURRENCY` | int | `1` | Process-wide image request cap, 1–8. |
| `IMAGE_GEN_TIMEOUT_SECONDS` | float | `300` | Whole HTTP request timeout, 30–900 seconds. |

See [Image generation](image-generation.md) for request contracts, workspace
persistence, local file limits, quota errors, and the moderation boundary.

---

## Attachments & image input

Image attachments collected from a turn's messages are staged in a private directory, used once, and then cleaned up. These settings cap per-file and aggregate size, define the orphan sweeper, and set the vision detail hint the provider sees.

| Env var | Type | Default | Description |
|---|---|---|---|
| `ATTACHMENT_STORE_DIR` | path | `data/attachments` | Private temporary staging root for image attachments collected from Discord. |
| `ATTACHMENT_MAX_BYTES` | positive int | `8388608` (8 MiB) | Max accepted size for one staged attachment. |
| `ATTACHMENT_MAX_TOTAL_BYTES` | positive int | `33554432` (32 MiB) | Aggregate image bytes staged across current, reply, and recent-history candidates in one normal message turn. |
| `ATTACHMENT_ORPHAN_TTL_SECONDS` | positive int | `86400` | Age at which a crash-orphaned image stage becomes eligible for deletion. Normal turn cleanup is immediate. |
| `ATTACHMENT_ORPHAN_SWEEP_INTERVAL_SECONDS` | positive int | `3600` | Interval between bounded orphan scans; one scan also runs on READY startup. |
| `ATTACHMENT_ORPHAN_SWEEP_MAX_FILES` | positive int | `1000` | Maximum regular files inspected by one orphan scan; directory traversal is bounded proportionally and symlinks are never followed. |
| `IMAGE_DETAIL` | str | `auto` | Vision detail hint (`low`/`high`/`original`/`auto`); unknown values fall back to `auto`. |
| `RECENT_IMAGE_LOOKBACK` | int | `10` | How far back to look for a recent image to reference on replies or turns with stored conversation history. Fresh @mentions do not scan ambient channel images. |
| `MAX_TURN_IMAGES` | int | `10` | Max newly collected current/reply/recent vision images. Persisted history has its own database cap; `0` disables all image input, including persisted images. |

---

## Hindsight (memory backend)

Hindsight is the optional long-term memory backend. See `docs/memory.md`.

| Env var | Type | Default | Description |
|---|---|---|---|
| `HINDSIGHT_URL` | str | "" | Reachable Hindsight service URL, either self-hosted or hosted by a third party. Memory tools activate only when this is configured and startup bank initialization succeeds. Leave it empty until the endpoint is ready; [`bot/deploy/hindsight/`](../bot/deploy/hindsight/README.md) contains a generic self-hosted Docker deployment. |
| `HINDSIGHT_API_KEY` | secret | (none) | Optional Hindsight API key. |

---

## User memory

Per-user memory is default-on whenever Hindsight is enabled. Users opt out with `/memory opt-out` and back in with `/memory opt-in`. These settings tune what gets recalled during an ordinary turn and how much the recall budget costs.

Whenever Hindsight is available, each user's memory preference starts out
enabled. A user turns it off with `/memory opt-out` and back on with
`/memory opt-in`. That preference is independent of the deployment-wide
auto-retain switch below.

| Env var | Type | Default | Description |
|---|---|---|---|
| `MEMORY_RECALL_TYPES` | csv | `observation` | Hindsight memory types recalled for a responding turn. `observation` is the consolidated, deduplicated layer; adding raw `world`/`experience` re-injects every fact several times. |
| `MEMORY_RECALL_BUDGET` | str | `mid` | Hindsight recall budget tier for automatic responding-turn recall. |
| `MEMORY_RECALL_MAX_TOKENS` | int | `2048` | Token cap on the injected recalled-memory block. |
| `MEMORY_MAX_WRITES_PER_TURN` | int | `3` | Cap on proactive `remember_user_memory` writes per model turn. |
| `MEMORY_AUTO_RETAIN_ENABLED` | bool | `false` | Background idle-flush of conversation transcripts into each memory-enabled participant's bank; users are enabled by default unless they opt out (docs/memory.md). |
| `MEMORY_AUTO_RETAIN_IDLE_MINUTES` | int | `30` | Quiet time before a conversation becomes flushable. |
| `MEMORY_AUTO_RETAIN_SWEEP_INTERVAL_SECONDS` | int | `300` | Sweeper cadence. |
| `MEMORY_AUTO_RETAIN_MIN_USER_CHARS` | int | `80` | Slices with less user-authored text are skipped (watermark still advances). |
| `MEMORY_AUTO_RETAIN_MAX_CONTENT_CHARS` | int | `24000` | Cap per retain document; oversized slices split into part documents. |
| `MEMORY_AUTO_RETAIN_BACKFILL_HORIZON_HOURS` | int | `24` | Conversations first seen already idle longer than this are watermarked without retaining. |
| `MEMORY_AUTO_RETAIN_MAX_FLUSHES_PER_SWEEP` | int | `20` | Bounds extraction burst per sweep. |

**Derived:** `user_memory_recall_types` (list).

---

## Privacy consent gate (disabled by default)

This is the first-interaction accept/decline gate in `app/consent.py`. When it's
enabled, a user's first interaction posts a one-time notice and holds the message
until they accept, so nothing reaches the third-party LLM provider or SQLite
before consent. See [`docs/privacy.md`](privacy.md).

| Env var | Type | Default | Description |
|---|---|---|---|
| `PRIVACY_CONSENT_ENABLED` | bool | `false` | Master switch for the consent gate. |
| `PRIVACY_CONSENT_TITLE` | str | `Before we chat: a quick privacy note` | Title of the consent prompt. |
| `PRIVACY_CONSENT_TEXT` | str | _(third-party provider notice)_ | Body of the consent prompt; the full default wording lives in `config/settings.py`. |
| `PRIVACY_CONSENT_TIMEOUT` | float | `300.0` | Seconds the accept/decline buttons stay live before the prompt expires (timeout drops the message without a block). |
| `PRIVACY_POLICY_URL` | str | _(empty)_ | Where this deployment publishes its full privacy policy. Empty drops the link from the `/privacy` embed instead of pointing members at a page the deployment does not control; the shipped source text is [`privacy-policy.md`](privacy-policy.md). |

---

## Thread handoff (gated)

Thread handoff lets the model move a conversation into a Discord thread it creates
(`move_to_thread`) and keep responding there without being mentioned; `leave_thread`
sends a final reply, then locks and archives the managed thread (`tools/threads.py`). The
bot needs the Create Public Threads, Send Messages in Threads, and Manage Threads
permissions for this. See [`docs/thread-handoff.md`](thread-handoff.md).

A managed thread also carries a **mode**. Auto-responding, the default, answers every
message; paused falls back to the ordinary channel contract (@mention, reply-ping, or
`hey <bot> …`) while keeping the thread and its transcript. The user who asked for the
handoff, configured STAFF, or anyone with Discord's effective Manage Threads permission
can switch it with `pause_thread_replies` / `resume_thread_replies`, and the same
authorization governs `leave_thread`. The model can open a thread quiet with
`move_to_thread(auto_reply=false)`. New threads start in whichever mode the
`thread_auto_respond:` fragment key below selects.

`move_to_thread` can also open the thread in **another** channel
(`move_to_thread(name, channel="#bot-spam")`), adding the asker to it and pointing them
at it from where they asked. That stays off unless the guild fragment lists channels in
`thread_targets:`, and both the asker and the bot must be able to post in the target;
forums are never eligible.

| Env var | Type | Default | Description |
|---|---|---|---|
| `THREAD_HANDOFF_ENABLED` | bool | `true` | Registers the thread tools (`move_to_thread`, `leave_thread`, `pause_thread_replies`, `resume_thread_replies`). On by default, with no external dependency. Keep off while `MESSAGE_CONTENT_INTENT` is false (thread auto-reply needs the intent). |
| `THREAD_HANDOFF_SUGGEST_AFTER_TOOL_CALLS` | int | `5` | After this many completed substantive tool actions in an eligible non-thread guild turn, append one optional `move_to_thread` advisory before the next model iteration. Planning, tool discovery, and thread-control calls do not count. `0` disables the advisory. |
| `THREAD_AUTO_HANDOFF_ENABLED` | bool | `false` | Deterministic backstop: when the model does not thread but produces a long reply in a channel that opted in via frontmatter (`auto_thread_min_lines` / `auto_thread_min_chars` in `config/channels/<id>.md`, or `auto_thread_always: true` to enroll with no length check), synthesize the handoff. Requires `THREAD_HANDOFF_ENABLED`. Successful automatic and model-requested handoffs react to the parent message with 🧵. |

---

## Instruction fragments (channel and thread scopes)

Operators can drop short markdown files into one of three fragment directories to inject per-channel or per-thread instructions into the system prompt. All three are keyed by a bare Discord snowflake, so the directory a file sits in is the only thing that says what its id means:

| File | Keyed by | Applies to |
|---|---|---|
| `<CONFIG_DIR>/channels/<channel_id>.md` | the channel | that channel |
| `<CONFIG_DIR>/channel_threads/<channel_id>.md` | the **parent channel** | every thread under it |
| `<CONFIG_DIR>/threads/<thread_id>.md` | the thread | that one thread |

Resolution is most-specific-wins, first **non-empty body**
(`config/fragments/prompt.py:instruction_fragment_candidates`):

* Outside a thread: `channels/<channel_id>.md`, unchanged.
* Inside a thread: `threads/<thread_id>.md`, then
  `channel_threads/<parent_channel_id>.md`, then `channels/<parent_channel_id>.md`.

This is a **replacement, not an addition**: a thread-scoped body suppresses the
channel's own instructions inside that thread. Clearing the text (or deleting
the file) restores inheritance, since an empty body falls through to the next
scope. A thread with no thread-scoped fragment inherits its parent channel's
instructions; before these scopes existed it silently got an empty slot.

The two thread scopes are **body only**. `pinned_tools`, `blocked_tools`,
`thread_handoff`, `thread_auto_respond`, and the `auto_thread_*` keys are read
at channel and guild scope only, and inside a thread they resolve against the
parent channel. Everything is read fresh each turn, with no restart needed.

In a forum channel every post is a thread, so `channel_threads/<forum_id>.md` is
the fragment that actually gets used there.

A note on scope: all of this applies to mention and reply turns, including
managed-thread follow-ups.

A note on timing: the turn that opens a thread runs before the thread exists.
The opening reply resolves the ordinary channel scope, and thread-scoped
instructions take effect from the first follow-up onward, so write them for a
conversation that is already in progress.

It's worth reviewing channel instructions with threads in mind. A channel
fragment that says "keep replies short, this channel is busy" follows the
conversation into a handoff thread that exists precisely to hold the long
reply. If that reads wrong, put the threaded behavior in
`channel_threads/<channel_id>.md`, which replaces the channel body inside
threads.

You edit these fragment files directly.

## Instance layout

Keep the application checkout replaceable. A running instance has three kinds
of files, each with its own ownership and backup needs:

- `CONFIG_DIR` holds operator-authored prompts, model routing, policy, and
  scoped configuration. When change history is useful, it belongs in a
  separate, access-controlled configuration repository.
- `SKILLS_DIR` holds private shared skills. You provision it, but staff tools
  may update it at runtime, so back it up even when a separate private repository
  also keeps a versioned copy.
- The writable paths documented throughout this page, including those under
  [Storage](#storage), hold runtime state such as the database, attachments,
  personal skills, workspaces, and logs. Treat them as user data, not as
  source-controlled configuration.

The checkout-relative defaults make local development easy and are excluded
from version control wherever they may contain live state. In production, use
durable paths outside the checkout so that an application upgrade or container
replacement can't take instance data with it. One possible layout is:

```text
/srv/kimi/
|-- app/                 # replaceable application checkout
|-- private/
|   |-- config/          # CONFIG_DIR
|   `-- skills/          # SKILLS_DIR
`-- instance/
    |-- data/            # database, attachments, personal skills
    |-- workspaces/
    |-- logs/
    `-- secrets/
```

Absolute paths are the safer choice in production, because relative paths are
resolved from the process working directory, which may differ between a shell,
a service manager, and a container. The directories don't need to share a
parent; the example only illustrates the separation. See
[Instance Data](instance-data.md) for the full public/private boundary,
recommended path settings, backup notes, and the provisioning procedure.

| Env var | Type | Default | Description |
|---|---|---|---|
| `CONFIG_DIR` | path | `config` | Operator config root: `prompt.md`, `persona.md`, `models.yaml`, `settings.md`, `tools.md`, and the `channels/`, `channel_threads/`, `threads/`, `servers/`, `prompts/`, `modules/`, `plugins/`, and `tools/` fragment trees. Consumed by prompt construction, guild/channel fragment loaders, core, module, and plugin settings overlays, tool policy/config, and model routing. |
| `SKILLS_DIR` | path | `skills/store` | Private durable instruction-skill store scanned by `skills/loader.py` and managed by staff skill tools. It is deployment data and is not stored in the repository; a missing store contributes no private skills, while shipped built-ins remain available. |
| `PLUGIN_MODULES` | CSV of module paths | _(empty)_ | Explicit operator-plugin allowlist; there is no filesystem or package auto-discovery. Each importable module exposes `register(ctx) -> None` (`app/plugins.py`). Loading constructs declared plugin settings from the same `ENV_FILE` as core and applies `<CONFIG_DIR>/plugins/<name>.md` before registration. Core tools register first; a plugin registration failure or invalid overlay skips only that plugin and rolls back partial registrations. |
| `KIMI_MODULES` | CSV of entry-point names | _(empty)_ | Explicit application-module allowlist. Installed packages are discovered through `kimi_agent.modules`, but only named modules load. Missing dependencies, incompatible APIs, invalid settings, or lifecycle failures abort startup. See [modules.md](modules.md). |
| `MODULE_START_TIMEOUT_SECONDS` | int | `60` | Ceiling for one module's `start()`. Exceeding it fails that module and aborts startup, like any other module start failure. Must be ≥ 1. See [modules.md](modules.md#lifecycle-contract). |
| `MODULE_CLOSE_TIMEOUT_SECONDS` | int | `15` | Ceiling for one module's `close()` during shutdown. Exceeding it cancels that close, logs an error, and continues with the next module. Must be ≥ 1. |
| `MODULE_SCHEDULER_MAX_CONCURRENT_JOBS` | int | `4` | How many module scheduler jobs may run at once across all modules; at most one job per module runs at a time. Must be ≥ 1. |

## Storage

The bot stores conversation transcripts, usage ledgers, provider circuit state, and optional per-user persona text in a single SQLite database. These settings pick the database file, the retention window for transcripts, and personal skill storage. See [database.md](database.md) for the schema, the encryption option, and the sweeper.

| Env var | Type | Default | Description |
|---|---|---|---|
| `DATABASE_PATH` | path | `data/bot.db` | SQLite database path (WAL). Production state; back it up consistently with its WAL sidecars. See [`docs/database.md`](database.md). |
| `DATABASE_ENCRYPTION_KEY` | secret | "" | SQLCipher encryption-at-rest passphrase. Empty (default) = plaintext `sqlite3`; when set, the DB is opened through `sqlcipher3` and keyed before any access. Losing the key makes an encrypted DB unrecoverable. Convert an existing plaintext DB with `sqlcipher_export` (see [`docs/database.md`](database.md)); do not just set this on it. Requires the Linux-only `sqlcipher3-binary` dependency. |
| `TRANSCRIPT_RETENTION_DAYS` | int | `30` | Rolling retention window for raw conversation transcripts. A background sweep purges a whole conversation (and its `messages`/routing/metadata/watermark rows) when `conversations.last_active_at` falls outside this window. Memory and the usage ledger are not on this clock. `0` disables the sweep (keep forever). Must be ≥ 0. See [`docs/privacy.md`](privacy.md). |
| `TRANSCRIPT_RETENTION_SWEEP_INTERVAL_SECONDS` | int | `3600` | How often the transcript-retention and video-session/provider-deletion sweepers run. Must be ≥ 1. |
| `PERSONAL_SKILLS_DIR` | path | `data/personal_skills` | Durable per-user instruction-only personal skill store. Not executable and not swept by workspace cleanup. |
| `USER_PERSONA_MAX_CHARS` | int | `2000` | Max compiled persona text stored in SQLite and inserted into the `<persona>` prompt slot. Must be positive. |
| `USER_PERSONA_REQUEST_MAX_CHARS` | int | `8000` | Max raw user request accepted by `persona_set`. Must be positive. |
| `USER_PERSONA_COMPILER_MAX_TOKENS` | int | `32000` | Max output tokens for the persona compiler call. Must be positive. The model itself, and that call's timeout, come from the `persona` role in `config/models.yaml`. See [`docs/persona.md`](persona.md). |

---

## Observability

These settings control the structured tool-call event stream. See
`docs/observability.md`.

| Env var | Type | Default | Description |
|---|---|---|---|
| `TOOL_EVENT_LOG_ENABLED` | bool | `false` | Emit JSONL tool-call, compaction, per-turn summary, moderation, and module-health events. |
| `TOOL_EVENT_LOG_PATH` | path | `logs/events.jsonl` | Output JSONL path. |
| `TOOL_EVENT_LOG_CONTENT_MODE` | str | `metadata` | How much content each row carries: `metadata`, `redacted`, or verbatim `full`. Set here only, never from the overlay. |
| `TOOL_EVENT_LOG_MAX_FIELD_BYTES` | int | `8192` | Truncation cap per field (keeps args/results bounded). |

---

## Secrets file

Exposable skills can declare named secrets that are loaded from this YAML file on demand. The file is optional and not required for an instruction-only deployment. See the [shared skill store documentation](../bot/skills/README.md#private-store) for the declaration format.

| Env var | Type | Default | Description |
|---|---|---|---|
| `SECRETS_FILE` | path | `secrets/secrets.yaml` | YAML of named secrets injected into skill scripts on demand (declared per skill). |

The file itself is optional when no executable skill needs a secret. If it is
absent, Kimi logs `Secrets file not found: <path>` and continues with an empty
store. An empty or non-mapping YAML document also produces an empty store. To
make an intentional no-secrets deployment quiet and explicit, create the file
with `{}` and owner-only permissions. A parse/read failure logs the exception
and likewise leaves the store empty.

`secrets/secrets.yaml` is a top-level YAML mapping:

```yaml
SOME_API_KEY: sk-...
OTHER_TOKEN: token-value
```

Instruction-only skills never receive these values. Executable skills declare the names
they need in `requires_secrets:` frontmatter, and the runner copies only those declared
names into the child process's environment. Every executable tool in a secret-backed skill
is forced to at least the `staff` trust tier during runtime registration, even if
hand-edited metadata declares a lower tier. If a declared secret is missing from the store,
the whole skill is skipped with a warning and none of its tools register. That is the same
fail-closed rule every other credential-gated tool follows: an absent key hides the tool
rather than exposing one that would fail mid-call.

`skills/runner.py` starts from a small environment: `PATH`, the declared secrets,
`WORKSPACE_DIR`, and a private temporary home (`HOME`, `USERPROFILE`, `XDG_CACHE_HOME`,
`XDG_CONFIG_HOME`, and `MPLCONFIGDIR` all point there). If a secret tries to declare a
reserved env name such as `PATH`, `LD_PRELOAD`, `DYLD_INSERT_LIBRARIES`, `PYTHONPATH`,
`NODE_OPTIONS`, `BASH_ENV`, `ENV`, `IFS`, or one of the scratch-home names, the runner
ignores it. Inside the Linux sandbox the temporary home is `/tmp/home` and `WORKSPACE_DIR`
is `/workspace`. Environment filtering complements the mount and process boundary, but it
is no protection against a secret you deliberately hand to the script. Captured
stdout/stderr and text output files are scrubbed for exact declared-secret values and
capped, but encoding, splitting, or otherwise transforming a secret slips past that
scrubber, and a network-enabled script can transmit it.

---

## Script execution (skills)

Skill-backed scripts run in a mandatory Linux Bubblewrap sandbox (`skills/runner.py`). If
any configured skill declares executable tools, startup requires an unprivileged service
account, `bwrap`, `prlimit`, and a successful namespace probe; root is rejected, and there
is no unsandboxed production fallback. A clean, instruction-only skill store can still be
used for development on other platforms.

Each invocation gets its own user, mount, PID, IPC, UTS, cgroup, and default-denied network
namespaces. It drops all capabilities, disables nested user namespaces, mounts the skill and
interpreter read-only, exposes a private `/proc` and a size-capped tmpfs `/tmp`, and mounts
only that call's output workspace read/write at `/workspace`. The host root, the service
home, other workspaces, deployment secrets and config, `/sys`, and the host `/etc` are all
absent. A per-tool `network: true` declaration shares the host network and adds minimal
resolver and CA mounts; be aware that this allows every public, private, and loopback
destination reachable from the service, and is not a hostname allowlist.

Bubblewrap's PID namespace reaps descendants, including new-session children. `prlimit`
supplies inherited per-process virtual-memory, CPU-time, file-size, open-file,
process-count, and core-file limits. These are not aggregate cgroup accounting, and the
process-count limit is per real UID, which is why executable-skill startup rejects root.
Run the bot under a dedicated unprivileged service account. The example systemd
unit, [`bot/deploy/kimi.service.example`](../bot/deploy/kimi.service.example), applies
`TasksMax=128`, `MemoryMax=4G`, and `CPUQuota=200%` to the complete service cgroup,
including executable-skill descendants. Tune those aggregate ceilings for the host, use
equivalent container limits outside systemd, and retain service-level egress controls as a
second layer.

| Env var | Type | Default | Description |
|---|---|---|---|
| `SCRIPT_DEFAULT_TIMEOUT` | int | `1200` | Default per-script timeout (s). |
| `SCRIPT_MAX_TIMEOUT` | int | `1200` | Ceiling on the requested wall-clock timeout (s). |
| `SCRIPT_MAX_CONCURRENCY` | int | `2` | Max concurrent script subprocesses. |
| `SCRIPT_OUTPUT_MAX_CHARS` | int | `200000` | Cap on captured script output. |
| `SCRIPT_OUTPUT_MAX_FILES` | int | `10` | Max files attached from one skill job workspace. |
| `SCRIPT_OUTPUT_MAX_FILE_BYTES` | int | `26214400` (25 MiB) | Max size for each auto-attached skill output file. |
| `SCRIPT_OUTPUT_MAX_SCAN_ENTRIES` | int | `1000` | Max file entries scanned when collecting skill output files. |
| `SCRIPT_SANDBOX_MEMORY_MAX_MB` | positive int | `2048` | Per-process virtual-address-space limit (MiB), inherited by descendants. This is not resident-memory or aggregate job accounting. |
| `SCRIPT_SANDBOX_CPU_SECONDS` | positive int | `300` | Per-process CPU-time limit (s), separate from wall time. |
| `SCRIPT_SANDBOX_MAX_FILE_BYTES` | positive int | `104857600` (100 MiB) | Kernel file-size limit for each file created by a sandboxed process. Auto-attachment remains bounded by `SCRIPT_OUTPUT_MAX_FILE_BYTES`. |
| `SCRIPT_SANDBOX_MAX_OPEN_FILES` | positive int | `256` | Per-process open-file descriptor limit. |
| `SCRIPT_SANDBOX_MAX_PROCESSES` | positive int | `64` | Per-real-UID process limit inherited by the script; executable-skill startup requires a non-root service user because this resource is UID-global. |
| `SCRIPT_SANDBOX_TMPFS_MAX_MB` | positive int | `256` | Size cap for the invocation's private `/tmp` tmpfs. |

---

## Code execution

`run_code` is Linux-only, disabled by default, and available to `MEMBER` and above unless `CODE_EXEC_MIN_TIER` raises the requirement. Setting the flag is not enough on its own: the sandbox and network profile you selected must pass their startup probe before the tool registers. See [code-exec.md](code-exec.md) for the full threat model, network modes, and provisioning steps.

| Setting | Type | Default | Meaning |
|---|---|---:|---|
| `CODE_EXEC_ENABLED` | bool | `false` | Request registration of `run_code`; a failed live sandbox probe still leaves it unavailable. |
| `CODE_EXEC_MIN_TIER` | `member`/`regular`/`staff` | `member` | Lowest trust tier that can see and use `run_code`. Raising it requires a restart. |
| `CODE_EXEC_NETWORK_MODE` | `none`/`host`/`netns` | `none` | Deployment-wide network boundary. `host` exposes every host-reachable route; `netns` requires the privileged settings below. |
| `CODE_EXEC_PYTHON_BIN` | path | `/usr/bin/python3` | System Python, used when no shared packages venv is configured. A networked mode also needs it to support venv creation. |
| `CODE_EXEC_VENV_DIR` | path | empty | Optional dedicated packages venv mounted read-only; never point this at the bot environment. When set, its `bin/python3` becomes the interpreter every run uses, and a networked mode needs that one to support venv creation. |
| `CODE_EXEC_EXTRA_RO_BINDS` | comma-separated paths | empty | Additional read-only mounts. Paths must not contain secrets. |
| `CODE_EXEC_BWRAP_BIN` | command | `bwrap` | Bubblewrap executable. |
| `CODE_EXEC_PRLIMIT_BIN` | command | `prlimit` | util-linux resource-limit executable. |
| `CODE_EXEC_SYSTEMD_RUN_BIN` | command | `systemd-run` | Transient cgroup scope/service launcher. |
| `CODE_EXEC_SUDO_BIN` | command | `sudo` | Netns-only privilege boundary launcher. |
| `CODE_EXEC_NETNS_HELPER_BIN` | path | empty | Netns-only root-owned helper. Its file selects one namespace; the model never supplies a namespace. |
| `CODE_EXEC_NETNS_RESOLV_CONF` | path | empty | Netns-specific resolver file hard-mounted as `/etc/resolv.conf`. |
| `CODE_EXEC_NETWORK_PROBE_BLOCKED_IP` | host or host:port | empty | Netns-only known-open private service that the startup probe must find unreachable. Required when enabled in `netns` mode. |
| `CODE_EXEC_WALL_TIMEOUT_SECONDS` | positive float | `300` | Per-run wall-clock deadline. |
| `CODE_EXEC_MAX_CPU_SECONDS` | positive int | `240` | Per-process CPU-time rlimit. |
| `CODE_EXEC_MAX_MEMORY_MB` | positive int | `3072` | Per-process virtual address-space rlimit. |
| `CODE_EXEC_MAX_TASKS` | positive int | `256` | Whole-cgroup task/process cap. |
| `CODE_EXEC_MAX_TOTAL_MEMORY_MB` | positive int | `2048` | Whole-cgroup real-memory cap; swap is disabled. |
| `CODE_EXEC_CPU_QUOTA_PERCENT` | positive int | `200` | Whole-cgroup aggregate CPU quota; `100` is one full core. |
| `CODE_EXEC_TMP_SIZE_MB` | positive int | `512` | Private `/tmp` tmpfs size cap. |
| `CODE_EXEC_MAX_FSIZE_MB` | positive int | `128` | Per-file-size rlimit. |
| `CODE_EXEC_MAX_OPEN_FILES` | positive int | `1024` | Open-file-descriptor rlimit. |
| `CODE_EXEC_MAX_WORKSPACE_FILES` | positive int | `50000` | Ordinary workspace entry ceiling monitored during a run. |
| `CODE_EXEC_WORKSPACE_QUOTA_POLL_SECONDS` | positive float | `5` | Seconds between complete in-flight workspace accounting scans; preflight and final scans always run. |
| `CODE_EXEC_WORKSPACE_QUOTA_SCAN_RETRIES` | int (`1`–`10`) | `4` | Total complete-scan attempts allowed for transient `ENOENT`/`ESTALE` races before accounting fails closed. Other scan errors fail immediately. |
| `CODE_EXEC_MAX_OUTPUT_BYTES` | positive int | `40000` | Independent bounded capture for stdout and stderr. |
| `CODE_EXEC_MAX_CONCURRENCY` | positive int | `1` | Bot-wide concurrent run count for `none`/`host`; netns remains single-slot. |
| `CODE_EXEC_ENV_DIR_MAX_MB` | positive int | `2048` | Per-workspace bytes allowed for regenerable `.venv`/`.pio` trees. |
| `CODE_EXEC_ENV_DIR_MAX_FILES` | positive int | `200000` | Regenerable environment entry allowance, including zero-byte files and directories. |
| `CODE_EXEC_NETWORK_WEEKLY_LIMIT` | nonnegative int | `100` | Per-user rolling seven-day run cap in `host`/`netns`; `0` disables and `STAFF` is exempt. |

The project does not include deployment-specific names for a VPN provider,
namespace, resolver, private address, production service unit, or host. Those
details remain private to each deployment. The generic helper and sudoers
templates live under
[`bot/deploy/code-exec-netns/`](../bot/deploy/code-exec-netns/README.md).

## Persistent browser

The optional persistent browser and visual renderer are Linux-only and share an external root-owned runtime. Visual jobs are one-shot and offline. The browser's network mode is chosen independently of code execution. See [browser.md](browser.md) for isolation details, network modes, and the upgrade procedure. The `BROWSER_NETNS_*` and probe
values are private instance state under the same rule as the code-exec ones
above.

| Setting | Type | Default | Meaning |
|---|---|---|---|
| `BROWSER_ENABLED` | bool | `false` | Request registration of `browser` and, when the exact Mermaid asset is present, searchable `render_chart` and `render_diagram`; missing runtime or a failed shared sandbox/network probe leaves them unavailable. There is no separate visual flag. |
| `BROWSER_NETWORK_MODE` | `host`/`netns` | `host` | Fixed network boundary. `netns` uses the VPN helper and never falls back to host. |
| `BROWSER_RUNTIME_DIR` | path | `/opt/kimi/betterwright` | Root-owned pinned BetterWright, Mermaid, Node, and BetterChromium runtime. |
| `BROWSER_PROFILES_DIR` | path | `data/browser_profiles` | Private per-user persistent profile root. |
| `BROWSER_BRIDGE_SCRIPT` | relative path | `web_browser/bridge.mjs` | Application-owned BetterWright JSON bridge under `bot/`. |
| `BROWSER_BWRAP_BIN` | command | `bwrap` | Bubblewrap executable. |
| `BROWSER_PRLIMIT_BIN` | command | `prlimit` | util-linux resource-limit executable. |
| `BROWSER_SYSTEMD_RUN_BIN` | command | `systemd-run` | Transient cgroup launcher. |
| `BROWSER_SYSTEMCTL_BIN` | command | `systemctl` | Used to confirm and stop worker units. |
| `BROWSER_SUDO_BIN` | command | `sudo` | Netns-only privilege boundary launcher. |
| `BROWSER_NETNS_HELPER_BIN` | path | empty | Netns-only fixed, root-owned namespace helper. |
| `BROWSER_NETNS_RESOLV_CONF` | path | empty | Netns resolver hard-mounted at `/etc/resolv.conf`. |
| `BROWSER_NETWORK_PROBE_BLOCKED_IP` | host or host:port | empty | Known-open private target the netns startup probe must find unreachable. |
| `BROWSER_CALL_TIMEOUT_SECONDS` | positive float | `30` | Deadline for one persistent-browser JavaScript step and the base deadline for one offline visual render. |
| `BROWSER_START_TIMEOUT_SECONDS` | positive float | `20` | Worker readiness deadline. |
| `BROWSER_IDLE_TTL_SECONDS` | positive float | `120` | Close an unused browser worker after this interval. |
| `BROWSER_WORKER_MAX_LIFETIME_SECONDS` | positive int | `3600` | Hard worker lifetime before recycling. |
| `BROWSER_PROFILE_TTL_SECONDS` | positive int | `604800` | Delete an inactive profile after this interval. |
| `BROWSER_MAX_PROFILE_MB` | positive int | `512` | Maximum bytes retained in one user's browser profile. |
| `BROWSER_MAX_SCREENSHOT_BYTES` | positive int | `8388608` | Maximum accepted size for one screenshot artifact or rendered visual PNG. |
| `BROWSER_MAX_TOTAL_MEMORY_MB` | positive int | `2048` | Whole-worker-cgroup real-memory cap; swap is disabled. |
| `BROWSER_MAX_TASKS` | positive int | `256` | Whole-worker-cgroup process/task cap. |
| `BROWSER_CPU_QUOTA_PERCENT` | positive int | `200` | Aggregate CPU quota; `100` is one full core. |
| `BROWSER_TMP_SIZE_MB` | positive int | `512` | Private `/tmp` tmpfs cap. |
| `BROWSER_MAX_FSIZE_MB` | positive int | `128` | Per-file-size rlimit. |
| `BROWSER_MAX_OPEN_FILES` | positive int | `1024` | File-descriptor rlimit. |
| `BROWSER_TIMEZONE` | string | `UTC` | Browser context timezone. |
| `BROWSER_LOCALE` | string | `en-US` | Browser context locale. |

## Workspaces (per-user file sandbox)

Each ordinary (user, guild) pair gets a sandboxed directory under `WORKSPACE_DIR`, keyed by `workspace_owner_key`. Personal `/chat` and DM turns use a separate `user_app_workspace_key` across invocation locations. These settings cap file lifetimes, the sweeper cadence, and the limits enforced by every workspace tool. See [workspace.md](workspace.md) for ownership rules and the path-resolution contract.

| Env var | Type | Default | Description |
|---|---|---|---|
| `WORKSPACE_DIR` | path | `workspaces` | Root for per-user workspaces. |
| `WORKSPACE_FILE_TTL` | positive int | `604800` | File lifetime before sweep (s); defaults to 7 days. |
| `WORKSPACE_MAX_SIZE_MB` | positive int | `150` | Per-workspace quota. |
| `WORKSPACE_SWEEP_INTERVAL` | positive int | `300` | Workspace sweeper interval (s). |
| `WORKSPACE_TOOL_MAX_FILE_BYTES` | int | `52428800` (50 MiB) | Max single-file write. |
| `WORKSPACE_TOOL_MAX_USER_BYTES` | int | `157286400` (150 MiB) | Per-user document-file cap. Regenerable env dirs use the separate caps above. |
| `WORKSPACE_TOOL_MAX_READ_BYTES` | int | `26214400` (25 MiB) | Max whole-file size `read_file`/`edit_file` will load and cumulative PDF extraction output. `grep_workspace` streams and is not bound by this. |
| `WORKSPACE_TOOL_MAX_PDF_PAGES` | positive int | `500` | PDFs above this page count are rejected before page text extraction. |
| `WORKSPACE_TOOL_MAX_TEXT_CHARS` | int | `65536` | Max chars surfaced from a text file (also caps `grep_workspace` total output). |
| `WORKSPACE_TOOL_MAX_ATTACHMENTS` | int | `5` | Max files queued on one outgoing reply. |
| `WORKSPACE_TOOL_MAX_IMPORT_BYTES` | int | `26214400` (25 MiB) | Max size of an imported attachment. |
| `WORKSPACE_TOOL_MAX_ZIP_ENTRIES` | int | `10000` | Max entries when zipping/extracting. |
| `WORKSPACE_TOOL_MAX_EXTRACT_TOTAL_BYTES` | int | `157286400` (150 MiB) | Max total uncompressed extract size (zip-bomb guard). |
| `WORKSPACE_TOOL_FETCH_TIMEOUT_SECONDS` | float | `30.0` | `fetch_url` timeout. |
| `WORKSPACE_TOOL_MAX_REDIRECTS` | int | `5` | Max redirects for `fetch_url`/downloads (each hop SSRF-revalidated). |
| `WORKSPACE_TOOL_DEFAULT_GREP_RESULTS` | int | `50` | Default `grep_workspace` match cap. |
| `WORKSPACE_TOOL_MAX_GREP_RESULTS` | int | `200` | Hard `grep_workspace` match cap. |
| `WORKSPACE_TOOL_MAX_GREP_CONTEXT` | int | `20` | Max surrounding lines per `grep_workspace` match. |
| `WORKSPACE_TOOL_MAX_GREP_LINE_CHARS` | int | `1000` | Max chars per surfaced grep line. |
| `WORKSPACE_TOOL_MAX_GREP_PATTERN_CHARS` | int | `256` | Max grep pattern length. |
| `WORKSPACE_TOOL_GREP_TIMEOUT_SECONDS` | float | `5.0` | Wall-clock ceiling for the regex matching in one `grep_workspace` call. The `regex` engine honors this mid-match and releases the GIL, so a catastrophic pattern is bounded instead of pinning the event loop. |
| `WORKSPACE_TOOL_GLOB_MAX_RESULTS` | int | `200` | Max file paths `glob_workspace` returns before truncating. |
| `WORKSPACE_TOOL_MULTI_EDIT_MAX_OPS` | int | `50` | Max number of edits `multi_edit` applies in one call. |
| `WORKSPACE_TOOL_VIEW_IMAGE_MAX_BYTES` | int | `5242880` (5 MiB) | Max size of a single workspace image `view_image` will show the model. |
| `WORKSPACE_TOOL_VIEW_IMAGE_MAX_PER_TURN` | int | `4` | Max images `view_image` surfaces to the model per reply. |
| `WORKSPACE_TOOL_MAX_ENTRIES` | int | `20000` | Max files + directories in one workspace. New-entry writes past this are refused; existing files stay editable and deletable. |

`extract_document_text` pulls readable text out of workspace documents into a generated
file (`.txt` for PDFs via PyMuPDF, `.md` for office formats (Word, Excel, PowerPoint,
OpenDocument, RTF, EPUB, CSV) via firecrawl-anydoc) and returns a `read_file` call hint so
the model can read it in chunks. It respects the existing single-file, per-user quota, and
read-output caps; PDF output accumulates incrementally up to the read-output cap, and all
native document parsing is serialized process-wide, including after an awaiting turn is
cancelled. Note that PyMuPDF still materializes one whole page before application code
gets a chance to truncate its text, so until parsing moves into a disposable worker the
repository offers no per-document memory boundary. You must set a whole-service memory
limit in the Linux service or container. The tool doesn't OCR scanned pages.

---

## Validators & startup behavior

- `ALLOWED_CHANNEL_IDS`, `ALLOWED_GUILD_IDS`, `STAFF_ROLE_IDS`, `REGULAR_ROLE_IDS`: each entry
  must be a numeric Discord ID; a bad entry raises at startup with a clear message.
- `DISCORD_SEARCH_EXCLUDED_CHANNELS`: every non-empty entry must be a unique numeric channel ID.
- `MODERATION_OUTPUT_EXEMPT_TIER`: must name a real trust tier.
- `REACT_TEMPERATURE`: a blank string is coerced to `None` (omit the param) instead of failing.
- **Model config**: `config/models.yaml` is parsed at startup. Unknown fields,
  unknown provider types, unknown secret env names, and broken model references
  all fail fast.
- **Codex auth**: validated at startup if any reachable enabled model role uses
  a Codex provider profile.

---

## External env keys outside `Settings`

Almost every setting the bot reads lives inside the `Settings` model. The only core-facing exception is `ENV_FILE`, which must be read from the raw process environment before `Settings` is constructed (see "Choosing a dotenv file" above). Plugin and module settings can also live outside the core model when the plugin or module explicitly declares a safe subset for operator files.

The separately deployed Hindsight Compose stack reads its own provider route and credentials from the adjacent `.env`; see [Deployment notes](#deployment-notes).

---

## Primary consumers

If you want to know where a setting is actually read, this is the map:

- `app.runtime.build_app(settings)` constructs providers, memory clients, tool
  registrations, stores, workspaces, attachment handling, startup validation,
  and Discord launch resources. `bot.py` only loads settings, builds the app,
  and runs it.
- `config/model_config.py` loads `config/models.yaml` and converts model entries
  plus provider profiles into `ProviderConfig` values.
- `providers/factory.py` consumes `ProviderConfig` and constructs concrete
  providers.
- `tools/discord_text_search.py` and `tools/threads.py` consume their optional
  tool sections.
- Plugin modules named in `PLUGIN_MODULES` read their settings from the same
  `ENV_FILE` as core. A plugin may explicitly expose safe, non-secret scalar
  fields through `<CONFIG_DIR>/plugins/<name>.md`; credentials, connection
  targets, paths, and every field explicitly classified as environment-only are
  documented by the plugin, not here.
- `app/moderation.py` and `moderation/` consume the content moderation settings.
- `app/consent.py` consumes the privacy consent settings.
- `memory/`, `tools/user_memory.py`, `tools/community.py`, and
  `commands/memory_cmd.py` consume the Hindsight, user recall, explicit
  memory-write, and community-memory settings.
  `discord_adapter/lifecycle.py` owns the workspace and auto-retain sweeper loops.
- `agent/attachments.py`, `agent/backfill.py`, `workspace/manager.py`, and `tools/workspace/`
  consume the attachment, channel-backfill, and workspace limits.
- `skills/loader.py`, `skills/registration.py`, `skills/runner.py`, and `skills/secrets.py`
  consume `SECRETS_FILE`, the script execution limits, and the workspace bounds for
  executable skill tools.
- `skills/personal.py` and `tools/personal_skills.py` consume `PERSONAL_SKILLS_DIR` for durable
  per-user instruction-only personal skills.
- `storage/db.py` uses `DATABASE_PATH`; `observability/events.py` (wired in
  `app/runtime.py`) and `docs/observability.md` cover the tool-event log settings.

---

## Deployment notes

- Your local `.env` wins over the code defaults. It is deployment data and is
  excluded from source control.
- The included `.env.example` leaves `HINDSIGHT_URL` empty. Set it explicitly
  to the reachable endpoint; `deploy/hindsight/` contains a generic self-hosted
  Docker Compose deployment and bring-up instructions.
- The Compose stack reads its Hindsight provider route and credential variables
  from the local `.env` beside its compose file. Keep that file outside the repository; see
  `deploy/hindsight/README.md`.
