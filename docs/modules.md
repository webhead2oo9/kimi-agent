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
