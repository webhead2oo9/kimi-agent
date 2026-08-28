# CLAUDE.md

Developer map for the Kimi Discord bot. The application lives entirely in `bot/`; `docs/` holds the canonical documentation set. This is the single file to extend. Code paths below are relative to `bot/` unless written otherwise.

## What This Is

A Python 3.14 Discord bot: a generalist assistant for Discord communities, named Kimi by default. The name comes from `BOT_NAME`/`settings.bot_name`; never hardcode it. The bot responds only when actually invoked: an @mention, a reply with the reply ping on (ping off is ignored), a `hey/hi <bot_name>` or `<bot_name> help` text invocation, or an unmentioned message inside an auto-responding thread it created via thread handoff. Paused managed threads use ordinary invocation rules. It ignores DMs unless `USER_APP_DM_ENABLED` is on, which routes a DM from an allowlisted user into personal chat. It runs a provider-neutral ReAct tool loop, and replies with Discord-safe chunking and attachments. Trust-tiered tools, script-backed skills, per-user workspaces, SQLite-backed conversation persistence, optional Hindsight memory.

This file is the map; per-subsystem behavior docs live in [`docs/`](docs/README.md) and [`docs/architecture.md`](docs/architecture.md) is the orientation-level tree. Design rationale belongs in a doc, not here. Two upkeep rules:

- When a change alters behavior described in a `docs/*.md`, update that doc in the same change. Docs describe current behavior only; keep an implementation plan only when an archival plan is explicitly requested.
- Release history belongs in tagged GitHub releases, not a rolling document in the repository. Put operator-facing changes and any required migration steps in the notes for the release that ships them.

The deployment host and process supervisor are deliberately not recorded here. Ask rather than inferring them, and never invent or carry over host paths, service units, or restart commands.

## Commands

Run every command from `bot/`, always through `uv run` (CI does the same with `working-directory: bot`). A bare `python`/`pytest`/`mypy` hits an interpreter missing `hindsight-client` and fails with a hundred-plus spurious collection errors.

```bash
uv run python bot.py                          # run the bot
ENV_FILE=.env.dev uv run python bot.py        # dev instance: test guild + own DB (docs/development.md)

uv --preview-features audit-command audit --locked  # locked dependency vulnerabilities
uv run ruff check .                           # lint
uv run ruff format --check .                  # formatting: the ONLY line-length enforcement (E501 is unselected)
uv run mypy .                                 # core types (Windows fallback: uv run python -m mypy .)
uv run mypy --config-file modules/example/pyproject.toml modules/example/src modules/example/tests  # example package types
uv run --all-packages python -m pytest -q     # all tests, including the reference module
uv run python -m pytest tests/test_core_smoke.py -k "test_name"
git diff --check                              # whitespace
uv run python -m pytest tests/test_docs_links.py -q   # after docs-only changes
```

Before handing off Python changes, run everything CI runs (`.github/workflows/ci.yml`): the locked dependency audit, ruff check, ruff format, mypy, pytest, plus `git diff --check`. Dependencies live in `pyproject.toml` and `uv.lock` only; CI syncs with `uv sync --locked --all-packages`, so re-run `uv lock` after any dependency change or CI fails. Dev extras: `uv sync --all-packages --extra dev`.

## Working Practices

- For reviews of uncommitted changes, inspect the live `git status` and diff before drawing conclusions. Keep read-only review tasks read-only unless the user explicitly switches to implementation.
- When asked for safe fixes after a review, add or update focused regression tests with the patch, rerun verification, and commit only after the requested green state is reached.
- Never start a second bot process against the production token or database; use the isolated workflow in [`docs/development.md`](docs/development.md).
- Keep secrets in ignored environment files or the deployment's secret store; never in tracked files or docs. Live model routing, deployment fragments, skill stores, databases, workspaces, logs, and auth files are private instance data. See [`docs/instance-data.md`](docs/instance-data.md).
- Keep repository-local paths and operational guidance scoped to this project. Unpublished project notes and private checkout paths are not public-repository artifacts.

## Conventions

**Boundaries (enforced by tests)**

- The package import graph is frozen in `tests/test_package_graph.py:_ALLOWED_EDGES`. A new cross-package import fails there until declared. Imports from `commands` to `app`, `memory` to `agent`, and `config` to `agent` are forbidden.
- `import discord` is confined to `discord_adapter/`, `app/`, and `commands/` (`tests/test_architecture_boundaries.py`). `tools/`, `workspace/`, and everything else are discord-free plain data and logic.
- `agent/core.py` is provider-agnostic: no provider imports. Provider-specific parsing stays under `providers/`; `providers/types.py` is the shared vocabulary (`ContentPart`, `ConversationMessage`, `ProviderRequest`, `ProviderResponse`, `ToolCall`, `GeneratedAsset`, `ProviderCapability`).
- `tools/registry.py:dispatch` is the privilege boundary. `min_tier`, `owner_only`, `guild_ids`, and the operator denylist are all re-checked there, and a tool the caller may not use is masked as `"Unknown tool"`, never refused, so existence never leaks. Never rely on prompt text for staff-only behavior.
- `modules/` implements the module API services (the standalone contract package is
  `packages/kimi-agent-module-api`; `modules/testing.py` is the core integration
  harness). The SDK must never import core runtime packages. `app/runtime.py:build_app`
  is the composition root; `bot.py` is only the entry point; tool/client wiring is
  `app/tools.py`.
- Fail-closed vs fail-open is principled: privilege gates and credential-gated registration fail closed; curation-only operator fragments (pins, channel/guild denylists, tool config) fail open to last-known-good; startup validation (models, deployment-wide tool policy, `settings.md`, executable-skill sandbox) aborts rather than degrades.

**Code style**

- New ordinary runtime modules use `from __future__ import annotations`; package markers and docstring-only modules may omit it.
- Prefer `@dataclass(frozen=True)` for internal value types, with `slots=True` where hot. Pydantic is confined to settings and configuration validation (`config/settings.py`, module settings specs); do not introduce `BaseModel` for ordinary domain types. Prefer `typing.Protocol` for injected seams; `LLMProvider` and `ModerationBackend` are the current ABCs.
- Use a module-level `logging.getLogger(__name__)` (named `log`; older modules use `logger`) with `%s`-style args for runtime logging (ruff `G`/`LOG` families). No `print()` in runtime code. Errors surfaced to Discord are concise and never contain tracebacks or secrets.
- Ruff's flake8-async rules are on: no blocking I/O inside `async def`. Line length is 100, owned by `ruff format`.
- mypy runs with `check_untyped_defs` on for runtime code (off for `tests/` and `evals/`) and `platform = "linux"`. `# type: ignore` and `# noqa` are rare in runtime code (about a dozen); `tests/` carries more, nearly all `Settings(...)  # type: ignore[call-arg]` against pydantic-settings' generated `__init__`. Always name the specific error code. Add a prose reason as well when the code alone does not explain the suppression: `import yaml  # type: ignore[import-untyped]` speaks for itself, a narrowing or a sentinel assignment does not.
- Comments explain *why*, not what. Keep the existing module layout; prefer small focused changes.
- Commit subjects are imperative present tense with no prefix ("Reject root for executable skill tools").
- This repo has production data: remove dead config, stale compatibility paths, and TODO scaffolding rather than letting them become patterns. Keep the schema source, tests, and docs aligned with the current baseline.

**Tests**

- Async tests need an explicit `@pytest.mark.asyncio`; there is no `asyncio_mode = auto`, so a missing marker is a silent failure.
- Prefer `monkeypatch` and hand-written `Fake*`/`Stub*` classes over `unittest.mock`. Skills tests build stores under `tmp_path`; never depend on the private `skills/store/`.
- `tests/conftest.py` has an autouse `_reset_tool_surfaces` fixture; `tests/helpers.py` exposes the project root path.
- Behavioral changes ship with a focused regression test. `tests/test_model_config.py` resolves against `config/models.example.yaml`, so keep that template valid.

**Settings and tools**

- New deployment settings go in `config/settings.py` **and** `.env.example`. Safe per-call behavior owned by one tool belongs in its registry `config_spec`, not in `Settings`.
- Add a gerund-phrase entry to `_TOOL_LABELS` in `agent/activity.py` for every new tool; a missing entry falls back to a title-cased raw name.
- `tests/test_docs_links.py` checks every relative link in the docs (including this file) and every backticked ALL-CAPS token against real settings and symbols.

## Architecture

### Message Flow

`KimiApplication.on_message` (`app/runtime.py`) → eligibility and DM rejection → invocation gate (`discord_adapter.io.should_respond`) → user block and stop-message gates → send-permission and admission gates (`app/admission.py`) → per-user privacy lease → consent (`app/consent.py`) → rooted conversation resolution (`app/conversation_routing.py`) → root lock and invocation re-check → `KimiApplication.handle_message` → turn wiring (`app/turn_entry.py`) → `agent/turn.py:handle_turn` (`prepare_turn`, `execute_turn`) → `agent/core.py:run_conversation` (ReAct loop) → provider `run_turn` → tool dispatch via `tools/registry.py` → `discord_adapter.io.send_response`.

Turns are stateless: no provider continuation, no in-process context cache. Each fresh mention creates a rooted conversation keyed by the triggering message id; `message_contexts` maps both user trigger ids and bot reply ids back to that root so replies continue the same transcript across restarts. `agent/context.py` builds a fresh `ConversationContext` each turn from persisted SQLite rows; reply chunks persist back through `ConversationStore.save_channel_messages`, deduped by Discord message id. The response lock is per root: different roots run in parallel, concurrent replies to one root serialize. `on_message` enforces user blocks before reactions, transcript writes, locks, tools, or provider calls; `block_user` is a member-tier self-block only, staff block others via `/moderation`.

When a tool-heavy turn approaches the model window, `agent/compaction.py:Compactor` summarizes the oldest in-loop iterations into one untrusted progress note (summarizer = `roles.compaction`; fallbacks are tool-body elision then hard truncation). A live `plan` checklist survives compaction, and after the first pass the triggering request is restated as the final message. Only the in-flight request is compacted, never the persisted transcript. See [`docs/compaction.md`](docs/compaction.md).

### Providers

`providers/base.py:LLMProvider` is the abstract interface; every provider normalizes into `ProviderResponse`/`ToolCall`. Supported profile types (`providers/factory.py:SUPPORTED_PROVIDER_NAMES`; if it isn't wired in the factory it isn't supported): `openai_compat` (Chat Completions, default), `openai_responses` (stateless Responses API, `store=false`), `anthropic` (native SDK, api.anthropic.com only), `anthropic_compat` (Messages-over-HTTP for gateways), `openrouter`, `codex` (WebSocket Responses transport in `codex/`).

`config/models.yaml` is **untracked instance state**; the tracked template is `config/models.example.yaml` and a missing live file fails startup rather than falling back. Roles `chat` and `compaction` are required; `chat_images`, `persona`, and the independent durable-worker role `coding` are optional. `coding` never inherits from `chat`, and its primary and `coding_fallbacks` must declare `text` and `tool_calling`. Each model declares a conservative `context_window` and capabilities, and routing rejects a model that cannot satisfy the turn. Any role may declare `<role>_fallbacks`; `ProviderManager.resolve` returns a `providers/failover.py:FailoverProvider` for single and multi-model chains. Transient errors get one retry, fallback is forward-only and sticky within a logical turn, and SQLite-backed circuits skip unhealthy model/account scopes across turns and restarts. Generic policy lives in `providers/failure_policy.py`; optional adapters normalize provider-specific structured errors. `openai_compat` streams with a stall watchdog (`PROVIDER_STREAM_STALL_TIMEOUT_SECONDS`, 90s); the whole-turn ceiling is `REACT_TURN_TIMEOUT_SECONDS` (3600s). The owner-only `/models` menu selects among up to 120 `selectable_chat_models`, shows circuits, and can reset all cooldowns; catalog edits still need a restart. See [`docs/providers.md`](docs/providers.md) and [`docs/provider-resilience.md`](docs/provider-resilience.md).

New provider: subclass `LLMProvider`, implement `run_turn`, declare `capabilities`, add a factory case and a `SUPPORTED_PROVIDER_NAMES` entry, document in `docs/providers.md` and the models template.

### Trust Tiers

`MEMBER` < `REGULAR` < `STAFF`, resolved in `trust/resolver.py` from Discord roles and the `STAFF_*`/`REGULAR_*` allowlists, layered **additively** with per-guild trust from `config/servers/<guild_id>.md` frontmatter (`config/fragments/guild_config.py:load_guild_trust`, read fresh each turn). A guild can grant standing but never strip someone the global config trusts. Every `resolve()` call site threads `guild_id`.

User-installed personal chat (`/chat`) resolves tier from the `USER_APP_*` ID allowlists in `trust/user_app.py` instead, independently of guild roles. Because that standing exists outside every guild, personal chat is **guild-less for every trust, policy, and data-scope decision**: `MessageContext.guild_id` is the logical scope and is `None` there, so guild-scoped tools are not dispatchable and guild artifacts are not reachable. The physical invocation location rides separately on `platform_guild_id`, carries no authority, and is barred from `tools/` by `tests/test_architecture_boundaries.py`. See [`docs/user-app.md`](docs/user-app.md).

### Tool Registry

`tools/registry.py:ToolRegistry` holds **core tools** (always visible to qualifying tiers) and **search tools** (hidden until `browse_tools` activates them; activation persists per root in `conversation_activated_tools`). Skill-backed tools carry `skill_name` for bulk replacement on reload. Per-tool scoping, all AND-ed and all re-checked at dispatch:

- `guild_ids` (a `frozenset`) scopes a tool *to* guilds: `None` = everywhere, a set = only those, empty = nowhere; DMs and personal chat never match. Use `is_registered` (not `has_tool`) for existence checks that must ignore scope.
- A tool whose target is a guild or deployment artifact must refuse a guild-less caller rather than widen: `None` guild means "no guild", never "all guilds". `app/turn_entry.py:_PERSONAL_CHAT_BLOCKED_TOOLS` hides community memory and shared skill management from personal chat, and `tools/skills.py` refuses again at the handler as the fail-closed second layer.
- `pinned_tools:` frontmatter pre-activates searchable tools (guild fragment as base, channel fragment unions on), read fresh each turn and never persisted. Pinning never widens privileges.
- `blocked_tools:` frontmatter is the denylist at three scopes: `config/tools.md` (deployment-wide, validated at `build_app`, last-known-good on bad reload), guild, and channel. `app/turn_entry.py` unions them and stashes the result on `ctx.blocked_tools`. The only way to *subtract* a global tool from a guild or channel; the denylist beats a pin.
- `config_spec` (`tools/config_spec.py:ToolConfigField`) declares typed per-tool operator config, read from `<CONFIG_DIR>/tools/<tool_name>.md` fresh each turn by `config/fragments/tool_config.py` and resolved through `tools/config_spec.py:resolve_config`. Handlers read `ctx.tool_configs.get("<tool>") or {}` and never merge defaults themselves. Credential, endpoint, and path names are rejected as config fields.

See [`docs/tools.md`](docs/tools.md) and [`docs/configuration.md`](docs/configuration.md).

### Skills

Shared discovery merges code-owned, read-only `skills/builtin/<name>/SKILL.md` documents with the private `<SKILLS_DIR>/<name>/SKILL.md` store. Built-ins are global, instruction/reference-only, startup-validated, and reserve their names; private collisions are ignored without deletion. The private store defaults to ignored `skills/store/`, may be absent, and may additionally declare executable tools (`tools:` with a `script:`). Loading and catalog behavior is in `skills/loader.py`, executable registration in `skills/registration.py`, and execution in `skills/runner.py`. Executable tools remain private-store-only, Linux-only, and fail closed without the Bubblewrap boundary. Staff manage only private instruction skills with `skill_create`/`skill_edit`; `reference/` files from either source are exposed through `skill_file` after `load_skill`. Personal skills (`data/personal_skills/<user_id>/`) are per-user, instruction-only, never executable. See [`bot/skills/README.md`](bot/skills/README.md) and [`docs/personal-skills.md`](docs/personal-skills.md).

### Workspace Tools

`workspace/manager.py:WorkspaceManager` gives each **(user, guild)** a sandboxed directory keyed `workspace_owner_key(user_id, guild_id)` = `<user_id>__<guild_id>` (`__dm` without a guild), surfaced as `MessageContext.workspace_key`; `ctx.user_id` stays the real Discord id for everything non-workspace. `tools/workspace/` exposes read/write/edit/`multi_edit`/move/glob/grep, `view_image`, `import_attachment`, `extract_document_text`, `zip`, `extract_archive`, and URL fetch, all bounded by caps in `config/settings.py`. Every model-supplied path goes through `WorkspaceManager.resolve_user_file_path` (rejects absolute/`..`/symlink), writes re-check under a per-workspace lock, and the sweeper in `discord_adapter/lifecycle.py` enforces TTL/quota. Non-image attachments are ephemeral turn context until `import_attachment`. The `plan` tool keeps an in-turn checklist on `MessageContext.plan` that never enters the transcript. See [`docs/workspace.md`](docs/workspace.md).

### Code Execution

Optional Linux-only `run_code` (`tools/code_exec.py`, `MEMBER`) executes inline Python/shell or workspace files through `sandbox/runner.py`: systemd whole-tree cgroups, Bubblewrap namespaces/mounts, libseccomp deny-list, rlimits, capped tmpfs/output/workspace growth, and a hard core-dump boundary. `CODE_EXEC_NETWORK_MODE` is deployment-wide: `none`, explicit-risk host networking, or an operator-provisioned `netns` entered through a root-owned helper that accepts no namespace selector. Enabling never bypasses `sandbox_available()`; a failed full-profile probe leaves the tool unregistered. Runs hold the same per-workspace lock as file mutations. Persistent `.venv`/`.pio` trees have separate quotas and fd-relative no-follow cleanup. See [`docs/code-exec.md`](docs/code-exec.md).

Optional durable coding tasks (`app/coding_tasks.py`, `storage/coding_tasks.py`, `tools/coding_tasks.py`) split repository work from the foreground Discord turn. Registration requires `CODING_TASKS_ENABLED`, a tool-capable `roles.coding`, and the live code sandbox. SQLite holds task/event/job state plus the durable task context and input metadata; startup recovers nonterminal tasks and marks uncertain jobs interrupted. Workers are globally bounded, serialize writes per workspace, checkpoint after tool batches, and expose only workspace, plan/progress, and managed-job tools. `/stop` and exact bot-directed `stop|cancel|abort` bypass admission to cancel foreground and background work; `/privacy` invokes the same teardown before deletion. See [`docs/coding-agent.md`](docs/coding-agent.md).

### Persistent Browser

Optional Linux-only `browser` (`tools/browser.py`, `MEMBER`) runs BetterWright through `web_browser/bridge.mjs` and the Bubblewrap/systemd/seccomp worker in `web_browser/service.py`. Profiles are per user and hashed-named, one worker at a time; screenshots reach generated workspaces only after containment, type, and size checks. The external runtime is pinned by `deploy/betterwright/install.sh`. Both `host` and fail-closed VPN `netns` modes exist, and netns shares the physical `NetnsLease` with networked code execution, so the two surfaces never hold it at once. The bridge disables the credential vault, downloads, live view, loopback, and private-network access; the daemon and cloud providers are never reached. See [`docs/browser.md`](docs/browser.md).

The same `BROWSER_ENABLED` gate and locked runtime expose searchable `render_chart` and `render_diagram` (`tools/visuals.py`, `MEMBER`) when the exact Mermaid asset is present. One call renders a fixed-style structured chart or constrained Mermaid source through `web_browser/visual_service.py` → fixed-code `visual_bridge.mjs`, verifies one 1200×675 PNG, and queues it with the required Discord attachment description. Visual jobs are globally serialized, ephemeral, and always offline: no persistent profile, resolver/certificates, `--share-net`, browser VPN lease, or model-supplied code. See [`docs/visual-rendering.md`](docs/visual-rendering.md).

### Video Understanding

Optional searchable `video` (`tools/video.py`, `video_understanding/`) analyzes public YouTube URLs plus exact current-message Discord attachments and safe workspace videos through the fixed Google Gemini Files + Interactions APIs. Registration requires both `VIDEO_UNDERSTANDING_ENABLED` and the environment-only `GEMINI_API_KEY`; the tool uses stable `gemini-3.7-flash`, not a role from `config/models.yaml`. Uploaded videos stream in bounded chunks (500 MiB and one-hour hard ceilings; one upload at a time), never whole-file memory, and arbitrary external video URLs remain rejected. `start` creates an actor/guild/root-scoped stored Interaction and opaque local handle; `ask` continues it with `previous_interaction_id`. SQLite persists only safe source/session/provider identifiers and durable Interaction/File deletion outboxes, while structured timestamped answers return as untrusted context. Sessions expire within 24 hours; transcript retention and full `/privacy` deletion queue the complete known provider state for Google deletion. Safe per-call limits live in `config/tools/video.md`. See [`docs/video-understanding.md`](docs/video-understanding.md).

### Image Generation

Optional REGULAR-tier core `generate_image` (`tools/image_gen.py`, `image_gen/`) replaces
regex-inferred provider image output with an explicit model tool. OpenAI `gpt-image-2` is the
shipped backend: ChatGPT OAuth is primary through the process-wide cached
`CodexAuthManager`, with an environment-only platform API key as fallback. The chat provider
is independent of the image backend. Generation uses JSON in both modes; OAuth edits use
Codex JSON data URLs while platform edits use multipart `image[]` parts. Safe per-call knobs
live in `config/tools/generate_image.md`.

Reference paths are bounded PNG/JPEG/WebP workspace files resolved through
`WorkspaceManager.resolve_user_file_path`, read off-loop under the workspace activity lock,
and capped per-file and in aggregate. Verified PNG output is quota-checked, saved under the
user workspace's `generated_images/`, and queued through `enqueue_workspace_file` with a
required Discord attachment description. Generic queued-file moderation screens that
description, not the generated image bytes. `ProviderCapability.IMAGE_OUTPUT` and
`GeneratedAsset` remain provider-contract surfaces for direct provider requests; normal
Discord turns never infer them from message text. See [`docs/image-generation.md`](docs/image-generation.md).

### Discord Retrieval and Reply Composition

`get_channel_context` (backed by `discord_adapter/gateway.py`) returns untrusted live context outside the persisted conversation. Normal guild turns also resolve up to five same-guild Discord message links, channel links, `<#channel>` mentions, and cache-known bare channel ids into ephemeral `[Automated hint: ...]` context (`discord_adapter/reference_hints.py` → `agent/discord_references.py`). Message links require the requester and bot to view the channel and read its history; metadata-only channel references require view access, and both paths honor `DISCORD_SEARCH_EXCLUDED_CHANNELS` and private-thread membership. Personal chat never resolves guild references. `discord_text_search` (`tools/discord_text_search.py`, searchable, `MEMBER`) searches a fresh positive scope of channels both the requester and bot can read, minus the same exclusions; it is enabled by default behind `DISCORD_TEXT_SEARCH_ENABLED` and Message Content intent. Deployment-specific retrieval belongs behind the plugin seam with its own gate, trust scope, and untrusted framing.

`build_discord_embed` (`tools/embeds.py`) and the thread tools (`tools/threads.py`, gated on `THREAD_HANDOFF_ENABLED`) queue plain-data requests that ride the final reply through `MessageContext` → `ConversationContext` → `TurnResult` → the Discord boundary, which re-checks every gate before posting. Bot-created threads are enrolled in `thread_conversations`, share the rooted transcript and lock, and can auto-respond or pause. `@everyone`/`@here` and pings are hard-blocked at the send layer via `AllowedMentions`, not by prompt. See [`docs/embeds.md`](docs/embeds.md) and [`docs/thread-handoff.md`](docs/thread-handoff.md).

### Memory

Optional, backed by [Hindsight](https://github.com/vectorize-io/hindsight) when `HINDSIGHT_URL` is set. `memory/banks.py` manages per-user banks (`user:{discord_id}`), per-guild community banks, and the skills bank. User memory is default-on; `/memory opt-in|opt-out|status` are the controls. `prepare_turn` recalls only the current user's memories as ephemeral context, never persisted. Two user-bank write paths: explicit `remember_user_memory` (first-party facts, source-anchored, capped by `MEMORY_MAX_WRITES_PER_TURN`) and auto-retain (`memory/auto_retain.py`, `MEMORY_AUTO_RETAIN_ENABLED`, per-(conversation, user) watermarks). Retrieval: `recall_user`, `reflect_user`. Community memory is staff-led through `teach` (`tools/community.py`); conversations are never auto-extracted into it. Deletion lives on `/privacy` via `memory/privacy.py:forget_user_memory`. See [`docs/memory.md`](docs/memory.md).

### Learning

The staff gesture for adding shared knowledge: a **fact** goes to community memory via `teach`, a **procedure** to a skill via `skill_create`/`skill_edit`; there is no unified `learn` tool. Triggered by prompting in conversation or by the **Teach Kimi** context menu by default (its name follows `BOT_NAME`; `commands/learn_cmd.py` → `app/learn_turn.py:run_learn_turn`, a STAFF turn on a structurally narrowed `LEARN_TOOLS` registry, capped at the module constant `LEARN_TURN_TIMEOUT_SECONDS`). Both sinks emit a `LearnEvent` (`tools/learn.py`) rendered by `app/learn_log.py` to the guild's `learn_log_channel_id`. The quoted message is untrusted and a poisoned skill is persistent injection, an accepted risk that rests on STAFF being the only trigger. Mechanics and rationale: [`docs/learning.md`](docs/learning.md).

### Persona, Consent, Moderation

- **User persona overrides** (`tools/persona.py`): `persona_set`/`persona_show`/`persona_clear`, REGULAR-tier searchable tools registered only when `config/models.yaml` assigns a `persona` role. The compiled persona replaces `config/persona.md` inside a code-owned frame; template guardrail prose stays higher priority. See [`docs/persona.md`](docs/persona.md).
- **Privacy consent gate** (`app/consent.py`, `PRIVACY_CONSENT_ENABLED`, off by default): prompts on first interaction before the lock, persistence, or any provider call. Fails closed. `/privacy` (`commands/privacy_cmd.py`) shows the TL;DR plus **Delete memory** and **Delete my data**; full deletion takes an exclusive per-user barrier. See [`docs/privacy.md`](docs/privacy.md) and [`docs/privacy-policy.md`](docs/privacy-policy.md).
- **Content moderation** (`moderation/`, `MODERATION_ENABLED`, off by default): screens model input and output through a pluggable backend with tier exemptions (`MODERATION_OUTPUT_EXEMPT_TIER`); wired by `app/moderation.py`. This screens the *bot*.
- **Application modules** (`app/modules.py`, `KIMI_MODULES`): separately installed, explicitly enabled packages may add commands, listeners, database migrations, background work, guild-config validators, and optional LLM tools. Core also provides actor-scoped, staff-approved guild fragment proposals through `ctx.proposals`. A configured module is required and fails startup if missing or incompatible. Module-owned behavior and retention belong in that module's documentation. See [`docs/modules.md`](docs/modules.md).

### Storage

`storage/db.py` is async SQLite (`aiosqlite`, WAL), initialized in `on_ready`; optional SQLCipher via `DATABASE_ENCRYPTION_KEY`. `messages` is the per-root transcript (one row per Discord message, deduped by `(conversation_id, discord_message_id)`); `message_contexts` maps ids to roots; `usage_ledger` and `paid_usage_ledger` feed `/usage`; `provider_circuits` persists safe normalized cooldown state. Transcripts age out after `transcript_retention_days` (30) via the retention sweeper; usage ledgers, circuit state, and Hindsight memory are excluded. Module schemas have independent versions in `module_schema_versions`. See [`docs/database.md`](docs/database.md).

### Observability and Evals

Tool calls, turn summaries, compaction, and moderation decisions emit structured JSONL through a non-blocking writer (`observability/events.py`, `TOOL_EVENT_LOG_ENABLED`, degrades silently). See [`docs/observability.md`](docs/observability.md).

`evals/` is an offline harness over the production `run_conversation` with a stub gateway: `evals.run` (blind-judged qualification), `evals.harness_run` (repeated mechanical scoring), `evals.compare`. Dispatch-level cassettes record `discord_text_search`, `internet_search`, the read-only Hindsight tools, and plugin tools on `eval_record`; `teach`/`remember_user_memory` and `eval_stub` plugin tools are write-stubbed. Nothing in `evals/` may call the Anthropic API; use `--dry-run` first. See [`docs/evals.md`](docs/evals.md).

### Configuration and Plugins

`config/settings.py` is the single `pydantic-settings` `Settings` class, loaded from env and `.env`. `config/operator_settings.py` overlays `<CONFIG_DIR>/settings.md` at `build_app` (`SETTINGS_SPEC` is generated from `Settings.model_fields` behind a fail-closed exclusion list: no secrets, paths, URLs, `database_*`, or `plugin_modules`); a malformed file stops startup. `CONFIG_DIR` and `SKILLS_DIR` have in-checkout defaults but production points both outside the checkout; [`docs/instance-data.md`](docs/instance-data.md) is the public/private boundary. `config/paths.py` holds the process-wide config-dir default and is stdlib-only. See [`docs/configuration.md`](docs/configuration.md).

Operator plugins (`app/plugins.py`): `PLUGIN_MODULES` is an explicit allowlist of modules exposing `register(ctx: PluginContext)`; no auto-discovery. Loaded after every core tool, so duplicate names resolve in core's favor; per-plugin failure is skip + rollback, never a boot abort. Plugins classify their own settings via `PLUGIN_SETTINGS`, contribute labels via `agent/activity.py:register_tool_labels`, and declare eval surfaces via `ctx.declare_surface_tools`. See [`docs/plugins.md`](docs/plugins.md).

### System Prompt

`config/fragments/prompt.py:build_system_prompt` is template-driven: markdown templates under `config/` with `<placeholder>` tokens that Python fills. Safety, guardrail, and content-rating rules are **prose in the templates**, not code. A template that drops them ships without them. Full-template resolution is most-specific-first: `config/prompts/channels/<thread_id>.md` > `config/prompts/channels/<parent_channel_id>.md` > `config/prompts/servers/<guild_id>.md` > `config/prompt.md`. The `<channel_instructions>` slot is thread-aware (`instruction_fragment_candidates`): `config/threads/<thread_id>.md` > `config/channel_threads/<parent>.md` > `config/channels/<parent>.md`, first non-empty body wins. Substitution is single-pass, Discord-sourced scalars are sanitized, and prompt files are read fresh each turn. See [`bot/config/prompts/README.md`](bot/config/prompts/README.md).
