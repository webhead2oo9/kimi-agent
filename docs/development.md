# Development

The bot is a long-lived process that talks to live Discord, so an end-to-end
"dev mode" really means **a second real instance**, with its own bot token,
database, and workspaces, pointed at a test guild. Unit tests and the offline
eval harness are useful, but they don't replace Discord with an interactive
simulator.

Run the commands below from `bot/` unless a section says otherwise. Keep the dev
instance isolated even when production runs on another machine: a copied token
or a shared path is all it takes for a local process to affect live users and
their data.

## First-time setup

Development requires Python 3.14 or newer and
[uv](https://docs.astral.sh/uv/). Install the application and development
dependencies from the lockfile:

```bash
cd bot
uv sync --extra dev
```

Always run Python tools through `uv run`. A bare `python`, `pytest`, or `mypy`
may pick up a different interpreter and produce misleading import or type
errors. The first sync creates a local `.venv`; later syncs update it whenever
`pyproject.toml` or `uv.lock` changes.

## The switch

`ENV_FILE` selects the dotenv file for the core `Settings` object **and every
enabled plugin settings class**, through the shared helper in
`config/environment.py`:

```bash
ENV_FILE=.env.dev uv run python bot.py
```

PowerShell doesn't support the inline assignment form, so set the variable for
the current shell instead:

```powershell
$env:ENV_FILE = ".env.dev"
uv run python bot.py
Remove-Item Env:ENV_FILE
```

When it's unset, the bot loads `.env`. A path that doesn't exist raises at
import, and that's intentional: loading nothing silently would
hand you a valid-but-empty config that dies somewhere far from the typo.

Plugin settings must not declare their own hard-coded `env_file`. Put private
plugin credentials and environment-only connection settings in the selected
file (`.env.dev` here), never in `.env`, so the second process stays isolated
across both core and optional integrations. See [plugins.md](plugins.md) for
the complete plugin contract.

## Setting up `.env.dev`

Copy `.env.example` to `.env.dev`, then set a **different value for every path**
so the dev instance can't touch production state:

```bash
DISCORD_BOT_TOKEN=<a second bot application's token>

# Separate state. Nothing here may overlap the production values.
DATABASE_PATH=data/dev/bot.db
WORKSPACE_DIR=data/dev/workspaces
ATTACHMENT_STORE_DIR=data/dev/attachments
PERSONAL_SKILLS_DIR=data/dev/personal_skills
TOOL_EVENT_LOG_PATH=logs/dev/events.jsonl
SECRETS_FILE=secrets/dev-secrets.yaml
CODEX_TOKEN_FILE=secrets/dev-codex-auth.json   # rewritten on OAuth refresh; never share it

# Optional: point at a scratch copy of the operator data instead of the live
# instance directory, so a bad prompt edit in dev cannot reach production
# (see "Trying a different LLM" below for the copy step).
CONFIG_DIR=config.dev
SKILLS_DIR=skills.dev/store
```

Both directory patterns are gitignored. Seed them from the production instance
only when a test genuinely needs that private content; otherwise a minimal
scratch config and an empty skill store are enough. See
[instance-data.md](instance-data.md).

## Trying a different LLM

Model routing lives in `<CONFIG_DIR>/models.yaml`, and that file is untracked
instance state (see [providers.md](providers.md)), so editing it to try another
model dirties nothing in git and can't be committed by accident.

For a change you want *only* in dev, give the dev instance its own operator
directory rather than editing the shared one:

```bash
mkdir -p config.dev/prompts/commands
cp config/prompt.md config/persona.md config.dev/
cp config/prompts/commands/*.md config.dev/prompts/commands/
cp config/models.example.yaml config.dev/models.yaml
# edit config.dev/models.yaml: replace placeholders, choose dev-only routing
CONFIG_DIR=config.dev ENV_FILE=.env.dev uv run python bot.py
```

Don't recursively copy `config/`: on a live checkout that would also sweep up
the ignored production model file, settings overlay, and Discord fragments.
Copy a specific private fragment only when the dev test actually requires it.

Add the key that the new profile's `api_key_env` names to `.env.dev`, not
`.env`. None of this reaches production: production reads its own `CONFIG_DIR`
on its own host, and a `git pull` there never carries a `models.yaml`.

The same isolation applies to plugin operator overrides. They live at
`<CONFIG_DIR>/plugins/<name>.md`, so `CONFIG_DIR=config.dev` keeps them out of
the production config tree. Plugin overrides are startup-only, which means you
edit them and then restart the dev bot before testing the changed behavior.

To compare models on fixed scenarios instead of by hand, use the eval harness.
Copy `evals/models.example.yaml` to the ignored `evals/models.yaml`; it's a
private catalog separate from the bot's routing. Eval keys are resolved
separately from the bot's as well: `ENV_FILE` doesn't apply to the harness,
which reads the shell environment first and then `bot/.env`
(`evals/models.py:resolve_api_key`), so export the key in the shell or keep it
in `.env`. See [evals.md](evals.md).

Of all these settings, `DATABASE_PATH` is the one that matters most. Production
`data/bot.db` is real user data, and a dev boot writing into it is exactly the
mistake worth engineering against.

Local state deserves the same care. Encryption is off by default on every
platform, and this repository installs SQLCipher only on Linux. A normal
Windows dev database is therefore plaintext and may contain retained
transcripts (30 days by default). Before cleaning or retiring a dev machine,
review what needs to be kept. `git clean -ndx` previews ignored files;
`git clean -fdx` removes them, including `data/`, dotenv files, and
`config/models.yaml`.

Leave `ALLOWED_GUILD_IDS` blank to exercise the normal activation flow: invite
the dev bot, then create `<CONFIG_DIR>/servers/<guild_id>.md` with
`bot_active: true` in its YAML frontmatter. Until that file is valid, the bot
stays connected to the test guild but ignores its messages. The environment
setting remains an optional bootstrap approval; `bot_active: false` keeps a
guild inactive even when the environment lists it.

## A second bot application

Create a separate application in the Discord Developer Portal, enable the
Message Content intent, and invite it to a test guild. Sharing one token between
two running processes means both receive every gateway event and both reply,
which is not what you want.

Give the dev application only the Discord permissions the features under test
require. Record its token only in `.env.dev`, and never copy the production
token as a temporary shortcut. Activate the test guild through
`<CONFIG_DIR>/servers/<guild_id>.md` or a dev-only `ALLOWED_GUILD_IDS` value.

## Starting and verifying the instance

Start the bot with the selected dev environment:

```bash
ENV_FILE=.env.dev uv run python bot.py
```

A successful boot logs the Discord account, the number of active and inactive
guilds, the database path, and the number of synchronized slash commands. Check
those values before sending a test message, since they're the quickest way to
catch the wrong token, guild, or database path. Then mention the bot in the
test guild and confirm that one reply appears and survives a process restart
as expected. Stop the process with `Ctrl+C`.

If the bot connects but ignores every message, check guild activation first.
An invited guild remains silent until it's explicitly activated, and
`bot_active: false` overrides `ALLOWED_GUILD_IDS`. See
[Setup](setup.md#the-bot-starts-and-then-ignores-every-mention) for the complete
failure checklist.

## Editing config and prompts

Edit `<CONFIG_DIR>/settings.md`, the model/prompt/persona files, and the
fragment trees directly. Point `CONFIG_DIR` at a scratch copy while
experimenting so a dev change can't reach production operator data.

Some files are read during each turn, while others shape startup composition,
so it helps to know which is which:

| Change | When it takes effect |
|---|---|
| Prompt templates, persona text, channel/server/thread fragments, tool policy, and live per-tool settings | The next turn; no restart required. |
| `.env.dev`, `models.yaml`, `settings.md`, `PLUGIN_MODULES`, plugin settings, secret metadata, and executable skill registrations | Restart the dev bot. |

When in doubt, restart before judging behavior. With isolated state a restart
is cheap, and it removes any ambiguity about which configuration the process
has actually loaded.

## What still needs the real thing

- **Memory** needs a reachable Hindsight (`HINDSIGHT_URL`). Leave it unset and
  the memory tools simply don't register.
- **Executable skills** need Linux with `bubblewrap` + `util-linux` and a
  non-root service account; on a Windows/macOS dev host, use an instruction-only
  or empty skill store (see [setup.md](setup.md)).
- **Persistent browser and visual rendering** also need the Linux isolation
  stack and pinned BetterWright/Mermaid runtime. They are off unless
  `BROWSER_ENABLED=true`, so a Windows/macOS dev host needs no change and can
  still run schema, validation, command-construction, and artifact tests. To
  exercise Chromium, use an isolated Linux test instance and a separate
  `BROWSER_PROFILES_DIR`; never point development at production profiles. Run
  `uv run python -m deploy.betterwright.smoke_test` there. See
  [browser.md](browser.md) and [visual-rendering.md](visual-rendering.md).
- **Content moderation** needs its provider key, and **Codex** needs a
  `CODEX_TOKEN_FILE` produced by `scripts/codex_auth.py`.

## Tests

The Python suite needs no dotenv file, Discord connection, or network. CI runs
these checks from `bot/` (`../.github/workflows/ci.yml`):

```bash
uv sync --locked --extra dev
uv --preview-features audit-command audit --locked
uv run ruff check .
uv run ruff format --check .   # line length is the formatter's job, not the linter's
uv run mypy .
uv run python -m pytest -q
npm ci --prefix deploy/betterwright --omit=dev --omit=optional --ignore-scripts
npm audit --prefix deploy/betterwright --omit=dev
node --check web_browser/bridge.mjs
node --check web_browser/visual_bridge.mjs
```

`uv run mypy .` is the CI and Linux form. On Windows, uv's script trampoline
may fail to canonicalize the path; use `uv run python -m mypy .` instead.

While you're developing, run the smallest relevant test first:

```bash
uv run python -m pytest tests/test_storage.py -q
uv run python -m pytest tests/test_storage.py::test_fresh_database_uses_the_current_schema_version -q
```

Use `uv run ruff format <paths>` to format the files you changed, then run the
full CI sequence before handoff. Tests use temporary databases and
hand-written fakes, so they never need `.env.dev` or the live dev bot.

Offline harness evals replay recorded tool calls through cassettes; see
[evals.md](evals.md).


## Module lifecycle ceilings

Two settings bound how long a configured module may hold up startup or
shutdown; both default to values a well-behaved module never reaches.

```dotenv
MODULE_START_TIMEOUT_SECONDS=60   # start() past this fails the module and aborts startup
MODULE_CLOSE_TIMEOUT_SECONDS=15   # close() past this is cancelled; shutdown continues
```

A start timeout logs `Kimi module <name> 'start() exceeded 60s'` and emits a
`module_health` event with state `failed`; the process then exits like any
other module failure, so the log and event are the diagnostic surface. A close
timeout logs `Kimi module <name> close() exceeded 15s; continuing shutdown`
and the remaining modules still close. In both cases the module's coroutine
is cancelled and given five seconds; one that ignores cancellation is logged
as abandoned and left to the event loop.
If a module trips either ceiling during development, the fix belongs in the
module (move slow work into a scheduler job, or make `close()` cancel rather
than await), not in the setting.

The module scheduler runs `MODULE_SCHEDULER_MAX_CONCURRENT_JOBS` (default 4)
jobs concurrently, at most one per module. If a dev instance shares a database
file with another running instance, the scheduler logs `Module scheduler
paused: another scheduler runner holds the lease` and runs nothing until the
other process stops (or its 60-second lease expires); the isolated dev setup
above avoids this by giving each instance its own database.
