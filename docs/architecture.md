# Architecture

This page is a map of Kimi: the ways people talk to it, and which folders own which job.

## What this bot is

People talk to Kimi in a few different ways. Under the hood they all use the same model loop, so swapping OpenAI, Anthropic, or another provider does not change the rest of the bot. There is also an optional background coding worker. You can install extra modules if you want features that are not in core.

### Community chat

This is ordinary Discord chat in a server. If someone mentions the bot, replies to it with the reply ping on, starts a message with `hey <bot name>`, `hi <bot name>`, or `<bot name> help`, or talks in an auto-responding thread it created, Kimi starts a tool-using turn with the saved conversation, trust-gated tools, and optional Hindsight memory. There is no general guild `/chat` command.

In code, a Discord message lands in `DiscordMessageController`, which decides whether the bot should answer at all and takes the lock for that conversation. It then hands the turn to the shared foreground runner, which runs the model and posts the reply back to the channel.

### Personal chat (optional)

`/chat` is optional and off until you enable it. It uses the same model loop, but each person gets a private `userchat:<user_id>` conversation, plus their own prompt, memory, and workspace. That chat is not tied to any Discord server, so server tools and server files stay out of reach. The channel they typed in is only used for Discord permissions. It does not grant extra trust. See [Discord user-app personal chat](user-app.md).

In code, the `/chat` command does its own access and consent checks and takes the conversation lock, then uses the same foreground runner. The only difference is how the reply gets back to Discord: a slash command reply instead of a channel message.

### Teach Kimi

Staff can use the message context menu (named after `BOT_NAME`) to teach the bot from a selected message. That is still a model turn, but it only gets the `LEARN_TOOLS` set. The reply is shown once and is not saved to the conversation. `app/learn_log.py` writes an audit record. Code path: `commands/learn_cmd.py` → `app/learn_turn.py`.

### Durable coding agent (optional)

Big repository jobs should not hold up a live Discord reply. `start_coding_task` hands them to a background worker with its own `roles.coding` model (never the normal chat model). Progress lives in SQLite and comes back to Discord as the work moves. This only turns on if you enable it, the coding model can call tools, and the Linux sandbox is available. See [Durable coding agent](coding-agent.md).

## Where things live (`bot/`)

| Path | What it is |
|---|---|
| `bot.py` | Starts the process: load settings, `build_app()`, run |
| `app/` | Where the bot is wired together, then handed Discord events |
| `agent/` | The model loop: turn prep, tools, compaction, attachments |
| `discord_adapter/` | Talks to Discord: sending, receiving, permissions, cleanup jobs |
| `providers/` | How Kimi talks to model APIs, including failover |
| `image_gen/` | Image generation/editing backend |
| `search/` | Internet search backends (TinyFish, Exa, Brave) and the chain that blends them |
| `video_understanding/` | Gemini video sessions |
| `xai/` | xAI login and credentials, plus the Responses transport used by Grok models and X search |
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
| `scripts/` | Operator helpers: Codex and xAI login, preflight, service install, diagnostics, sandbox probe |

`app/` is a large package. These are the files you are most likely to open:

| File | What it does |
|---|---|
| `runtime.py` | `build_app()` wires everything together and receives Discord events |
| `lifecycle.py` | Opens the database and background resources when Discord reports READY, and shuts them down cleanly |
| `message_runtime.py` | Decides whether a guild message gets a reply and assembles the turn; `admission.py` and `conversation_routing.py` do the detailed checks |
| `foreground_turn.py` | The shared prepare → run → deliver sequence used by guild messages and `/chat` |
| `guild_turn_adapter.py` | Delivers a guild-message turn's reply back to the channel |
| `user_app_chat.py`, `user_app_turn_adapter.py` | Personal `/chat`: its access rules, and delivering its reply through the slash-command response |
| `user_app_consent.py` | The privacy consent prompt shared by `/chat` and Teach Kimi |
| `response_delivery.py` | Posts replies and attachments to Discord, checking workspace file rules first |
| `command_sync.py` | Publishes slash commands to Discord after READY |
| `guild_activation.py` | Tracks which servers are active and refreshes their state |
| `root_locks.py` | One lock per conversation, so two replies to the same conversation run one after the other |
| `work_cancellation.py` | Stops running foreground turns and coding tasks for `/stop`, reset, and `/privacy` |
| `coding_tasks.py`, `coding_delivery.py` | Schedules durable coding tasks and reports their progress to Discord |
| `turn_entry.py` | Works out which tools a given turn may use |
| `tools.py` | Registers the built-in tools and their clients |
| `modules.py`, `plugins.py` | Loads installed modules and operator plugins |

## Design choices that matter

**Wired in one place.** Core starts in `app/runtime.py:build_app()`. Extra tools for one deployment can come from plugins (`app/plugins.py`). If a plugin breaks, Kimi logs it and keeps going. Installed modules (`app/modules.py`) are required by default; explicitly optional modules can be disabled after recoverable failures.

**Each server keeps its own stuff.** Every stored record carries the server (guild) id. A server has to be explicitly allowed. Trust, prompts, pins, and denylists come from `config/servers/<id>.md`. Workspaces and community memory are per server. `/chat` is the exception so using it inside a server cannot pull that server's private config.

**Config is files.** `models.yaml` and `settings.md` are checked once at startup, so changing them means a restart. Prompt and policy files are re-read every turn, so you can edit those and see the change on the next message.

**Refuse by default.** Tools check trust and denylists when they run. Moderation blocks a bad reply before it hits Discord. A required module that is missing or incompatible stops the process. Hindsight is the exception: if it is down, the bot keeps running without memory.

## Where to go next

- [`../bot/README.md`](../bot/README.md): what the bot can do
- [Configuration](configuration.md): every setting and live fragment
- [Discord user-app personal chat](user-app.md): optional `/chat`
- [Tool catalog](tools.md): built-in tools and who can use them
- [Code execution](code-exec.md): the Linux sandbox
- [Visual rendering](visual-rendering.md): charts and Mermaid
- [Durable coding agent](coding-agent.md): background coding jobs
- [Internet search](internet-search.md): TinyFish, Exa, and Brave search
- [X search](x-search.md): searching X through xAI
- [Video understanding](video-understanding.md): YouTube and uploaded video
- [Image generation](image-generation.md): image generation and editing
