# Kimi

> A self-hosted Discord assistant with a provider-neutral tool loop.

![Python](https://img.shields.io/badge/python-3.14+-blue.svg)
![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)
![discord.py](https://img.shields.io/badge/discord.py-2.7+-5865F2.svg)

Kimi is a generalist assistant for Discord communities. It responds only when
invoked. It runs a provider-neutral
[ReAct](https://arxiv.org/abs/2210.03629) tool-use loop behind trust tiers and
config gates: it can search configured Discord history, work with per-user
files, build structured embeds, move a conversation into a managed thread, and
remember durable facts about who it's talking to. Staff can separately opt
channels into local known-bad image fingerprint enforcement.

The bot's runtime name comes from `BOT_NAME` (default `Kimi`) and is never
hardcoded on the Discord-facing surface; the persona lives in
[`config/persona.md`](config/persona.md). This setting does not rename the
Discord account itself. For a complete rename, update the existing
application/bot identity in the Discord Developer Portal so its bot ID, token,
guild installations, and permissions stay intact.

## What it does

- **Invocation-gated chat.** Replies to an @mention, a pinged reply, a
  `hey/hi <bot name>` / `<bot name> help` text invocation, or any message in a
  thread it created via handoff; ignores DMs; and keeps conversations rooted in
  SQLite so a reply continues its own thread even across restarts.
- **Provider-neutral.** Route chat, compaction, and optional durable coding models from
  `config/models.yaml`: OpenAI-compatible (Chat Completions or Responses),
  Anthropic (native or compat gateway), OpenRouter, or Codex. The agent core
  never touches provider-specific types. See [docs/providers.md](../docs/providers.md).
- **Trust-tiered tools.** `MEMBER < REGULAR < STAFF`, resolved from Discord
  roles and enforced at dispatch, not by prompt text. See the
  [built-in tool catalog](../docs/tools.md).
- **Long-term memory (optional).** Per-user banks backed by
  [Hindsight](https://github.com/vectorize-io/hindsight), with proactive writes,
  recall, reflection, opt-out controls, and staff-led per-guild community
  knowledge through the `teach` tool. See [docs/memory.md](../docs/memory.md).
- **Per-user workspaces.** Application-contained file tools (read/write/edit,
  archive extract, URL fetch) with path, size, quota, and TTL caps. This boundary
  applies to the file tools; operator-authored skill scripts use a separate
  Linux Bubblewrap boundary with only their exact per-call workspace writable.
- **Durable coding agent (optional).** Hand repository-scale work to a
  separately routed background worker with persisted progress, managed sandbox
  jobs, recovery, steering, and cancellation. See
  [docs/coding-agent.md](../docs/coding-agent.md).
- **Discord context and search.** Fetch recent channel context on demand and,
  when configured, search selected Discord channels without persisting the
  retrieved messages.
- **Video understanding (optional).** Ask a stateful Gemini 3.7 Flash specialist
  about a public YouTube video or a streamed Discord/workspace clip up to 500 MiB,
  then continue with rooted follow-ups and timestamped evidence. See
  [docs/video-understanding.md](../docs/video-understanding.md).
- **Browser and visual rendering (optional).** Keep per-user BetterWright
  profiles for interactive web work and expose a searchable call that renders
  accessible fixed-style charts or constrained Mermaid PNGs. Visual jobs use a
  separate ephemeral offline worker. See [docs/browser.md](../docs/browser.md)
  and [docs/visual-rendering.md](../docs/visual-rendering.md).
- **Managed threads.** Move a conversation into a bot-created thread, pause or
  resume automatic replies, and preserve the rooted transcript across the
  handoff. See [docs/thread-handoff.md](../docs/thread-handoff.md).
- **Skills.** Built-in read-only Markdown guidance ships with the bot; staff
  manage deployment-owned instruction docs from inside Discord.
  Operator-authored script-backed tools run under mandatory Linux isolation and
  default-denied networking; the writable store is private instance data. Staff
  can also teach from a selected human message through the **Teach Kimi**
  context menu (or **Teach &lt;name&gt;** when `BOT_NAME` is customized).
- **Discord commands.** `/memory`, `/mod`, `/moderation`, `/privacy`, and
  `/usage` expose user controls and staff operations; owner-only `/models`
  changes the global chat model without restarting the bot.
- **Safety rails.** Optional privacy consent and LLM content moderation, user
  blocks, trust-tiered tools, moderation cases/logging, and read-only
  FingerprintHub sync with bounded local image matching.
- **Operator plugins.** Community-specific tools and guild scoping load from
  your own packages via an explicit `PLUGIN_MODULES` allowlist, without forking
  the core. See [docs/plugins.md](../docs/plugins.md).
- **Observability.** Structured JSONL tool-event logging for local inspection.
  See [docs/observability.md](../docs/observability.md).

## How it works

```
  @mention / pinged reply / "hey <bot name>"
          │
   eligibility + invocation gates   (DMs, blocked users, ping-off replies dropped)
          │
   rooted conversation              (DB-mapped reply continues its root)
          │
   fresh ConversationContext        (recent SQLite transcript rows)
          │
   ReAct loop  ───────────►  provider.run_turn   (OpenAI / Anthropic / Codex / …)
          │                        │
   tool registry  ◄────────────────┘             (trust-tiered, per-conversation activation)
          │
   Discord-safe reply + attachments
```

Foreground chat turns are stateless: there's no chat-provider continuation
state. Each fresh mention opens a rooted logical conversation keyed by the
triggering message id, and the transcript is rebuilt from persisted SQLite rows
each turn. The optional `video` tool is a deliberate specialist exception: its
actor-scoped Gemini Interaction chain persists behind an opaque rooted handle. The full flow lives
in `app/runtime.py` → `agent/turn.py` → `agent/core.py`.

## Architecture

| Area | Where | Docs |
|------|-------|------|
| Composition root & message flow | `app/runtime.py`, `agent/` |  |
| Providers (LLM abstraction) | `providers/` | [providers.md](../docs/providers.md) |
| Tool registry & trust tiers | `tools/registry.py`, `trust/` | [tools.md](../docs/tools.md) |
| Operator plugins | `app/plugins.py`, deployment-owned packages | [plugins.md](../docs/plugins.md) |
| Application modules | `app/modules.py`, separately installed packages | [modules.md](../docs/modules.md) |
| Managed thread handoff | `tools/threads.py`, `app/threads.py` | [thread-handoff.md](../docs/thread-handoff.md) |
| Running a dev instance | `.env.dev` + `ENV_FILE` | [development.md](../docs/development.md) |
| Skills (instructions + scripts) | `skills/` | [skills/README.md](skills/README.md) |
| Memory (Hindsight) | `memory/` | [memory.md](../docs/memory.md) |
| Learning (teach + bot-name-derived menu) | `tools/learn.py`, `app/learn_turn.py`, `commands/learn_cmd.py` | [learning.md](../docs/learning.md) |
| Workspaces & file tools | `workspace/manager.py`, `tools/workspace/` | [workspace.md](../docs/workspace.md) |
| Durable coding agent | `app/coding_tasks.py`, `storage/coding_tasks.py`, `tools/coding_tasks.py` | [coding-agent.md](../docs/coding-agent.md) |
| Video understanding | `video_understanding/`, `tools/video.py`, `storage/video_sessions.py` | [video-understanding.md](../docs/video-understanding.md) |
| Persistent browser and visual rendering | `web_browser/`, `tools/browser.py`, `tools/visuals.py` | [browser.md](../docs/browser.md), [visual-rendering.md](../docs/visual-rendering.md) |
| Storage (async SQLite) | `storage/` | [database.md](../docs/database.md) |
| Privacy & content moderation | `app/consent.py`, `moderation/` | [privacy.md](../docs/privacy.md) |
| User persona overrides | `tools/persona.py` | [persona.md](../docs/persona.md) |
| Observability | `observability/` | [observability.md](../docs/observability.md) |
| Config (every setting) | `config/settings.py` | [configuration.md](../docs/configuration.md) |

## Quick start

Requirements: Python 3.14+, [uv](https://docs.astral.sh/uv/), a Discord bot
token, and an LLM API key.

```bash
git clone <repo-url> kimibot
cd kimibot/bot

cp .env.example .env
cp config/models.example.yaml config/models.yaml
# Create or restore the private skills/store directory when this instance uses skills.
# Edit config/models.yaml: endpoints, model IDs, context windows, capabilities,
# roles, fallbacks, and overrides. Add image routing only for verified models.
# Edit .env and set, at minimum:
#   DISCORD_BOT_TOKEN
#   ALLOWED_GUILD_IDS (or activate a guild in the private CONFIG_DIR)
#   the secret env var(s) your config/models.yaml references (e.g. MODEL_API_KEY)

uv run python bot.py
```

To run a second instance against a test guild without touching production
state, put its token and its own `DATABASE_PATH` in `.env.dev` and boot with
`ENV_FILE=.env.dev uv run python bot.py`. See
[development.md](../docs/development.md).

Optional integrations and behaviors are gated by config. Hindsight memory needs
its backend; Discord text search, moderation, and thread handoff each have their
own settings. [docs/configuration.md](../docs/configuration.md) documents every
setting. [docs/instance-data.md](../docs/instance-data.md) defines what belongs in
the public repository and what must remain private deployment state.

> Use `uv run` for everything. A bare `python`/`pytest` runs against an
> interpreter missing `hindsight-client` and fails with spurious collection
> errors; `uv run` uses the locked environment.

## Development

```bash
uv run python -m pytest          # run the test suite (~150 test files)
uv --preview-features audit-command audit --locked  # dependency vulnerabilities
uv run ruff check .              # lint
uv run ruff format --check .     # formatting (owns the 100-col line length)
uv run mypy .                    # type check
uv run python -m compileall .    # quick syntax check
```

Dependencies are declared in `pyproject.toml` and locked in `uv.lock`. Dev
extras come from `uv sync --extra dev` (what CI runs); `uv pip install -e ".[dev]"`
also works for an editable install.

## Project layout

```
app/               composition root + Discord runtime
discord_adapter/   the Discord boundary: io, gateway, background sweepers
agent/             turn handling, ReAct core, context, compaction
providers/         provider-neutral LLM layer (OpenAI, Anthropic, OpenRouter, Codex, …)
tools/             core + searchable tools (memory, workspaces, threads, Discord search, …)
workspace/         per-user sandboxed file workspaces (stdlib-only)
skills/            skill loading, registration, sandboxed runner, and the private-store guide
memory/            Hindsight client and per-user banks
storage/           async SQLite core schema plus per-module migration ledger
moderation/        content-moderation service and backends
observability/     structured JSONL tool/turn/moderation event logging
usage/             token-usage normalization and pricing
codex/             Codex WebSocket Responses transport
config/            settings, persona, prompt templates
config/fragments/  operator markdown read fresh each turn (pins, denylists, per-tool config)
commands/          memory, models, moderation, privacy, and usage commands plus the bot-name-derived teaching menu
trust/             trust-tier resolution
evals/             offline eval harness (cassette-replayed runs over the real core)
utils/             small shared helpers
deploy/            optional service deployments (Hindsight compose stack)
scripts/           operator/maintenance scripts
../docs/           project and per-subsystem documentation
tests/             test suite
```

Project-level context lives in
[`../docs/architecture.md`](../docs/architecture.md). [`../CLAUDE.md`](../CLAUDE.md) is the
developer map for this directory.

## License

[MIT](../LICENSE).
