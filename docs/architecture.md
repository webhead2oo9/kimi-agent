# Architecture

This is the project-level map: what the system is made of and where each piece
lives.

## What this bot is

At its core, the bot has two foreground model-driven surfaces over a
provider-neutral LLM layer, an optional durable background coding surface, and
a seam for optional application modules:

**Mention-triggered conversational agent.** This is the community's chat
surface. A guild message that mentions the bot (or lands in a handoff thread)
enters a provider-neutral ReAct loop with transcript-backed context, trust-gated
tool dispatch, and Hindsight memory. There is no conversational *slash*
command; the staff and moderation commands are ordinary app commands.

**"Teach Kimi" message context menu** (the name follows `BOT_NAME`;
`commands/learn_cmd.py` → `app/learn_turn.py`). This is a staff-only scoped
turn over the same ReAct core, running on an independent registry limited to
`LEARN_TOOLS`. It's answered ephemerally, never persisted to a transcript, and
audited by `app/learn_log.py`.

**Durable coding agent (optional).** Large repository tasks leave the foreground
Discord turn through `start_coding_task` and continue in a bounded background
worker. The worker resolves the independent `roles.coding` model chain, stores
task, checkpoint, event, and managed-job state in SQLite, and reports progress
and completion back to Discord. It registers only when explicitly enabled and
both its tool-capable model role and the Linux code sandbox are available; it
never falls back to `roles.chat`. See [Durable coding agent](coding-agent.md).

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
                        providers.py, coding_tasks.py + coding_jobs.py,
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
                        tools + optional code-execution/browser/visual/coding-task
                        surfaces
web_browser/            BetterWright persistent-browser bridge and isolated
                        per-user worker lifecycle, plus the fixed-code ephemeral
                        offline chart/Mermaid renderer
workspace/              per-user sandboxed file workspaces (stdlib-only; the
                        runtime data tree at workspaces/ is created on demand)
sandbox/                Linux code-execution boundary: Bubblewrap/systemd-run,
                        seccomp policy, network-namespace lease, resource limits
commands/               staff/user app commands (/privacy, /memory, /usage,
                        /models, ...) and the "Teach Kimi" context menu
memory/                 Hindsight client, bank scoping, auto-retain, opt-out
storage/                SQLite WAL core schema v2, module schema ledger,
                        conversation + coding task/event/job state,
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

A few decisions shape everything above, so they're worth stating explicitly.

- **Composition root for the core.** Runtime, config, providers, storage, and
  trust all wire up in one place (`app/runtime.py:build_app()`).
  Deployment-specific tools arrive through the best-effort plugin seam
  (`app/plugins.py`), while required commands, listeners, schema, background
  jobs, and optional LLM tools arrive through the fail-fast application-module
  seam (`app/modules.py`).
- **Guild-scoped by construction.** `guild_id` rides every schema, and the
  per-guild seams are real: guild activation is explicit and fail-closed;
  trust, pins, denylists, and prompts layer per guild from
  `config/servers/<id>.md`; workspaces are keyed per (user, guild); community
  memory banks are per guild; and tools and skills can be guild-scoped. The
  result is that one deployment can serve several communities without sharing
  their data surfaces.
- **File-based operator config.** `config/models.yaml` and the `settings.md`
  overlay are validated once at startup. Prompt and policy frontmatter
  fragments are read fresh for each turn, so those targeted edits need neither
  an admin console nor a restart.
- **Fail-closed boundaries.** Tool dispatch gates on trust tier and denylists;
  output moderation fails closed before Discord delivery; configured modules
  fail startup if they're absent or incompatible. The exception is the
  Hindsight gate, which degrades the bot cleanly instead of erroring.

## Deeper reading

Once you have the map, these are the places to go next:

- `../bot/README.md`: bot-level feature/architecture summary.
- `configuration.md`: complete configuration reference.
- `tools.md`: complete built-in tool catalog and availability gates.
- `code-exec.md`: code-execution modes, threat model, deployment, and verification.
- `visual-rendering.md`: one-call charts/Mermaid, offline rendering, and deployment.
- `coding-agent.md`: durable coding lifecycle, model routing, recovery, and cancellation.
- `internet-search.md`: Exa/Brave search behavior, output, per-turn budget, and cost accounting.
