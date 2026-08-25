# Application modules

Kimi Agent is a complete LLM bot without optional application modules. Modules
are separately installed Python packages for capabilities that own more than a
single tool: Discord commands/listeners, database tables, background work,
guild configuration, and optionally model-callable tools.

Installed packages advertise a `ModuleSpec` through the
`kimi_agent.modules` entry-point group. Installation alone does nothing. The
operator explicitly activates packages with a comma-separated list:

```dotenv
KIMI_MODULES=community_moderation,image_fingerprints
```

Configured modules are part of the deployment contract. Startup fails if one
is missing, has an incompatible module API, has an inactive dependency, has
invalid settings, or fails to start. Dependencies start first and modules close
in reverse order. An empty `KIMI_MODULES` loads no module code or schema.

## Package and schema contract

- Pin module distributions in deployment-owned requirements/lock data. Do not
  add optional packages to the core lock file.
- Each module has its own version and independent ordered migration list. Core
  stores applied versions in `module_schema_versions`; module tables are absent
  until that module starts.
- A module can depend on another named module. Every dependency must also be
  present in `KIMI_MODULES`; dependencies are not activated implicitly.
- Module settings use the same selected dotenv as core. Explicitly exposed,
  non-secret operator overrides live under
  `<CONFIG_DIR>/modules/<module_name>.md`.
- Modules can register normal tools on the shared registry and declare their
  activity labels/evaluation surfaces. This is optional; a command/listener-only
  module need not expose anything to the LLM.

For a tagged Git deployment, keep module requirements outside the core lock:

```text
kimi-agent-community-moderation @ git+ssh://git@github.com/webhead2oo9/kimi-agent-modules.git@community-moderation-v0.1.0#subdirectory=packages/community-moderation
kimi-agent-image-fingerprints @ git+ssh://git@github.com/webhead2oo9/kimi-agent-modules.git@image-fingerprints-v0.1.0#subdirectory=packages/image-fingerprints
```

Install that deployment-owned file after each core sync, or layer it at launch
with `uv run --with-requirements modules.lock python bot.py`. Private Git access
must be provisioned on the host; local versioned wheels are an equivalent
offline deployment source.

The companion repository is
[`webhead2oo9/kimi-agent-modules`](https://github.com/webhead2oo9/kimi-agent-modules).
It currently contains `community_moderation` and the dependent
`image_fingerprints` package. It is private during initial development; module
configuration, privacy, and FingerPrint Hub deployment details live with those
packages.

## Modules versus operator plugins

Use an application module for a required, versioned capability with lifecycle
or schema. Use an [operator plugin](plugins.md) for a best-effort,
deployment-local LLM tool extension. A broken configured module aborts startup;
a broken plugin is logged, rolled back, and skipped.
