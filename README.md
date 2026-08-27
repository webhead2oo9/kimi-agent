# Kimi

> A self-hosted Discord assistant with a provider-neutral tool loop.

![Python](https://img.shields.io/badge/python-3.14+-blue.svg)
![discord.py](https://img.shields.io/badge/discord.py-2.7+-5865F2.svg)
![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)

You run Kimi on your own host, against your own LLM provider: OpenAI-compatible,
Anthropic, OpenRouter, or Codex, swapped in `config/models.yaml` without touching
the agent core.

It responds to an @mention, a pinged reply, a "hey Kimi" in chat, or a message in
a thread it opened, and to nothing else. Each invocation runs a
[ReAct](https://arxiv.org/abs/2210.03629) tool loop, replying with Discord-safe
chunking, embeds, and attachments.

Persona, rules, and subject matter come from per-server and per-channel
instruction fragments rather than from the code, so one deployment can serve
several communities without forking the application.

## What it can do

| Capability | In short |
|---|---|
| **Multiple providers** | Route chat, compaction, and optional durable coding models via `config/models.yaml`: OpenAI-compatible, Anthropic, OpenRouter, or Codex. The agent core never sees provider types. |
| **Trust-tiered tools** | `MEMBER < REGULAR < STAFF`, resolved from Discord roles and enforced at dispatch, not by prompt wording. |
| **Long-term memory** | Optional per-user memory banks via [Hindsight](https://github.com/vectorize-io/hindsight), with recall, reflection, opt-out, and staff-taught community knowledge. |
| **Per-user workspaces** | File read/write/edit, archive extraction, document-to-text, and URL fetch, with path, size, quota, and TTL caps enforced by the application. |
| **Durable coding agent** | Optionally hand large repository work to a separately routed background worker that persists progress, runs managed sandbox jobs, recovers after restarts, and can be steered or cancelled. |
| **Video understanding** | Optionally ask a stateful Gemini 3.7 Flash specialist about public YouTube videos or uploaded Discord/workspace clips, with rooted follow-ups and timestamped evidence. |
| **Persistent browser** | Optional per-user BetterWright profiles for interactive web tasks, isolated with Bubblewrap/systemd/seccomp and routed over the host network or a fixed VPN namespace. |
| **Visual rendering** | A searchable call renders accessible fixed-style charts or constrained Mermaid diagrams as PNG attachments through an ephemeral offline browser worker. |
| **Skills** | Staff-managed Markdown instruction docs plus operator-authored script tools that run under mandatory Linux isolation with networking denied by default. |
| **Managed threads** | Hand a conversation into a bot-owned thread and keep the transcript intact across the move. |
| **Discord context** | Pull recent channel history on demand and, when enabled, search selected channels without persisting what it reads. |
| **Safety and moderation** | Privacy consent, LLM content moderation, user blocks, moderation cases and staff logs, and known-bad image fingerprint matching. |
| **Operator plugins** | Add community-specific tools from your own packages through an explicit allowlist. |

Foreground chat turns are stateless, and each conversation is keyed to the
message that started it and persisted in SQLite, so a reply continues its own
thread even across restarts. The optional video specialist is a deliberate
actor-scoped stateful tool behind the rooted conversation. Optional subsystems stay
off until configured.

## Repository layout

```
bot/     the application (source, config templates, tests, deployment files)
docs/    the project documentation (setup, architecture, per-subsystem reference)
```

All commands run from `bot/`, and CI (`.github/workflows/ci.yml`) audits locked
dependencies and runs lint, types, and the test suite from there too.

## Quick start

You'll need Python 3.14+, [uv](https://docs.astral.sh/uv/), a Discord bot
token, and an API key for at least one LLM provider.

```bash
cd bot
uv sync
cp .env.example .env
cp config/models.example.yaml config/models.yaml
```

Then fill in two files:

- **`.env`**: set `DISCORD_BOT_TOKEN`, `ALLOWED_GUILD_IDS`, and whichever
  API-key variable(s) your `models.yaml` references (e.g. `MODEL_API_KEY`).
- **`config/models.yaml`**: replace every placeholder endpoint and model ID,
  and set accurate context windows, capabilities, roles, and fallbacks.

```bash
uv run python bot.py
```

Always use `uv run`; a bare `python` or `pytest` picks up an interpreter
without the locked dependencies. [docs/setup.md](docs/setup.md) walks a first
deployment end to end, and [docs/development.md](docs/development.md) shows how
to run a second instance against a test guild without touching production
state.

## Where to go next

| I want to… | Read |
|---|---|
| See the full feature list and architecture diagram | [`bot/README.md`](bot/README.md) |
| Deploy for the first time | [`docs/setup.md`](docs/setup.md) |
| Understand the system shape | [`docs/architecture.md`](docs/architecture.md) |
| Look up a setting | [`docs/configuration.md`](docs/configuration.md) |
| Know what's public source vs. private instance data | [`docs/instance-data.md`](docs/instance-data.md) |
| Browse every doc | [`docs/README.md`](docs/README.md) |
| Contribute code | [`CLAUDE.md`](CLAUDE.md), the developer map |

## License

[MIT](LICENSE). Copyright (c) 2026 Webhead.
