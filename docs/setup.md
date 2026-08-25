# Setup and boot

What a fresh deployment needs, in order. Runtime commands and configuration
live under `bot/`; each command block below is self-contained and starts from
the repository root.

## 1. Dependencies

```sh
cd bot
uv sync
```

Python ≥3.14, managed by uv (`pyproject.toml` / `uv.lock` are pinned). Linux is
the supported production and security-review target; other platforms are suitable
for development but do not carry the production containment guarantees.

Executable skill tools additionally require Bubblewrap and util-linux. On
Debian/Ubuntu:

```sh
sudo apt-get install bubblewrap util-linux
```

When `SKILLS_DIR` contains a `tools:` declaration, boot runs a real namespace
probe and fails if the packages, kernel user-namespace support, or host security
policy cannot create the boundary. The bot process must also run as an
unprivileged service account; executable-skill boot explicitly rejects UID 0.
There is no unsandboxed fallback. An instruction-only or empty skill store does
not require these binaries.

The persistent browser additionally requires Node 22.18 or newer and the
root-owned pinned runtime installed by
[`bot/deploy/betterwright/install.sh`](../bot/deploy/betterwright/install.sh).
Follow [Persistent browser](browser.md) for host or VPN-namespace deployment.
Without a valid runtime and sandbox probe, boot continues but the `browser` tool
does not register.

## 2. Minimal configuration

Boot smoke (login, respond to mentions) needs exactly four things:

| Requirement | Where | Notes |
|---|---|---|
| Discord bot token | `DISCORD_BOT_TOKEN` in `bot/.env` | Copy `bot/.env.example` → `bot/.env` first; the token comes from the Developer Portal application. |
| A guild the bot is in | Developer Portal invite URL | `applications.commands` + `bot` scopes, with least-privilege permissions for the features you enable. |
| That guild activated | `ALLOWED_GUILD_IDS=<guild id>` in `.env`, **or** `config/servers/<guild_id>.md` with `bot_active: true` | Guild activation fails closed: an invited but unactivated guild gets no responses at all. Empty `ALLOWED_GUILD_IDS` does **not** mean "all guilds". |
| One chat provider | `bot/config/models.yaml` + its `api_key_env` in `.env` | Copy `config/models.example.yaml` → `config/models.yaml`; replace its non-routable host/model placeholders; set accurate context windows, capabilities, roles, and fallbacks; then fill the selected key. The template stays text-only until vision is explicitly verified. |

`config/models.yaml`, deployment guild/channel/thread fragments, `.env`, and the
entire live `SKILLS_DIR` are gitignored instance data. Generic examples and
read-only built-in skills are tracked. The in-checkout paths are fine for a test
deployment; production points `CONFIG_DIR` and `SKILLS_DIR` at a private tree
outside the checkout. An absent private skill store is valid; restore it before
boot when the deployment needs its learned or operator-authored skills. See
[`instance-data.md`](instance-data.md) for the private-repository tree, required
prompt/model files, deployment workflow, and the data that must remain outside
both repositories.

### Optional but recommended for a test deployment

- `BOT_NAME`: substituted into the persona (`config/persona.md`).
- Message Content intent: enabled in the Developer Portal for the full
  experience ("hey <name>" text trigger, thread auto-reply,
  `discord_text_search`). Without it, plain mentions and replies still work;
  set `MESSAGE_CONTENT_INTENT=false` and `THREAD_HANDOFF_ENABLED=false` while
  it's unapproved.

### Not needed for boot

Everything else degrades or stays off: Hindsight memory (empty `HINDSIGHT_URL`
= memory disabled, bot runs fine), OpenAI moderation, `DISCORD_SEARCH_CHANNELS`,
  application modules, plugins, the persona compiler, SQLCipher at-rest encryption. Defaults in
`bot/.env.example` are production-reasonable; only fill what you use.

## 3. Boot

```sh
cd bot
uv run python bot.py
```

Expected: settings validate, SQLite opens and initializes schema v1, the gateway
connects, `on_ready` completes boot under the
READY initialization lock, JSONL turn events write to `logs/` when enabled. A mention in
the test guild round-trips: mention → ReAct turn → reply → durable transcript.

## 4. Verification gates

Standing gates before declaring any change good (what CI enforces):

```sh
cd bot
uv sync --locked --extra dev
uv run ruff check .
uv run ruff format --check .
uv run mypy .
uv run python -m pytest -q
```

These are exactly what CI runs (`.github/workflows/ci.yml`).

## 5. When boot fails

Symptom first. Each message below is the exact text the process logs.

| What you see | What it means |
|---|---|
| `DISCORD_BOT_TOKEN is not set`, then exit 1 | The token is missing from the selected dotenv file (`.env`, or whatever `ENV_FILE` names). |
| `Configured model credentials are unavailable; check config/models.yaml and the referenced .env secret values.`, then exit 1 | A reachable model has no usable key. The check covers every role including `roles.compaction`, so a compaction-only profile with an unset key stops boot too. `keyless: true` profiles and Codex profiles are checked differently: gateway-held credentials and the token file. |
| `Model routing file not found: <path>. Copy config/models.example.yaml to <path>, then replace its placeholders.` | `<CONFIG_DIR>/models.yaml` does not exist. Boot never falls back to the tracked template. |
| `Codex authentication rejected (...). Run: python scripts/codex_auth.py --token-file <file>`, then exit 1 | The stored Codex token was revoked. A network error or timeout during the same check only warns and retries on first use. |
| `Executable skill tools require Linux; unsandboxed execution is disabled` | The skill store declares `tools:` on a non-Linux host. |
| `Executable skill tools require an unprivileged service account; refusing to run as root` | The process is UID 0. |
| `Executable skill tools require bwrap, prlimit; unsandboxed execution is disabled` | The packages from step 1 are absent, or present but not executable. |
| `Executable skill sandbox probe exited <code>: <stderr>` | Bubblewrap is installed, but the kernel or host security policy will not let it create the namespace. |

The four sandbox messages appear only when the store actually declares `tools:`.
An instruction-only or empty store skips the probe.

### The bot starts and then ignores every mention

Booting is not activation, and an unactivated guild logs nothing at all. Check
the guild's activation state:

- No `config/servers/<guild_id>.md`, and the id is not in `ALLOWED_GUILD_IDS`:
  the state is `pending`. The bot is silent.
- The fragment exists but the state is `invalid_setup`: `server_setup_activation`
  refused it. It fails closed on *any* malformed sibling key, not just
  `bot_active`. A non-numeric `learn_log_channel_id`, or one bad entry in `staff_user_ids`,
  `staff_role_ids`, `regular_role_ids`, or `thread_targets` voids the whole file.
  Active modules can add their own fail-closed validators. A typo in a trust list
  must not activate a guild with the wrong boundaries.
- `bot_active: false` wins over `ALLOWED_GUILD_IDS`.

Non-fatal at boot: a chat model whose `context_window` is smaller than
`COMPACTION_TRIGGER_TOKENS` + `REACT_MAX_TOKENS` logs one warning naming both
numbers and the model, and the bot keeps running.

Dev-instance isolation (separate config/data for a second local bot) is covered
in [`development.md`](development.md).
