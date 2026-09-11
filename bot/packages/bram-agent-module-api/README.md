# Bram Agent Module API

The stable, host-independent contracts for building Bram application modules:
separately installed packages that add commands, LLM tools, background jobs,
event handlers, per-guild settings, and durable data to a Bram deployment.

This package contains no bot runtime, Discord client, database
implementation, or module loader. It exports:

- `ModuleSpec`, `ModuleLoadContext`, `ModuleRuntimeContext`: the declaration a
  module publishes and the two contexts the host hands it.
- `bram_agent_module_api.contracts`: every runtime port as a `typing.Protocol`
  (storage, scheduler, events, Discord actions, interactions, HTTP, services,
  trust, proposals, health) plus the validators the host runs at preflight.
- `bram_agent_module_api.events`: the normalized `discord.*` event payloads.
- `bram_agent_module_api.files`: `ToolFiles`, `ToolAttachment`, `ToolFile`, and
  `FileAccessError` for invocation-scoped attachment and workspace reads.
- `bram_agent_module_api.testing`: a fake for every port, `load_context()` for
  exercising `create()`, and `MemoryStorage` (install the `testing` extra) so a
  module can unit test itself with only this package installed.

A module exposes a `ModuleSpec` through the `bram_agent.modules` entry-point
group:

```toml
[project]
dependencies = ["bram-agent-module-api>=2,<3"]

[project.entry-points."bram_agent.modules"]
my_module = "my_module_package:SPEC"
```

The source must pin the API contract it implements when constructing the
specification:

```python
from bram_agent_module_api import ModuleSpec

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

The [module guide](https://github.com/bram-agent/bram-agent/blob/main/docs/modules.md)
documents installation, declarations, lifecycle, and every runtime port. The
[reference module](https://github.com/bram-agent/bram-agent/tree/main/bot/modules/example)
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

SDK 2.1 adds `ModuleToolContext.trigger_discord_message_id`. SDK 2.3 adds
`trigger_discord_message_snapshot`, an immutable host-owned capture of the
message, guild, channel, author, content, and bot status made at turn entry.
Mention-path tool calls receive both; personal and non-message surfaces receive
`None`. Modules that need authoritative evidence from the triggering message
should require `bram-agent-module-api>=2.3,<3` and use the snapshot instead of
re-fetching mutable or deletable Discord state.

Modules use namespaced guild documents and the physical table names returned
by `ctx.storage.table()`.

SDK 2.2 adds optional `ModuleToolContext.files`. Declare
`ModulePermissions(tool_files=True)`, depend on `bram-agent-module-api>=2.2,<3`,
and require `tools.files.v1` in `ModuleSpec.requires_capabilities`. Existing modules
keep `api_version=2` and receive `files=None` without the permission.
`ctx.files.attachments` lists admitted current/reply entries;
`read_attachment(id, max_bytes=...)` and `read_workspace(path, max_bytes=...)`
return bounded bytes without network downloads. The reader expires when the handler
returns and is scoped to the actual caller. `testing.FakeToolFiles` supports
independent tests. Module `ctx.http` methods apply an 8 MiB host ceiling even when
callers supply `max_bytes`; `download` buffers and validates the bounded response
before yielding chunks so connections are released on early consumer exit. See the
[file access guide](https://github.com/bram-agent/bram-agent/blob/main/docs/module-files.md)
for moderation, reply-image availability, privacy, and limits.

## Testing the SDK

From this package directory, run its tests without installing the Bram application:

```console
uv run --isolated --group test python -m pytest -q
```
