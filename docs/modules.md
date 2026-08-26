# Application modules

Kimi Agent is a complete LLM bot on its own; application modules are optional.
A module is a separately installed Python package for a capability that owns
more than a single tool: Discord commands and listeners, database tables,
background work, guild configuration, and optionally model-callable tools.

An installed package advertises a `ModuleSpec` through the
`kimi_agent.modules` entry-point group, but installing it does nothing by
itself. You activate packages explicitly with a comma-separated list:

```dotenv
KIMI_MODULES=community_moderation,image_fingerprints
```

Once a module is on that list it is part of the deployment contract, so
startup fails if it is missing, has an incompatible module API, has a
dependency that is not active, has invalid settings, or fails to start.
Dependencies start first, and modules close in reverse order. An empty
`KIMI_MODULES` loads no module code and no module schema.

## Package and schema contract

- Pin module distributions in deployment-owned requirements or lock data. Do
  not add optional packages to the core lock file.
- Each module has its own version and its own independent, ordered migration
  list. The core stores applied versions in `module_schema_versions`, and a
  module's tables are absent until that module starts.
- A module can depend on another named module. Every dependency must also be
  present in `KIMI_MODULES`, because dependencies are never activated
  implicitly.
- Module settings use the same selected dotenv as the core. Explicitly exposed,
  non-secret operator overrides live under
  `<CONFIG_DIR>/modules/<module_name>.md`.
- A module's runtime context is the only thing it needs from core; the
  `kimi_agent_module_api` package exports contracts, event dataclasses,
  image helpers, and test fakes, never core implementation types.
- A module can register ordinary tools on the shared registry and declare its
  activity labels and evaluation surfaces. This is optional; a module that only
  provides commands or listeners need not expose anything to the LLM.

For a tagged Git deployment, keep the module requirements outside the core
lock:

```text
kimi-agent-community-moderation @ git+ssh://git@github.com/webhead2oo9/kimi-agent-modules.git@community-moderation-v0.2.0#subdirectory=packages/community-moderation
kimi-agent-image-fingerprints @ git+ssh://git@github.com/webhead2oo9/kimi-agent-modules.git@image-fingerprints-v0.2.0#subdirectory=packages/image-fingerprints
```

Install that deployment-owned file after each core sync, or layer it on at
launch with `uv run --with-requirements modules.lock python bot.py`. Private
Git access has to be provisioned on the host; local versioned wheels are an
equivalent source if you prefer an offline deployment.

The modules Kimi ships with live in the companion repository,
[`webhead2oo9/kimi-agent-modules`](https://github.com/webhead2oo9/kimi-agent-modules):
`community_moderation` (staff cases, `/mod`, and the moderation log),
`image_fingerprints` (known-bad image enforcement, which depends on
`community_moderation`), and `config_admin` (staff-facing proposal tools for
the control plane). Each package documents its own configuration, privacy
posture, and any external service it talks to, so this page stays about the
contract they share.

## Declarations

A `ModuleSpec` can declare what the module intends to use. Declarations are
validated when the module set is preflighted, before any module code runs, so
a malformed declaration aborts startup with a named reason:

- `permissions.discord_actions`: the Discord operations the module will call
  (`send_message`, `send_dm`, `edit_message`, `delete_message`, `ban`, `kick`,
  `timeout`, `fetch_message`, `fetch_member`).
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

The rules live in `kimi_agent_module_api.contracts`, which imports only the
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
  `"<module>_cases"`, or the legacy name when the spec declares a
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
  keys dropped; every change is a `module_health` observability event.
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
  (`kimi_agent_module_api.events`) carrying IDs and whatever cannot be
  re-fetched, never SDK objects. `publish` returns immediately; each
  subscriber module has a bounded queue and a small worker pool with a
  per-handler timeout, failures are logged and counted in that module's
  health metrics, and a full queue drops its oldest pending event. Events
  are lost on restart; durable work belongs in the scheduler.
- `ctx.discord`: the declared Discord operations (`send_message`,
  `send_dm`, `edit_message`, `delete_message`, `ban`, `kick`, `timeout`,
  `fetch_message`, `fetch_member`) on stable IDs, returning public
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
  `get(guild_id)` returns a cached snapshot (`values`, `valid`, `errors`,
  `revision`), refreshed on the guild-activation cadence and on managed
  config activation; `is_enabled(guild_id)` is the guild being active and
  the document valid; `on_change(callback)` fires per changed guild. An
  invalid document follows the schema's `invalid_policy`: `disable_module`
  turns the module off for that guild, `disable_guild` (the default) takes
  the guild out of the bot's active set until it is fixed. The owner edits
  these documents through `guild:<guild_id>:<module_name>` proposals.
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

The bot owner can inspect all of this with `/modules status` (health per
module) and `/modules manifest` (every declaration, including escape
hatches such as `raw_bot`).

## Testing a module

`kimi_agent_module_api.testing` ships protocol-level fakes for every service
port (`FakeEvents`, `FakeScheduler`, `FakeDiscordActions`, `FakeInteraction`,
`FakeHttp`, and friends). They import nothing beyond the standard library and
the contracts, so a module can unit-test its logic with only the API package
installed. `FakeDiscordActions` enforces the module's declared actions,
`FakeInteractions.component_min_tiers[(kind, key)]` lets tests check a
component's access tier, and `FakeScheduler.run_due(now)` runs jobs only when
the test advances time.

For composition tests, core's `modules.testing.build_test_runtime(tmp_path,
names)` loads the named modules through the real `ModuleManager`, applies their
migrations to a fresh SQLite file, and starts them with a per-module context
whose ports are the fakes above. `runtime.ctx_for(name)` returns a module's
context and `runtime.ports[name]` its fakes for assertions. Module test suites
may import that harness; module production source may import only
`kimi_agent_module_api`.

## Control-plane API

The module API can advertise `proposals.v1`, `config.v1`, and `restart.v1` when
the control plane is enabled. A trusted module may inspect redacted managed
configuration, create a durable proposal, or register an action in its own
namespace. Only the configured bot owner can approve a proposal. See
[module-control-plane.md](module-control-plane.md) for activation, restart,
rollback, and the deliberately excluded operations.

## Modules versus operator plugins

Reach for an application module when you need a required, versioned capability
with a lifecycle or a schema. Reach for an [operator plugin](plugins.md) when
you want a best-effort, deployment-local LLM tool extension. The failure modes
follow from that split: a broken configured module aborts startup, while a
broken plugin is logged, rolled back, and skipped.
