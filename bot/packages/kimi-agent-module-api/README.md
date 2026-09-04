# Kimi Agent Module API

The stable, host-independent contracts for building Kimi application modules:
separately installed packages that add commands, LLM tools, background jobs,
event handlers, per-guild settings, and durable data to a Kimi deployment.

This package contains no bot runtime, Discord client, database
implementation, or module loader. It exports:

- `ModuleSpec`, `ModuleLoadContext`, `ModuleRuntimeContext`: the declaration a
  module publishes and the two contexts the host hands it.
- `kimi_agent_module_api.contracts`: every runtime port as a `typing.Protocol`
  (storage, scheduler, events, Discord actions, interactions, HTTP, services,
  trust, proposals, health) plus the validators the host runs at preflight.
- `kimi_agent_module_api.events`: the normalized `discord.*` event payloads.
- `kimi_agent_module_api.testing`: a fake for every port, `load_context()` for
  exercising `create()`, and `MemoryStorage` (install the `testing` extra) so a
  module can unit test itself with only this package installed.

A module exposes a `ModuleSpec` through the `kimi_agent.modules` entry-point
group:

```toml
[project]
dependencies = ["kimi-agent-module-api>=2,<3"]

[project.entry-points."kimi_agent.modules"]
my_module = "my_module_package:SPEC"
```

The source must pin the API contract it implements when constructing the
specification:

```python
from kimi_agent_module_api import ModuleSpec

SPEC = ModuleSpec(
    name="my_module",
    version="0.1.0",
    create=create,
    api_version=2,
)
```

`api_version` is a required keyword. Keep it as a literal rather than deriving
it from the installed SDK's `MODULE_API_VERSION`; unchanged module source must
not silently claim compatibility merely because it was rebuilt with a newer
SDK.

The [module guide](https://github.com/webhead2oo9/kimi-agent/blob/main/docs/modules.md)
documents installation, declarations, lifecycle, and every runtime port. The
[reference module](https://github.com/webhead2oo9/kimi-agent/tree/main/bot/modules/example)
is a complete, commented example that exercises most ports; start there.

Guild-scoped live command replacement was added in 1.1. Modules using
`InteractionRouter.replace_guild_commands()` should require the host capability
`discord.guild_commands.v1`.

Version 1.2 adds typed modal forms and a narrow Components V2 layout model. Once a response uses
that layout model, Discord requires every later edit of the same message to remain a layout.
Modules using them should require
`discord.modals.v1` and/or `discord.components_v2.v1`.

Version 1.3 adds cached author classification to message-deletion events:
`MessageDeleteEvent.author_is_bot` and `MessageBulkDeleteEvent.bot_message_ids`.
The values remain unknown for messages that were absent from Discord's cache.

Version 2 requires an explicit, source-pinned `ModuleSpec.api_version`, and
removes the temporary guild-settings legacy flag and module table aliases.
Modules must use namespaced guild documents and migrate legacy tables to the
physical names returned by `ctx.storage.table()` before upgrading.

## Testing the SDK

From this package directory, run its tests without installing the Kimi application:

```console
uv run --isolated --group test python -m pytest -q
```
