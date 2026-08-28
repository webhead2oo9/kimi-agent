# Reference module: kudos

A complete, deliberately small Kimi application module. It lets members thank
each other, keeps a per-guild leaderboard, and posts a periodic digest. The
feature is an excuse: the point is that every file shows one part of the
module API in its real shape, with comments explaining *why* the host works
that way.

It is a starting point, not a dependency. Copy the layout, rename the package
and the entry point, and replace the behavior.

## What it demonstrates

| Surface | Where | What to look at |
|---|---|---|
| Entry point + `ModuleSpec` | `pyproject.toml`, `spec.py` | Discovery, preflight-validated declarations |
| Deployment settings | `settings.py` | `pydantic-settings` model, operator-exposed subset, env prefix |
| Per-guild settings | `guild_settings.py` | Typed schema, enum + bool + id fields, cross-field validator, `invalid_policy` |
| Migrations | `migrations.py` | Two ordered forward-only migrations on prefixed tables |
| Storage | `ledger.py` | `storage.table()`, reads on the shared connection, `write_transaction()` |
| LLM tools | `spec.py`, `module.py` | One core tool (`give_kudos`), one searchable (`kudos_leaderboard`), activity labels, guild-less refusal |
| Slash commands | `module.py` | `/kudos give`, `/kudos top`, staff-only `/kudos setup`, typed options |
| Persistent button | `module.py` | "Thank back" button that survives restarts via `custom_id` parts |
| Scheduler | `module.py` | Durable periodic `digest` job with per-guild failure isolation |
| Events | `module.py` | Subscribes to `discord.member_remove`; publishes `reference_kudos.given` |
| Discord actions | `module.py` | Declared `send_message` for the digest embed |
| Trust | `module.py` | `ctx.trust.tier()` against the guild's `giver_min_tier` |
| Services | `module.py` | Provides `kudos.board@1` for sibling modules |
| Proposals | `module.py` | `/kudos setup` proposes a guild document instead of writing config |
| Health | `module.py` | Metrics and `degraded` state after digest failures |
| Testing | `tests/` | Unit tests on the API fakes only; no host import |

Not used here: `ctx.http` (it needs a real declared host), `raw_bot` and
`raw_storage` (escape hatches a module should not need), `consumes`
(this module has no dependency), and `activation_capabilities`.

## Layout

```text
example/
├── pyproject.toml                          # metadata, entry point, dev tooling
├── README.md
├── LICENSE
├── src/community_agent_reference_module/
│   ├── __init__.py                         # exports SPEC, nothing else
│   ├── spec.py                             # ModuleSpec + create(): declarations and tool wiring
│   ├── settings.py                         # KudosSettings + operator-exposed fields
│   ├── guild_settings.py                   # per-guild schema and validator
│   ├── migrations.py                       # 001_create_kudos, 002_index_kudos
│   ├── ledger.py                           # all SQL, over the ModuleStorage port
│   ├── module.py                           # KudosModule: start/close and every handler
│   └── py.typed
└── tests/
    ├── conftest.py                         # in-memory storage + a started-module fixture
    ├── test_spec.py                        # declarations pass host preflight; create() wiring
    └── test_module.py                      # tools, commands, button, digest, events, service
```

Read the files in the order the host uses them: `spec.py` → `settings.py` →
`guild_settings.py` → `migrations.py` → `ledger.py` → `module.py`.

## How the host runs it

1. **Preflight.** The host loads `SPEC` from the entry point and validates its
   declarations before running any module code. A bad declaration aborts
   startup with a named reason.
2. **Settings.** The host builds `KudosSettings` from the environment and the
   dotenv, then overlays the fields listed as `exposed` from
   `<CONFIG_DIR>/modules/reference_kudos.md`.
3. **Load.** `create()` receives the prepared settings and the tool registry,
   registers the two LLM tools, and returns a `KudosModule`.
4. **Migrate.** Migrations run in order against the module's prefixed tables.
   The host records each applied name in `module_schema_versions`.
5. **Start.** `start()` receives the `ModuleRuntimeContext` and registers the
   digest job, the event subscription, the commands, the button, and the
   service. Raising here aborts startup.
6. **Close.** On shutdown, registrations are released newest-first.

## Try it

From `bot/`, install the workspace and enable the entry point in `.env`:

```console
uv sync --all-packages --extra dev
```

```dotenv
KIMI_MODULES=reference_kudos
```

Then run the bot normally. In a server, ask the bot to give someone kudos,
run `/kudos top`, or have staff run `/kudos setup #channel` and approve the
proposal that appears. Optional overrides:

```dotenv
REFERENCE_KUDOS_DAILY_LIMIT=3
REFERENCE_KUDOS_DIGEST_INTERVAL_SECONDS=86400
```

or the same fields, without the prefix, as frontmatter in
`<CONFIG_DIR>/modules/reference_kudos.md`.

## Make it yours

1. Copy this directory out of the repository.
2. Rename the package directory, the distribution name in `pyproject.toml`,
   and the entry-point name. The module name must match `ModuleSpec.name`
   and `ModuleSettingsDefinition.name`; it prefixes every table and the
   module's event namespace.
3. Depend on `kimi-agent-module-api` from PyPI instead of the workspace.
4. Replace the behavior. Keep the split: declarations in `spec.py`, SQL in
   one place, business rules in one method shared by every entry point.
5. Write your own privacy note. A module that stores member data (this one
   stores user ids and free-text reasons) must say what it keeps and for how
   long, and the operator must link that from the deployment's privacy notice.

Once separated, the package checks itself with only the API installed:

```console
uv sync --extra dev
uv run ruff check .
uv run mypy .
uv run pytest
```

## Data this module keeps

One row per kudos: guild id, giver id, receiver id, the reason text, and a
timestamp. Rows for a member are deleted when the host reports that they left
the guild. Nothing leaves the host process; the digest is posted only to the
channel a guild's staff approved.
