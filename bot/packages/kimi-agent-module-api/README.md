# Kimi Agent Module API

The stable, host-independent contracts for building Kimi application modules:
separately installed packages that add commands, LLM tools, background jobs,
event handlers, per-guild settings, and durable data to a Kimi deployment.

This package deliberately contains no bot runtime, Discord client, database
implementation, or module loader. It exports:

- `ModuleSpec`, `ModuleLoadContext`, `ModuleRuntimeContext`: the declaration a
  module publishes and the two contexts the host hands it.
- `kimi_agent_module_api.contracts`: every runtime port as a `typing.Protocol`
  (storage, scheduler, events, Discord actions, interactions, HTTP, services,
  trust, proposals, health) plus the validators the host runs at preflight.
- `kimi_agent_module_api.events`: the normalized `discord.*` event payloads.
- `kimi_agent_module_api.testing`: a fake for every port so a module can unit
  test itself with only this package installed.

A module exposes a `ModuleSpec` through the `kimi_agent.modules` entry-point
group:

```toml
[project]
dependencies = ["kimi-agent-module-api>=1,<2"]

[project.entry-points."kimi_agent.modules"]
my_module = "my_module_package:SPEC"
```

The main repository's reference module (`bot/modules/example`) is a complete,
commented example that exercises every port; start there.
