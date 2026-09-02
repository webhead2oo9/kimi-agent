# Development

This page covers first-time setup and running an isolated dev instance. The
maintainer sections on lock auditing, distribution builds, and module lifecycle
ceilings are kept short; follow the linked pages for the full procedures.

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

Development requires Python 3.14 or newer and its standard `venv` module.
Install the three local workspace projects and their application and development
dependencies in one resolver run:

```bash
cd bot
python3 -m venv .venv
.venv/bin/python -m pip install \
  --editable ./packages/kimi-agent-module-api \
  --editable ./modules/example \
  --editable ".[dev]"
.venv/bin/python -m pip check
```

If [uv](https://docs.astral.sh/uv/) is already installed, these two commands
replace the setup block on POSIX hosts:

```bash
uv sync --locked --all-packages --extra dev
.venv/bin/python -m ensurepip
.venv/bin/python -m pip install --no-deps \
  --editable ./packages/kimi-agent-module-api \
  --editable ./modules/example \
  --editable .
.venv/bin/python -m pip check
```

uv prunes pip on each sync, while later module-install commands need it. Both
routes create `.venv`. Always use that environment's explicit executables; a
bare `python`, `pytest`, or `mypy` may select a different interpreter. On
Windows PowerShell, the uv equivalent is:

```powershell
uv sync --locked --all-packages --extra dev
.\.venv\Scripts\python.exe -m ensurepip
.\.venv\Scripts\python.exe -m pip install --no-deps `
  --editable ./packages/kimi-agent-module-api `
  --editable ./modules/example `
  --editable .
.\.venv\Scripts\python.exe -m pip check
```

The complete standard PowerShell setup is:

```powershell
Set-Location bot
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install `
  --editable ./packages/kimi-agent-module-api `
  --editable ./modules/example `
  --editable ".[dev]"
.\.venv\Scripts\python.exe -m pip check
```

The other explicit executable mappings are `.venv/bin/ruff` to
`.\.venv\Scripts\ruff.exe` and `.venv/bin/mypy` to
`.\.venv\Scripts\mypy.exe`.

## Choosing which dotenv file to load

`ENV_FILE` selects the dotenv file for the core `Settings` object **and every
enabled plugin or application-module settings class**, through the shared helper in
`config/environment.py`:

```bash
ENV_FILE=.env.dev .venv/bin/python bot.py
```

PowerShell doesn't support the inline assignment form, so set the variable for
the current shell instead:

```powershell
$env:ENV_FILE = ".env.dev"
.\.venv\Scripts\python.exe bot.py
Remove-Item Env:ENV_FILE
```

When `ENV_FILE` is unset, the bot loads `.env`. If the file you name does not exist, the bot refuses to start. That is deliberate: silently loading nothing would give you a valid-looking but empty config that fails somewhere far from the typo.

Plugin and module settings must not declare their own hard-coded `env_file`. Put their private credentials and environment-only connection settings in the selected file (`.env.dev` here), never in `.env`, so the second process stays isolated across core and optional integrations. See [plugins.md](plugins.md) and [modules.md](modules.md) for their complete contracts.

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
BROWSER_PROFILES_DIR=data/dev/browser_profiles
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
CONFIG_DIR=config.dev ENV_FILE=.env.dev .venv/bin/python bot.py
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

Of all these settings, `DATABASE_PATH` is the one that matters most. Production `data/bot.db` is real user data, and a dev instance writing into it is exactly the mistake this setup exists to prevent.

Local state deserves the same care. Encryption is off by default on every platform, and this repository installs SQLCipher only on Linux. A normal Windows dev database is therefore plaintext and may contain retained transcripts (30 days by default). Before cleaning or retiring a dev machine, review what needs to be kept. `git clean -ndx` previews ignored files; `git clean -fdx` removes them, including `data/`, dotenv files, and `config/models.yaml`.

Leave `ALLOWED_GUILD_IDS` blank to exercise the normal activation flow: invite the dev bot, then create `<CONFIG_DIR>/servers/<guild_id>.md` with `bot_active: true` in its YAML frontmatter. Until that file is valid, the bot stays connected to the test guild but ignores its messages. The environment setting is an optional bootstrap approval; `bot_active: false` keeps a guild inactive even when the environment lists it.

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
ENV_FILE=.env.dev .venv/bin/python bot.py
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
[Setup](setup.md#18-troubleshooting) for the complete
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
- **Code execution and coding tasks** need the full Linux boundary in
  [code-exec.md](code-exec.md) (Bubblewrap, `prlimit`, libseccomp, a lingering
  user systemd manager, and a file `core_pattern`). Without it the live-jail
  tests skip, and `run_code` does not register.
  `python -m scripts.sandbox_probe` names the missing prerequisite for the
  configured profile; the CI `sandbox` job provisions all of them and runs
  those tests with `KIMI_REQUIRE_SANDBOX_TESTS=1`, where a sandbox-gate skip
  counts as failure.
- **Persistent browser and visual rendering** also need the Linux isolation
  stack and pinned BetterWright/Mermaid runtime. They are off unless
  `BROWSER_ENABLED=true`, so a Windows/macOS dev host needs no change and can
  still run schema, validation, command-construction, and artifact tests. To
  exercise Chromium, use an isolated Linux test instance and a separate
  `BROWSER_PROFILES_DIR`; never point development at production profiles. Run
  `.venv/bin/python -m deploy.betterwright.smoke_test` there. See
  [browser.md](browser.md) and [visual-rendering.md](visual-rendering.md).
- **Content moderation** needs its provider key, and **Codex** needs a
  `CODEX_TOKEN_FILE` produced by `scripts/codex_auth.py`.

## Tests

The Python suite needs no dotenv file, Discord connection, or network. Tests
construct `Settings` through `tests.helpers.make_settings` (or pass
`_env_file=None` explicitly), and an autouse fixture removes ambient settings
variables before each test; `tests/test_settings_isolation.py` enforces both.
A test that intentionally reads the live operator profile carries the
`uses_live_settings_env` marker. `KIMI_REQUIRE_SANDBOX_TESTS` is not a setting
and is never removed. After the first-time standard environment setup, run these
checks from `bot/`:

```bash
.venv/bin/ruff check .
.venv/bin/ruff format --check .   # line length is the formatter's job, not the linter's
.venv/bin/mypy .
.venv/bin/mypy --config-file modules/example/pyproject.toml modules/example/src modules/example/tests
.venv/bin/python -m pytest -q
npm ci --prefix deploy/betterwright --omit=dev --omit=optional --ignore-scripts
npm audit --prefix deploy/betterwright --omit=dev
node --check web_browser/bridge.mjs
node --check web_browser/visual_bridge.mjs
node --test tests/js/*.test.mjs
```

The equivalent Python checks in Windows PowerShell are:

```powershell
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\ruff.exe format --check .
.\.venv\Scripts\mypy.exe .
.\.venv\Scripts\mypy.exe --config-file modules/example/pyproject.toml modules/example/src modules/example/tests
.\.venv\Scripts\python.exe -m pytest -q
```

CI also proves the standard venv/pip install. Maintainer lock auditing and
distribution builds still require uv:

```bash
uv sync --locked --all-packages --extra dev
uv --preview-features audit-command audit --locked
uv build --package kimi-agent-module-api --no-sources --out-dir dist/module-api
uv build --package community-agent-reference-module --no-sources --out-dir dist/reference-module

api_wheel=$(find dist/module-api -maxdepth 1 -name '*.whl' -print -quit)
api_sdist=$(find dist/module-api -maxdepth 1 -name '*.tar.gz' -print -quit)
reference_wheel=$(find dist/reference-module -maxdepth 1 -name '*.whl' -print -quit)
test -n "${api_wheel}"
test -n "${api_sdist}"
test -n "${reference_wheel}"
uv run python scripts/verify_module_api_dist.py verify \
  "${api_wheel}" "${api_sdist}" "${reference_wheel}"
```

The distribution verifier itself creates isolated environments through uv, so that final maintainer check is not yet a pip-only command.

While you're developing, run the smallest relevant test first:

```bash
.venv/bin/python -m pytest tests/test_storage.py -q
.venv/bin/python -m pytest tests/test_storage.py::test_fresh_database_uses_the_current_schema_version -q
```

Use `.venv/bin/ruff format <paths>` to format the files you changed, then run the full CI sequence before handoff. Tests use temporary databases and hand-written fakes, so they never need `.env.dev` or the live dev bot.

Offline harness evals replay recorded tool calls through cassettes; see
[evals.md](evals.md).


## Module lifecycle ceilings

Two settings bound how long a configured module may hold up startup or
shutdown; both default to values a well-behaved module never reaches.

```dotenv
MODULE_START_TIMEOUT_SECONDS=60   # start() past this fails the module and aborts startup
MODULE_CLOSE_TIMEOUT_SECONDS=15   # close() past this is cancelled; shutdown continues
```

A start timeout raises `Kimi module '<name>' start() exceeded 60s`, emits a `module_health` event with state `failed`, and the process exits like it would for any other module failure, so look at the log and the event to diagnose it. A close timeout logs `Kimi module <name> close() exceeded 15s; continuing shutdown` and the remaining modules still close. In both cases the module's coroutine is cancelled and given five seconds to stop; one that ignores cancellation is logged as abandoned and left to the event loop.

If a module trips either ceiling during development, the fix belongs in the module (move slow work into a scheduler job, or make `close()` cancel rather than await), not in the setting.

The module scheduler runs `MODULE_SCHEDULER_MAX_CONCURRENT_JOBS` (default 4) jobs concurrently, at most one per module. If a dev instance shares a database file with another running instance, the scheduler logs `Module scheduler paused: another scheduler runner holds the lease` and runs nothing until the other process stops (or its 60-second lease expires). The isolated dev setup above avoids this by giving each instance its own database.
