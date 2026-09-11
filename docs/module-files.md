# Files in module tools

Module API 2.2 adds `ModuleToolContext.files`: read-only access to admitted
attachments and the current caller's workspace. It works in guild chat and, for
tools registered with `guild_only=False`, personal chat. The host supplies identity
and workspace scope; modules never accept a user ID to choose whose files to read.

## Declare access

Depend on `bram-agent-module-api>=2.2,<3`, keep `api_version=2`, and require the
host capability `tools.files.v1`. Installing a newer SDK does not add this service
to an older host.

```python
from bram_agent_module_api import ModulePermissions, ModuleSpec

SPEC = ModuleSpec(
    name="my_media",
    version="1.0.0",
    api_version=2,
    create=create,
    requires_capabilities=("tools.files.v1",),
    permissions=ModulePermissions(tool_files=True),
)
```

`/modules manifest` lists the permission. Without it, `ctx.files` is `None`, even
if other modules have access. A host without a configured reader rejects modules
declaring this permission during loading. Existing modules keep their behavior;
the new context field is optional.

## Select and read

`ctx.files.attachments` is an immutable tuple of `ToolAttachment` entries. Each
has an opaque invocation-local `id`, `filename`, byte `size`, optional `media_type`,
`source` (`current` or `reply`), optional `workspace_path`, and optional
`unavailable_reason`. Filenames and media metadata are untrusted input.

```python
from bram_agent_module_api import FileAccessError, ModuleToolContext

async def inspect_attachment(arguments: dict, ctx: ModuleToolContext) -> str:
    if ctx.files is None:
        return "File access is unavailable."
    matches = [item for item in ctx.files.attachments
               if item.source == "current" and item.filename == arguments.get("filename")]
    if len(matches) != 1:
        return "Select one attachment by its exact, unique filename."
    try:
        file = await ctx.files.read_attachment(matches[0].id, max_bytes=10485760)
    except FileAccessError as exc:
        return str(exc)
    return f"Read {len(file.data)} bytes of {file.media_type or 'unknown media'}."
```

`ToolFile` contains `filename`, `media_type`, and `data` as bytes (omitted from its
representation). Current attachments use the same staged workspace files that
`generate_image.reference_attachments` selects. Staging occurs after input
moderation; an unavailable or unstaged attachment cannot fall back to a fresh
Discord download through this port. The API does not resize, transcode, or strip
metadata. A workspace file can subsequently be edited; reads return its current
bytes, not a historical snapshot.

Reply entries contain only the reply images admitted into this turn. They have
generated names such as `reply-image-1.png` when the host has no original filename.
They are not automatically saved to the workspace. Images deduplicated from
history, excluded by the turn image budget, or removed by moderation are absent.
The reader does not substitute a history image or re-fetch messages. Generic reply
files, including audio, are not exposed by this turn input surface.

For an existing saved path, use
`await ctx.files.read_workspace("chat-attachments/123/example.png", max_bytes=10485760)`.
Paths are relative to the caller's workspace. Saved uploads and generated outputs
can be read in later turns until normal deletion or retention removes them. There
is no module-specific file store. Use the host's existing list/search tools to
select a saved path; this API does not enumerate the entire workspace.

## Bounds, lifetime, and privacy

- Every read requires a positive integer `max_bytes`. The host also applies
  `WORKSPACE_TOOL_MAX_FILE_BYTES` and a 50 MiB hard ceiling as an aggregate
  returned-byte budget per invocation. Oversized reads fail without truncation;
  repeated reads charge again.
- Reads use the existing workspace resolver and activity locks. Absolute paths,
  traversal, symlinks, directories, and paths outside the caller's workspace are
  rejected. Cancellation waits for an in-flight file worker before releasing its
  workspace lease.
- The port and attachment IDs expire when the handler finishes, including failure
  or cancellation. Do not retain them for background work. The budget does not bound
  copies that trusted module code retains outside the invocation.
- `FileAccessError.code` distinguishes `expired`, `unknown_attachment`,
  `unavailable`, `not_file`, `too_large`, `invalid_limit`, and `budget_exhausted`.
  Error messages omit absolute paths and signed URLs.
- Reads make no network calls. A module uploading bytes must declare the destination
  and document upload, retention, and deletion behavior. Copies retained by a module
  are its responsibility; host workspace and transcript privacy controls still apply.

## Testing

The SDK's `FakeToolFiles` accepts attachment descriptors plus `ToolFile` values
keyed by ID, or workspace files keyed by relative path. Pass it as
`ModuleToolContext(files=fake, ...)`. It enforces bounds and an aggregate budget,
records reads, and rejects unavailable entries. `fake.close()` simulates handler
return. It never reads a local file or contacts a network.

For integration tests, `modules.testing.build_test_runtime` supplies a real reader
over the test directory's `workspaces/` folder. Host tests cover caller isolation,
staging, permissions, and lifetime separately from module business logic.
