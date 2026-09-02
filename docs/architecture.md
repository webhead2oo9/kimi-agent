# Architecture

This page is a map of Kimi: the ways people talk to it, and which folders own which job.

## What this bot is

People talk to Kimi in a few different ways. Under the hood they all use the same model loop, so swapping OpenAI, Anthropic, or another provider does not change the rest of the bot. There is also an optional background coding worker. You can install extra modules if you want features that are not in core.

### Community chat

This is ordinary Discord chat in a server. If someone mentions the bot, ping-replies to it, says `hey/hi <bot name>`, or talks in an auto-responding thread it created, Kimi starts a tool-using turn with the saved conversation, trust-gated tools, and optional Hindsight memory. There is no general guild `/chat` command.

The gateway entry point delegates to `DiscordMessageController`, which owns the Discord gates and root lock, then sends the turn through the shared foreground runner and its guild-message adapter.

### Personal chat (optional)

`/chat` is optional and off until you enable it. It uses the same model loop, but each person gets a private `userchat:<user_id>` conversation, plus their own prompt, memory, and workspace. That chat is not tied to any Discord server, so server tools and server files stay out of reach. The channel they typed in is only used for Discord permissions. It does not grant extra trust. See [Discord user-app personal chat](user-app.md).

Its registered command keeps the interaction gates and root lock, then uses the same foreground runner with the deferred-interaction adapter.

### Teach Kimi

Staff can use the message context menu (named after `BOT_NAME`) to teach the bot from a selected message. That is still a model turn, but it only gets the `LEARN_TOOLS` set. The reply is shown once and is not saved to the conversation. `app/learn_log.py` writes an audit record. Code path: `commands/learn_cmd.py` → `app/learn_turn.py`.

### Durable coding agent (optional)

Big repo jobs should not sit in a live Discord reply. `start_coding_task` hands them to a background worker with its own `roles.coding` model (never the normal chat model). Progress lives in SQLite and comes back to Discord as the work moves. This only turns on if you enable it, the coding model can call tools, and the Linux sandbox is available. See [Durable coding agent](coding-agent.md).

## Where things live (`bot/`)

| Path | What it is |
|---|---|
| `bot.py` | Starts the process: load settings, `build_app()`, run |
| `app/` | Where the bot is wired together, then handed Discord events |
| `agent/` | The model loop: turn prep, tools, compaction, attachments |
| `discord_adapter/` | Talks to Discord: sending, receiving, permissions, cleanup jobs |
| `providers/` | How Kimi talks to model APIs, including failover |
| `image_gen/` | Image generation/editing backend |
| `search/` | Internet search (Exa, Brave, and the shared chain) |
| `video_understanding/` | Gemini video sessions |
| `config/` | Settings, `models.yaml`, operator overlay |
| `config/fragments/` | Markdown you can edit without restarting: pins, denylists, prompts, per-tool settings |
| `tools/` | Tool registry and the built-in tools |
| `web_browser/` | Persistent browser profiles, plus one-shot chart/Mermaid rendering |
| `workspace/` | Per-user file folders |
| `sandbox/` | The Linux jail for running code |
| `commands/` | Slash commands and the Teach Kimi menu |
| `memory/` | Hindsight memory: banks, auto-retain, opt-out |
| `storage/` | SQLite: conversations, usage, circuits, coding tasks, video sessions |
| `trust/` | Who counts as member, regular, or staff |
| `moderation/` | Screens model input and output |
| `observability/` | JSONL logs of turns, tools, and moderation |
| `usage/` | Token and cost accounting |
| `codex/` | Codex login and its WebSocket transport |
| `utils/` | Small shared helpers |
| `evals/` | Offline test harness for the model loop |
| `skills/` | Built-in skill docs plus the private skill store |
| `modules/` | Services used by installed modules, plus the in-repo example |
| `packages/` | The standalone module API package |
| `tests/` | Tests, including the import-boundary checks |
| `deploy/` | Installer bits for the browser runtime and network namespaces |
| `scripts/` | Operator helpers: Codex login, preflight, service install, diagnostics |

`app/` is a large package. The important files are `runtime.py` (`build_app` and thin Discord ingress), `lifecycle.py` (repository ownership, READY initialization, background resources, and shutdown), `message_runtime.py` (message admission, routing, and turn composition), `response_delivery.py` (workspace-guarded Discord response delivery), `command_sync.py` (READY-cohort command publication), `guild_activation.py` (live guild activation and refresh), `foreground_turn.py` (the typed prepare/run/deliver seam), `guild_turn_adapter.py` (gateway-message delivery through a frozen collaborator bundle), `user_app_chat.py` (personal-chat policy and execution), `user_app_consent.py` (shared interaction consent), `user_app_turn_adapter.py` (deferred `/chat` delivery), `work_cancellation.py` (foreground/coding teardown), `coding_tasks.py` (durable worker scheduling), `coding_delivery.py` (durable Discord projection and control), `root_locks.py` (refcounted conversation serialization), `turn_entry.py` (who gets a turn), `tools.py` (what tools get wired), `modules.py`, and `plugins.py`.

## Design choices that matter

**Wired in one place.** Core starts in `app/runtime.py:build_app()`. Extra tools for one deployment can come from plugins (`app/plugins.py`). If a plugin breaks, Kimi logs it and keeps going. Installed modules (`app/modules.py`) are required: missing or incompatible ones stop startup.

**Each server keeps its own stuff.** Guild id is on the data. A server has to be explicitly allowed. Trust, prompts, pins, and denylists come from `config/servers/<id>.md`. Workspaces and community memory are per server. `/chat` is the exception so using it inside a server cannot pull that server's private config.

**Config is files.** `models.yaml` and `settings.md` are checked at startup. Prompt and policy files are re-read every turn, so you can edit those without a console or a restart.

**Refuse by default.** Tools check trust and denylists when they run. Moderation blocks a bad reply before it hits Discord. A configured module that is missing or incompatible stops the process. Hindsight is the exception: if it is down, the bot keeps running without memory.

## Where to go next

- [`../bot/README.md`](../bot/README.md): what the bot can do
- [Configuration](configuration.md): every setting and live fragment
- [Discord user-app personal chat](user-app.md): optional `/chat`
- [Tool catalog](tools.md): built-in tools and who can use them
- [Code execution](code-exec.md): the Linux sandbox
- [Visual rendering](visual-rendering.md): charts and Mermaid
- [Durable coding agent](coding-agent.md): background coding jobs
- [Internet search](internet-search.md): Exa/Brave search
- [Video understanding](video-understanding.md): YouTube and uploaded video
- [Image generation](image-generation.md): image generation and editing
