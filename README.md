# Kimi

> A Discord assistant built for you and your communities.

![Python](https://img.shields.io/badge/python-3.14+-blue.svg)
![discord.py](https://img.shields.io/badge/discord.py-2.7+-5865F2.svg)
![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)

> **Name and affiliation:** This project is an independent open-source Discord
> assistant. It is not affiliated with, endorsed by, or sponsored by Moonshot AI
> or its Kimi products and language models. “Kimi” is simply the name of this bot,
> and the software can be configured to use many different model providers.

Kimi is a bot for communities that want an AI helper without reinventing the wheel.
You point it at whatever LLM you like (OpenAI-compatible,
Anthropic, OpenRouter, Codex, and more providers to come) in one YAML file, give each server or channel its
own persona and rules in plain Markdown, and it takes it from there.

It only speaks when spoken to: an @mention, a pinged reply, a `hey Kimi` or
`Kimi help`, or a message inside a thread it's running. Ordinary DMs are
ignored by default. Operators can optionally let approved users continue their
personal chat in DMs or through the user-installed `/chat` command. Under the
hood, every invocation runs a
[ReAct](https://arxiv.org/abs/2210.03629) tool loop and comes back with a
properly chunked Discord reply, embeds and attachments included.

Roughly what that looks like:

> **@you:** hey Kimi, what was that Rust book you recommended me a while back?
>
> **Kimi:** Rust for Rustaceans, back in March. You said you were going to start it after finishing the async chapter of the Book. Did you?  

## What it can do

Plain chat works out of the box. There's lots of options to look through and setup, so make sure you read through the documentation, or have your agent read through it.

| | |
|---|---|
| **Any provider** | Chat, compaction, and an optional background coding model each get their own route in `config/models.yaml`, with fallbacks. The agent core never learns which vendor it's talking to. |
| **Trust tiers** | `MEMBER < REGULAR < STAFF`, taken from Discord roles. Who can use which tool is enforced in code at dispatch time, not by asking the model nicely. |
| **Memory** | Per-user long-term memory via [Hindsight](https://github.com/vectorize-io/hindsight): recall, reflection, opt-out, and staff-taught community knowledge. |
| **Workspaces** | Each user gets a sandboxed folder: read, write, edit, unzip, pull text out of documents, fetch URLs. Sizes, quotas, and TTLs are capped by the app. |
| **Coding agent** | Hand bigger repo jobs to a background worker that keeps its own progress, runs sandboxed jobs, survives restarts, and can be steered or cancelled. |
| **Video** | Ask about a YouTube link or an uploaded clip and get timestamped answers, with follow-up questions in the same conversation. |
| **Browser** | Per-user persistent browser profiles for real web tasks, locked down with Bubblewrap/systemd/seccomp and optionally routed through a VPN namespace. |
| **Charts and diagrams** | Render accessible charts or Mermaid diagrams to PNG through an ephemeral, offline browser worker. |
| **Skills** | Staff-written Markdown playbooks, plus operator-authored script tools that run under mandatory Linux isolation with no network unless you say so. |
| **Threads** | Move a conversation into a bot-owned thread without losing the transcript. |
| **Discord context** | Pull recent channel history on demand, or search chosen channels, without persisting what it reads. |
| **Safety** | Privacy consent, LLM output moderation, user blocks, trust-tiered tools, and strict workspace/network boundaries. |
| **Extensions** | Add deployment-owned tools with plugins, or attach any installed lifecycle module through a stable standalone API. |
| **Personal user app** | Optionally grant selected Discord IDs one `/chat` thread and workspace that follows them across locations. |

Turns are stateless and every conversation is keyed to the message that started
it, stored in SQLite, so replying to an old answer picks the thread back up even
after a restart.

## Repository layout

```
bot/     the application (source, config templates, tests, deployment files)
docs/    the project documentation (setup, architecture, per-subsystem reference)
```

Everything runs from `bot/`, CI included (`.github/workflows/ci.yml` audits
locked dependencies and runs lint, types, and tests from there).

## Quick start

You'll need Python 3.14+, [uv](https://docs.astral.sh/uv/), a Discord bot token,
and one LLM provider you can reach. Most want an API key; Codex uses a token file,
and some gateways inject credentials upstream.

```bash
cd bot
uv sync
cp .env.example .env
cp config/models.example.yaml config/models.yaml
```

Then fill in two files:

- **`.env`**: `DISCORD_BOT_TOKEN`, `ALLOWED_GUILD_IDS`, and whatever credential
  variables your `models.yaml` references (for example `MODEL_API_KEY`).
- **`config/models.yaml`**: swap out every placeholder endpoint and model ID, and
  be honest about context windows, capabilities, roles, and fallbacks.

```bash
uv run python bot.py
```

Always go through `uv run`; a bare `python` or `pytest` finds an interpreter
without the locked dependencies and fails in confusing ways.
[docs/setup.md](docs/setup.md) walks a first deployment end to end, and
[docs/development.md](docs/development.md) shows how to run a second instance
against a test guild without touching production state.

## Where to go next

| I want to… | Read |
|---|---|
| See the full feature list and architecture diagram | [`bot/README.md`](bot/README.md) |
| Deploy for the first time | [`docs/setup.md`](docs/setup.md) |
| Understand the system shape | [`docs/architecture.md`](docs/architecture.md) |
| Look up a setting | [`docs/configuration.md`](docs/configuration.md) |
| Configure user-installed personal chat | [`docs/user-app.md`](docs/user-app.md) |
| Develop or install an application module | [`docs/modules.md`](docs/modules.md), [`bot/modules/example`](bot/modules/example/README.md) |
| Know what's public source vs. private instance data | [`docs/instance-data.md`](docs/instance-data.md) |
| Browse every doc | [`docs/README.md`](docs/README.md) |
| Contribute code | [`CLAUDE.md`](CLAUDE.md), the developer map |

## License

[MIT](LICENSE). Copyright (c) 2026 Webhead.
