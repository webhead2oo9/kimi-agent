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
- A module can register ordinary tools on the shared registry and declare its
  activity labels and evaluation surfaces. This is optional; a module that only
  provides commands or listeners need not expose anything to the LLM.

For a tagged Git deployment, keep the module requirements outside the core
lock:

```text
kimi-agent-community-moderation @ git+ssh://git@github.com/webhead2oo9/kimi-agent-modules.git@community-moderation-v0.1.0#subdirectory=packages/community-moderation
kimi-agent-image-fingerprints @ git+ssh://git@github.com/webhead2oo9/kimi-agent-modules.git@image-fingerprints-v0.1.0#subdirectory=packages/image-fingerprints
```

Install that deployment-owned file after each core sync, or layer it on at
launch with `uv run --with-requirements modules.lock python bot.py`. Private
Git access has to be provisioned on the host; local versioned wheels are an
equivalent source if you prefer an offline deployment.

The companion repository is
[`webhead2oo9/kimi-agent-modules`](https://github.com/webhead2oo9/kimi-agent-modules).
It currently contains `community_moderation`, the dependent
`image_fingerprints` package, and the experimental `config_admin` proposal
module. It is private during initial development, and
each package's configuration, privacy, and FingerPrint Hub deployment details
live alongside that package rather than here.

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
standard library so a package can validate its own declarations in tests. The
services these declarations govern land incrementally as optional fields on
the runtime context; until they do, declarations are validated but not yet
enforced.

## Runtime services

Each started module receives its own frozen `ModuleRuntimeContext` carrying
`module_name` and the service ports core has implemented so far:

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

The bot owner can inspect all of this with `/modules status` (health per
module) and `/modules manifest` (every declaration, including escape
hatches such as `raw_bot`).

## Testing a module

`kimi_agent_module_api.testing` ships protocol-level fakes for every service
port (`FakeEvents`, `FakeScheduler`, `FakeDiscordActions`, `FakeInteraction`,
`FakeHttp`, and friends). They import nothing beyond the standard library and
the contracts, so a module can unit-test its logic with only the API package
installed. `FakeDiscordActions` enforces the module's declared actions, and
`FakeScheduler.run_due(now)` runs jobs only when the test advances time.

For composition tests, core's `modules.testing.build_test_runtime(tmp_path,
names)` loads the named modules through the real `ModuleManager`, applies their
migrations to a fresh SQLite file, and starts them with a per-module context
whose ports are the fakes above. `runtime.ctx_for(name)` returns a module's
context and `runtime.ports[name]` its fakes for assertions. Module test suites
may import that harness; module production source may import only
`kimi_agent_module_api`.

## Experimental control-plane API

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
