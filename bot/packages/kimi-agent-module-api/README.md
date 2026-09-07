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
- `kimi_agent_module_api.files`: `ToolFiles`, `ToolAttachment`, `ToolFile`, and
  `FileAccessError` for invocation-scoped attachment and workspace reads.
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

Modules using guild-scoped live command replacement through
`InteractionRouter.replace_guild_commands()` should require the host capability
`discord.guild_commands.v1`.

The SDK provides typed modal forms and a narrow Components V2 layout model. Once a response uses
that layout model, Discord requires every later edit of the same message to remain a layout.
Modules using them should require
`discord.modals.v1` and/or `discord.components_v2.v1`.

Message-deletion events include cached author classification:
`MessageDeleteEvent.author_is_bot` and `MessageBulkDeleteEvent.bot_message_ids`.
The values remain unknown for messages that were absent from Discord's cache.

SDK 2.1 adds `ModuleToolContext.trigger_discord_message_id`. Mention-path tool
calls receive the exact source Discord message snowflake; other surfaces receive
`None`. Modules that act on a user's source message should require
`kimi-agent-module-api>=2.1,<3`, reject `None`, fetch that exact message through
`ctx.discord`, and verify its author before acting.

Modules use namespaced guild documents and the physical table names returned
by `ctx.storage.table()`.

SDK 2.2 adds optional `ModuleToolContext.files`. Declare
`ModulePermissions(tool_files=True)`, depend on `kimi-agent-module-api>=2.2,<3`,
and require `tools.files.v1` in `ModuleSpec.requires_capabilities`. Existing modules
keep `api_version=2` and receive `files=None` without the permission.
`ctx.files.attachments` lists admitted current/reply entries;
`read_attachment(id, max_bytes=...)` and `read_workspace(path, max_bytes=...)`
return bounded bytes without network downloads. The reader expires when the handler
returns and is scoped to the actual caller. `testing.FakeToolFiles` supports
independent tests. See the
[file access guide](https://github.com/webhead2oo9/kimi-agent/blob/main/docs/module-files.md)
for moderation, reply-image availability, privacy, and limits.

## Testing the SDK

From this package directory, run its tests without installing the Kimi application:

```console
uv run --isolated --group test python -m pytest -q
```
