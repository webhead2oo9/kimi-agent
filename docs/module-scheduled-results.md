# Published scheduled-task results

SDK 2.4 adds `ctx.scheduled_results`, a durable subscription service for outputs
that assistant tasks have published to Discord. Require
`kimi-agent-module-api>=2.4,<3` and the host capability `scheduled_results.v1`.
`MODULE_API_VERSION` remains 2.

## Register a subscriber

Declare each subscription name in the module specification:

```python
from kimi_agent_module_api import ModulePermissions, ModuleSpec

SPEC = ModuleSpec(
    name="reports",
    version="1.0.0",
    api_version=2,
    create=create,
    requires_capabilities=("scheduled_results.v1",),
    permissions=ModulePermissions(scheduled_results=("published_reports",)),
)
```

Register its handler during `start()`, using an operator-configured guild ID:

```python
from kimi_agent_module_api import ScheduledResult, ScheduledResultFiles

async def on_result(result: ScheduledResult, files: ScheduledResultFiles) -> None:
    # Use notification_id as the idempotency key for local or remote processing.
    for message in result.messages:
        for attachment in message.attachments:
            if attachment.filename == "report.csv":
                data = await files.read(attachment.id, max_bytes=1_000_000)
                await import_report(result.notification_id, data)

async def start(self, ctx):
    assert ctx.scheduled_results is not None
    await ctx.scheduled_results.subscribe(
        "published_reports", guild_id=self.settings.guild_id, handler=on_result
    )
```

Names use 1–64 lowercase letters, digits, or underscores. Each registration is
scoped to one positive Discord guild ID and receives all published task results
in that guild. The same name can be registered in several guilds. Modules are
trusted, installed code; this permission grants access to published output in
those guilds. Before dispatch, the host checks module activation and guild
settings, the owner's current membership, block status and task tier, the guild's
task policy, and visibility of the origin and every output channel.

## Result and attachment access

`ScheduledResult` contains a stable notification ID, subscription name, task/run/
revision IDs, guild and owner IDs, publication and expiry times as Unix seconds,
and an ordered tuple of `ScheduledResultMessage` objects. Its outcome is
`completed`: silent checks, failures, and requests for input produce no result
notification. Test previews never notify live subscribers.

Each message contains the final posted text, a `MessageRef`, attachment metadata,
and optional `embed_json` containing a captured Discord embed, capped at 32 KiB.
If the embed exceeds that cap, `embed_unavailable_reason` explains its omission;
the notification still delivers its text, message references, and attachments.
Only required output posts are included. Management logs, task state,
instructions, provider payloads, and working transcripts are excluded. Embeds
are captured when this host version confirms publication; messages posted by
older versions may have no embed snapshot. Later Discord edits do not change
the result.

`files.read()` reads only attachments actually published by this run. It rechecks
access and retention, caps each read at 8 MiB and the entire invocation at 16 MiB,
and raises `ScheduledResultAccessError` when access or a limit prevents the read.
Oversized files remain visible as metadata. Readers expire when the handler
returns, raises, is cancelled, or times out. Embed URLs are returned as data;
the service does not fetch them.

## Publication and retries

The host creates notifications in the transaction that records the last
required Discord post as sent. Partial publication waits for the remaining
posts; optional logging does not delay notification. Task completion is
independent of module processing.

Returning acknowledges the notification. Exceptions or a 30-second timeout retry
with the same ID, starting after 30 seconds and backing off to one hour. Four
notifications can be processed concurrently. A 60-second lease lets a restarted
host recover an interrupted attempt. Processing is **at least once**: a crash
after a module's write but before acknowledgement can repeat its handler.
Commit a deduplication receipt with local writes in one transaction, or pass the
notification ID to a remote service's idempotency API.

A handler that ignores cancellation keeps its concurrency slot. Its module's
notifications are paused and its health is marked failed until the handler exits
or the host restarts. This prevents overlapping retries of a stuck handler.

Subscriptions and acknowledgements survive restarts. Register the same names
and guilds on every start to reconnect handlers. New subscriptions receive only
future publications. Disabled, missing, or unauthorized subscribers stay blocked
and are checked again after a minute. `/tasks history` shows notification states
and exports IDs, attempts, retry times, and reasons in its JSON attachment.
Loaded modules report queue health through `/modules status`; declarations
appear in `/modules manifest`. A successful notification cannot clear another
notification's failure. Retries never regenerate output or repost to Discord.

## Retention and deletion

Notifications and acknowledgements expire 30 days after publication, or earlier
if the task/run is deleted. Pending notifications do not extend task artifact
retention. Expired readers stop working immediately; the dispatcher removes
expired rows on its next sweep. Deleting a task or deleting its owner's task
data cascades through notifications and saved attachments. Privacy deletion
blocks new callbacks and waits for guarded callbacks already in progress.
Ordinary task deletion revokes future reads, but cannot recall data already
handed to an in-flight callback.

`await ctx.scheduled_results.unsubscribe(name, guild_id=...)` deletes that
subscription and its notifications. Closing, disabling, or uninstalling a module
leaves subscriptions intact for recovery. Pending notifications stay blocked
until reinstallation, explicit unsubscribe, or expiry, and remain visible in
task history.

Modules own any copies they retain or send elsewhere, including their deletion
and retention policy. The public SDK currently has no generic privacy-deletion
callback. Avoid retaining user content unless the deployment has an appropriate
deletion mechanism. The opt-in [reference subscriber](../bot/modules/example/README.md)
stores only opaque notification receipts and expiry times, never result content
or owner IDs. Its transaction demonstrates idempotent local processing.

## Testing

Use `kimi_agent_module_api.testing.FakeScheduledResults` in standalone tests.
Register a handler, then call `deliver(result, files={...})`. Failures propagate
and can be retried with the same result; successful IDs are acknowledged,
preview delivery is suppressed, and readers close when the invocation ends.
The fake has no database or background worker. Host tests exercise publication,
recovery, authorization, privacy deletion, and retention against real SQLite.
