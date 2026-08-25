# Architecture

Project-level map: what the system is made of and where each piece lives.

## What this bot is

Two model-driven surfaces over a provider-neutral LLM layer, plus a seam for
optional application modules:

**Mention-triggered conversational agent.** This is the community's chat
surface. A guild message that mentions the bot (or a handoff thread) enters a
provider-neutral ReAct loop with transcript-backed context, trust-gated tool
dispatch, and Hindsight memory. There is no conversational *slash* command;
staff/mod commands are ordinary app commands.

**"Teach Kimi" message context menu** (name follows `BOT_NAME`;
`commands/learn_cmd.py` → `app/learn_turn.py`). A staff-only scoped turn over the
same ReAct core on an independent registry limited to `LEARN_TOOLS`, answered
ephemerally and never persisted to a transcript; audited by `app/learn_log.py`.

## Where things live (`bot/`)

```
bot.py                  entry: Settings -> build_app() -> run()
app/                    composition root + Discord-facing application glue:
                        runtime.py (build_app, on_ready boot, on_message),
                        turn_entry.py (turn admission -> ReAct wiring),
                        tools.py (tool/client wiring), modules.py (versioned
                        application modules), plugins.py (operator plugin
                        loader), consent.py,
                        admission.py, conversation_routing.py, threads.py +
                        thread_handoff_boundary.py, memory.py, moderation.py,
                        providers.py,
                        tool_surfaces.py, learn_turn.py + learn_log.py
agent/                  ReAct engine, turn prep, compaction, attachments
discord_adapter/        the Discord boundary: io (send/receive gates, chunking),
                        gateway (live channel/member reads), lifecycle (sweepers)
providers/              neutral LLM interface, provider profiles, failover
search/                 provider-neutral internet search chain + Exa/Brave clients
config/                 pydantic-settings, models.yaml routing, operator overlay
config/fragments/       operator markdown read fresh each turn: guild/channel
                        pins and denylists, per-tool config, prompt templates
tools/                  registry, browse-tools activation, workspace + memory +
                        community-knowledge + internet/Discord search + persona
                        tools + optional code-execution/browser surfaces
web_browser/            BetterWright JSON bridge and isolated per-user worker
                        lifecycle
workspace/              per-user sandboxed file workspaces (stdlib-only; the
                        runtime data tree at workspaces/ is created on demand)
sandbox/                Linux code-execution boundary: Bubblewrap/systemd-run,
                        seccomp policy, network-namespace lease, resource limits
commands/               staff/user app commands (/privacy, /memory, /usage,
                        /models, ...) and the "Teach Kimi" context menu
memory/                 Hindsight client, bank scoping, auto-retain, opt-out
storage/                SQLite WAL core schema v1, module schema ledger,
                        LLM/paid-tool usage stores, global model selection
trust/                  trust-tier resolution from Discord roles + allowlists
moderation/             content safety: LLM input/output screening, tier
                        exemptions, pluggable backend (shipped and wired)
observability/          versioned JSONL turn/tool/moderation events
usage/                  LLM token/cost normalization and pricing
codex/                  Codex OAuth + WebSocket transport
utils/                  leaf helpers: frontmatter, atomic writes, formatting
evals/                  deterministic stub-surface harness + scenario set
skills/                 merged read-only built-ins + private shared-skill store,
                        executable-skill registration and sandbox runner;
                        live skills/store content is instance data
tests/                  test suite (incl. architecture-boundary guards)
deploy/                 optional service deployments, BetterWright installer,
                        and generic network-namespace provisioning templates
scripts/                operator/maintenance scripts
```

## Structural decisions

- **Composition root for the core.** Runtime, config, providers, storage, and
  trust wire up in one place (`app/runtime.py:build_app()`); deployment-specific
  tools arrive through the best-effort plugin seam (`app/plugins.py`). Required
  commands, listeners, schema, background jobs, and optional LLM tools arrive
  through the fail-fast application-module seam (`app/modules.py`).
- **Guild-scoped by construction.** `guild_id` rides every schema and the
  per-guild seams are real: guild activation is explicit and fail-closed,
  trust/pins/denylists/prompts layer per guild from `config/servers/<id>.md`,
  workspaces are keyed per (user, guild), community memory banks are per guild,
  and tools/skills can be guild-scoped. One deployment can serve several
  communities without sharing their data surfaces.
- **File-based operator config.** `config/models.yaml` and the `settings.md`
  overlay are validated once at startup. Prompt/policy frontmatter fragments
  are read fresh for each turn, so those targeted edits do not require an admin
  console or restart.
- **Fail-closed boundaries.** Tool dispatch gates on trust tier and denylists;
  output moderation fail-closes before Discord delivery; configured modules
  fail startup if absent or incompatible; the Hindsight
  gate degrades the bot cleanly instead of erroring.

## Deeper reading

- `../bot/README.md`: bot-level feature/architecture summary.
- `configuration.md`: complete configuration reference.
- `tools.md`: complete built-in tool catalog and availability gates.
- `code-exec.md`: code-execution modes, threat model, deployment, and verification.
- `internet-search.md`: Exa/Brave search behavior, output, per-turn budget, and cost accounting.
