# Application modules

Application modules are separately installed Python packages. Core does not
ship a catalog of them, download them at runtime, or test repositories that
happen to contain them. An operator chooses which distributions to install and
which installed entry points to activate.

Use a module when an extension needs lifecycle hooks, migrations, durable data,
events, background jobs, Discord interactions, or host services. If it only
registers LLM tools for one deployment, an [operator plugin](plugins.md) is the
smaller interface.

| | Operator plugin | Application module |
|---|---|---|
| Selected by | Import path in `PLUGIN_MODULES` | Entry-point name in `KIMI_MODULES` |
| Discovery | Direct Python import | Installed `community_agent.modules` entry point |
| Failure | Logged and skipped | Aborts startup |
| Best for | A few deployment-owned tools | A versioned application capability with state/lifecycle |
| Packaging | Any importable code | Installable Python distribution |

## Try the reference module

This repository maintains one example at `bot/examples/reference-module`. It
is executable documentation and a CI fixture, not a production dependency or a
default-enabled feature.

From `bot/`:

```console
uv sync --all-packages --extra dev
```

Then set the installed entry-point name and start normally:

```dotenv
KIMI_MODULES=reference_greeter
```

The example owns one exposed greeting setting, one `reference_greet` LLM tool,
one scoped migration with a persistent invocation count, and normal
`start()`/`close()` lifecycle hooks. Copy its package structure to begin a
module of your own; it is intentionally small enough to replace rather than
inherit.

## Attach any module package

A module distribution depends on the standalone
`community-agent-module-api` package, exposes a `ModuleSpec`, and advertises it
in `pyproject.toml`:

```toml
[project]
name = "my-assistant-module"
version = "0.1.0"
dependencies = ["community-agent-module-api>=1,<2"]

[project.entry-points."community_agent.modules"]
my_module = "my_assistant_module:SPEC"
```

Install it using whatever source your deployment controls—a local path, wheel,
private Git repository, or package index—then add `my_module` to
`KIMI_MODULES`. A local checkout does not need publishing:

```console
uv pip install -e ../my-assistant-module
```

```dotenv
KIMI_MODULES=my_module
```

Installing a distribution alone never activates it. Core does not scan a
folder and never auto-installs a configured name. Once a name is active it is
part of the deployment contract, so startup fails if it is missing, has an
incompatible API, has an inactive dependency, has invalid settings, or fails
to start. Dependencies start first and modules close in reverse order.

An empty `KIMI_MODULES` does not import module entry points or run module
migrations. Existing module tables remain in the shared database while their
modules are disabled or absent; disabling is not data deletion.

A module may separately declare `activation_capabilities` for an optional
feature that is meaningful only when core is configured to expose it. Missing
activation capabilities soft-disable that module (and its dependents) without
creating it, running migrations, or aborting bot startup; `/modules status`
shows the reason. `requires_capabilities` remains a hard compatibility check.

## Package and schema contract

- Pin third-party module distributions in deployment-owned requirements or
  lock data. Do not add them to the core lock file; the in-repository reference
  module is a workspace-only CI fixture.
- Each module has its own version and independent, ordered, forward-only
  migrations. Core records applied versions in `module_schema_versions`.
  Migrations create or update tables when the module starts; those tables remain
  while the module is disabled or absent.
- A module can depend on another named module. Every dependency must also be
  present in `KIMI_MODULES`, because dependencies are never activated
  implicitly.
- Module settings use the same selected dotenv as the core. Explicitly exposed,
  non-secret operator overrides live under
  `<CONFIG_DIR>/modules/<module_name>.md`.
- A module's runtime context is the only thing it needs from core; the
  `community_agent_module_api` package exports contracts, event dataclasses,
  image helpers, and test fakes, never core implementation types.
- A module can register ordinary tools on the shared registry and declare its
  activity labels and evaluation surfaces. This is optional; a module that only
  provides commands or listeners need not expose anything to the LLM.

For repeatable deployments, keep third-party module requirements in
deployment-owned lock data and install them after the core sync. Private Git
access or a local wheelhouse works without involving core CI. Each module owns
its release process, tests, configuration docs, and privacy disclosures.
Before activating a module, the operator must make those disclosures reachable
from the deployment's public privacy notice. A module that observes events or
content not addressed to the bot must say so explicitly, including what it
processes, why, where it sends data, and how long it retains the result.

## Publishing the API

Publishing the API lets module authors depend on a small, neutral wheel instead
of cloning this application. Publishing example modules is unnecessary: they
are templates, while real modules belong to their own maintainers.

The SDK source is `bot/packages/community-agent-module-api`, versioned from
`1.0.0` with `MODULE_API_VERSION = 1`. Tags named
`community-agent-api-v<version>` run the tag-only release workflow. It verifies
the tag/version match, tests the workspace, builds with workspace sources
disabled, imports the wheel in an isolated environment, and publishes using a
PyPI Trusted Publisher—there is no long-lived PyPI token in GitHub.

Before the first tag, reserve the `community-agent-module-api` project through
PyPI's pending-publisher flow and configure this repository, workflow
`release-community-agent-api.yml`, environment `pypi`. A name lookup is not a
reservation, so confirm availability again immediately before the first
release.

## Declarations

A `ModuleSpec` can declare what the module intends to use. Declarations are
validated when the module set is preflighted, before any module code runs, so
a malformed declaration aborts startup with a named reason:

- `permissions.discord_actions`: the Discord operations the module will call
  (`send_message`, `send_dm`, `edit_message`, `delete_message`, `ban`, `kick`,
  `timeout`, `fetch_message`, `fetch_member`, `fetch_channel`, `fetch_messages`,
  `fetch_pins`, `fetch_public_threads`, `check_channel_access`).
- `permissions.event_topics`: core (`discord.*`) or sibling-module topics the
  module subscribes to. A module never declares its own namespace; it may
  publish only under `<module_name>.*`.
- `permissions.http_hosts`: exact outbound hosts, the `discord-cdn` token, or
  `${setting_name}` resolved from the module's settings. Wildcards are not
  accepted.
- `provides` / `consumes`: exact `(name, version)` services. A consumed service
  must come from a module listed in `dependencies`.
- `guild_settings`: a typed per-guild schema whose `invalid_policy` defaults to
  `disable_guild`, so an enforcement module with a broken guild document fails
  closed.

The rules live in `community_agent_module_api.contracts`, which imports only the
standard library so a package can validate its own declarations in tests.
Every declaration is enforced by the matching runtime service below.

## Runtime services

Each started module receives its own frozen `ModuleRuntimeContext`: its
`module_name`, `is_guild_active`, `current_config_dir()`, `capabilities`,
and one port per service. A module never receives the Discord client, the
database, or another module's object; `raw_bot` and `raw_storage` are
populated only for a module whose permissions declare them, and the owner
manifest lists those escape hatches. Modules are trusted, in-process code:
the ports are a contract and an audit surface, not a sandbox.

- `ctx.storage`: the shared database seen through the module's table prefix.
  `ctx.storage.table("cases")` returns the quoted physical name
  `"<module>_cases"`, or the aliased name when the spec declares a
  `table_aliases` entry. A module that defines `scoped_migrations` gets a
  `MigrationContext` with the same `table()` helper instead of a raw
  connection. This is naming discipline on one shared connection, not SQL
  isolation; every writer still goes through `write_transaction()`.
- `ctx.health.report(state, detail, metrics)`: `starting`, `healthy`,
  `degraded`, or `failed`. Core sets `starting` before `start()`, `failed`
  (and aborts startup) if `start()` raises, and `healthy` after a clean
  return unless the module already reported otherwise. A module that
  declares a service in `provides` but never provides it is marked
  `degraded`. Detail is truncated, metrics are capped and secret-looking
  keys dropped. Each change is emitted as a best-effort `module_health`
  event when the event log is enabled.
- `ctx.services.provide(name, version, impl)` / `ctx.services.get(name,
  version)`: exact-version services between modules. Both must match the
  spec's `provides` / `consumes`; a consumer must depend on the provider so
  it starts later. `get` returns a proxy that raises `ServiceUnavailable`
  once the provider closes.

- `ctx.events.publish(topic, payload)` / `ctx.events.subscribe(pattern,
  handler)`: an in-process bus. A module publishes only under
  `<module_name>.*`; subscribing to `discord.*` or a sibling's topics
  requires the topic (or `<namespace>.*`) in `permissions.event_topics`.
  Core publishes normalized `discord.message`, `discord.message_edit`,
  `discord.message_delete`, `discord.member_join`, `discord.member_remove`,
  `discord.member_update`, and `discord.audit_log_entry` events
  (`community_agent_module_api.events`) carrying IDs and whatever cannot be
  re-fetched, never SDK objects. `publish` returns immediately; each
  subscriber module has a bounded queue and a small worker pool with a
  per-handler timeout, failures are logged and counted in that module's
  health metrics, and a full queue drops its oldest pending event. Events
  are lost on restart; durable work belongs in the scheduler.
- `ctx.discord`: the declared Discord operations (`send_message`,
  `send_dm`, `edit_message`, `delete_message`, `ban`, `kick`, `timeout`,
  `fetch_message`, `fetch_member`, `fetch_channel`, paginated
  `fetch_messages`, `fetch_pins`, `fetch_public_threads`, and
  `check_channel_access`) on stable IDs, returning public
  snapshots. Calling an undeclared action raises
  `UndeclaredDiscordAction`. `ban`, `kick`, and `timeout` refuse the bot,
  the acting user, other bots, and any member whose trust tier is not
  below the actor's, unless the spec sets
  `permissions.override_target_policy`. Every action is scoped to guilds
  core considers active. A `MessageRef` cannot be reused across guilds: core
  checks the channel's guild before asking Discord to fetch or delete the
  message. Discord caps audit-log reasons at 512 characters, so that limit
  includes the module prefix and core keeps the beginning of your reason (where
  correlation markers normally live). Outgoing messages never ping.
- `ctx.guild_settings`: the module's typed per-guild settings, declared as
  `ModuleSpec.guild_settings` (fields of kind `int`, `id`, `id_list`, `str`,
  `str_list`, `enum`, `bool`, plus an optional validator). Values live in
  `<CONFIG_DIR>/guild-modules/<guild_id>/<module_name>.md` (frontmatter
  only). These documents are parsed strictly. Broken frontmatter markers or
  invalid YAML make the snapshot invalid instead of quietly behaving like
  empty settings. Until a guild has that document, the schema's field names are
  read from `servers/<guild_id>.md` and the snapshot reports
  `legacy=True`, with the module marked `degraded` naming those guilds.
  `guild_ids()` lists the active guilds known to this module, and
  `get(guild_id)` returns a cached snapshot (`values`, `valid`, `errors`,
  `revision`), refreshed on the guild-activation cadence and immediately after
  an approved proposal; `is_enabled(guild_id)` is the guild being active and
  the document valid; `on_change(callback)` fires per changed guild. An
  invalid document follows the schema's `invalid_policy`: `disable_module`
  turns the module off for that guild, `disable_guild` (the default) takes
  the guild out of the bot's active set until it is fixed. Staff can propose
  replacements through `guild:<guild_id>:<module_name>` and approve them in Discord.
- `ctx.scheduler`: durable jobs. `register(name, handler)` in `start()`
  binds a handler by name; `run_at(key, when, name, payload)` and
  `run_every(key, interval, name, payload, jitter_seconds=, backoff=)`
  persist the job in `module_scheduler_jobs` (unique per module and key;
  scheduling the same key again replaces it). One runner claims due jobs
  by taking a lease, heartbeats it while the handler runs, deletes a
  one-shot job on success, reschedules a periodic one from completion plus
  jitter, and backs a failing one off. A live lease is never run twice; an
  expired lease from a crashed process is claimed again. A persisted job
  whose handler is not registered stays paused and marks the module
  `degraded`. Kimi runs one process; there is no multi-node coordination.
- `ctx.interactions`: slash commands and persistent components without the
  Discord SDK. `add_command(CommandSpec, handler)` builds the app command
  (top-level or one group level; `string`/`integer`/`boolean`/`user`/
  `channel`/`role` options with choices, bounds, and autocomplete), gates
  it by the spec's `min_tier` before the handler runs, and hands the handler
  a `ModuleInteraction` whose option values are stable IDs. `respond`,
  `defer`, `edit_original`, and `follow_up` never ping. Buttons and selects
  are `ButtonSpec`/`SelectSpec` values whose custom IDs are
  `m:<module>:<key>:...`. Components default to member access. If a button or
  select can do privileged work, register it with `min_tier="regular"` or
  `min_tier="staff"`; core checks the person who actually clicked it and
  rejects unauthorized clicks ephemerally before your handler runs. You can
  also give a registration an expiry. One persistent dispatcher routes clicks,
  so a button still works after a restart as long as the module re-registers
  its key in `start()`. Registrations are removed when the module closes; core
  syncs the command tree once per READY.
  Not supported: modals, attachment/number option kinds, context menus,
  per-guild command scoping, localization.
- `ctx.http`: outbound HTTP limited to the hosts in
  `permissions.http_hosts`. A rule names an exact host, the `discord-cdn`
  token, or `${setting_name}` resolved from the module's settings at load
  (a bare host or a URL, whose scheme and port then apply), plus allowed
  schemes, ports, and a `network` policy. `public` hosts resolve through
  the same public-only resolver as URL downloads, so DNS can never land on
  a private or metadata address; `private` allows exactly that host, which
  is how an owner points a module at a self-hosted service. `get`,
  `post_json` (no retry), and streaming `download` re-check every redirect
  hop, cap bodies while streaming, bound timeouts, and never put headers or
  credentials in errors. Wildcard hosts are not supported.
- `ctx.trust.tier(guild_id, user_id)`: read-only trust lookup (`member`,
  `regular`, `staff`) for modules that keep their own protections.
- `ctx.proposals`: a `proposals.v2` port already bound to this module. Its
  `snapshot`, `propose`, and `get` methods require a `ProposalActor` and enforce
  that reads, status, targets, and the review channel all belong to the actor's
  guild. Supported targets are `guild:<id>`, `channel:<id>`, and
  `guild:<id>:<module>`. Settings, models, prompts, tool policy,
  deployment-wide module/plugin configuration, and secrets are excluded.

The bot owner can inspect all of this with `/modules status` (health per
module) and `/modules manifest` (every declaration, including escape
hatches such as `raw_bot`).

## Testing a module

`community_agent_module_api.testing` ships protocol-level fakes for every service
port (`FakeEvents`, `FakeScheduler`, `FakeDiscordActions`, `FakeInteraction`,
`FakeHttp`, `FakeProposals`, and friends). They import nothing beyond the standard library and
the contracts, so a module can unit-test its logic with only the API package
installed. `FakeDiscordActions` enforces the module's declared actions,
`FakeInteractions.component_min_tiers[(kind, key)]` lets tests check a
component's access tier, and `FakeScheduler.run_due(now)` runs jobs only when
the test advances time.

For composition tests, core's `modules.testing.build_test_runtime(tmp_path,
names)` loads the named modules through the real `ModuleManager`, applies their
migrations to a fresh SQLite file, and starts them with a per-module context
whose ports are the fakes above. `runtime.ctx_for(name)` returns a module's
context, `runtime.ports[name]` its fakes, and `runtime.registry` the composed
tool registry for assertions. Module test suites
may import that harness; module production source may import only
`community_agent_module_api`.

## Configuration proposals

Core always advertises `proposals.v2`. A module receives a module-bound port,
so it cannot attribute a proposal to another installed module. Snapshot and
status reads are actor-scoped just like writes. Creation records the exact live
fragment as a rollback baseline and its SHA-256 revision; approval refuses to
clobber a later operator edit even when the caller omitted an expected
revision. The review card goes to the guild's `proposal_channel_id` or the
invoking channel, after core proves that channel belongs to the same guild.
Persistent Approve/Reject buttons require staff tier and survive restarts.

Approval writes only below `CONFIG_DIR`, refreshes guild activation and module
guild settings, and rolls back to the recorded baseline if the candidate makes
the guild invalid. The stored baseline and proposed-content hash also let a
retry reconcile a process interruption without an eight-state workflow.

## Modules versus operator plugins

Reach for an application module when you need a required, versioned capability
with a lifecycle or a schema. Reach for an [operator plugin](plugins.md) when
you want a best-effort, deployment-local LLM tool extension. The failure modes
follow from that split: a broken configured module aborts startup, while a
broken plugin is logged, rolled back, and skipped.
