# Tool catalog

This page lists every built-in tool Kimi can expose to the language model,
along with who can use each one and which runtime gates decide whether it gets
registered at all. The schemas and the enforcement behind them live in
`tools/`.

Deployment plugins and operator-authored script-backed skill tools are dynamic
rather than built in, so they get their own treatment under
[Extensible tools](#extensible-tools).

## Discord application commands

Before the tools themselves, it helps to separate out the slash commands. These
are direct Discord UI actions that a person invokes; the model never selects
them:

| Command | Access | Purpose |
|---|---|---|
| `/memory status`, `/memory opt-in`, `/memory opt-out` | Member | Inspect or change the current user's long-term-memory preference. |
| `/privacy` | Member | Show the privacy summary and confirmed memory/full-data deletion controls. |
| `/stop` | Member | Cancel the current response/coding work, all of the member's work, or one owned coding task. |
| `/usage` | Member; expanded for staff | Show the current user's usage, or let staff inspect another user or server totals. |
| `/models` | Owner | Inspect or change the global chat-model override. |
| `/moderation block`, `unblock`, `status` | Staff | Manage bot-level user blocks. |
| `/mod note`, `warn`, `timeout`, `kick`, `ban`, `history`, `case` | Staff | Create Discord moderation actions/cases and inspect case history. |
| **Teach Kimi** message context menu (name follows `BOT_NAME`) | Staff | Run the scoped learning flow against one selected human message. |

The optional `config_admin` module lets staff inspect guild-scoped fragments
and draft full-content replacements. Core posts each proposal as a persistent
Discord card; a staff member in that guild approves or rejects it with the
card's buttons.

## Visibility and access

The tables below use a few terms consistently, so here is what they mean:

- **Core** tools are placed in the model's tool schema at the start of every
  eligible turn.
- **Searchable** tools appear only by name and summary in `browse_tools`. The
  model loads the relevant names when a request calls for them, and that
  activation persists for the rest of the rooted conversation.
- **Contextual core** tools are core registrations that a per-turn policy masks
  whenever they could not act anyway, such as the thread lifecycle tools when
  the conversation is not inside a managed thread.
- **Tier** is the minimum resolved trust tier, where `MEMBER < REGULAR < STAFF`.
  Dispatch rechecks the tier on every call, so a tool name lingering in stale
  conversation state does not grant access.
- Any tool can additionally be hidden by the deployment, guild, or channel
  `blocked_tools` policy, and searchable tools can be pre-activated with
  `pinned_tools`. Neither mechanism bypasses the trust or dispatch checks. See
  [configuration](configuration.md).

When a tool is unavailable or the caller is not authorized, it is left out of
the model-facing surface and masked as `Unknown tool` at dispatch, so its
existence never leaks. The registry in
[`bot/tools/registry.py`](../bot/tools/registry.py) is the authoritative
boundary; prompt text is never an access control.

## Discovery and task flow

| Tool | Visibility | Tier | Purpose |
|------|------------|------|---------|
| `browse_tools` | Core | Member | List the searchable catalog and activate selected tools for the conversation. |
| `plan` | Core | Member | Maintain the user-visible checklist for one multi-step reply; the plan is not persisted. |

## Discord context, members, and safety

| Tool | Visibility | Tier | Purpose and availability |
|------|------------|------|--------------------------|
| `get_channel_context` | Core | Member | Read bounded recent context before the triggering message. Returned messages and image references are untrusted context. |
| `lookup_member` | Searchable | Member | Resolve a current-guild member by ID or name and return profile/role information. Staff callers also receive the bot's resolved trust tier. |
| `discord_text_search` | Searchable | Member | Search message text only in explicitly configured channels. Registered when `DISCORD_SEARCH_CHANNELS` is non-empty; Message Content intent must also be enabled. |
| `internet_search` | Core | Member | Search the live web, or read pages the model already has URLs for. Registered when `EXA_API_KEY` or `BRAVE_API_KEY` is set; a search blends the configured providers by default. |
| `block_user` | Core | Member | Stop the current speaker from using the bot. It cannot target another user, and staff cannot be self-blocked through this tool. |

`get_channel_context` reads the live Discord window without adding any of
those messages to the rooted transcript. `discord_text_search` is an optional,
allowlisted search surface, and it never quietly broadens to the whole guild.
See [configuration](configuration.md#discord-text-search-gated).

`internet_search` returns compact, untrusted results and never tells the model
which provider answered. A search that found nothing says so in as many words,
so the model can tell an empty web from a broken provider. See
[Internet search](internet-search.md).

## Video understanding

| Tool | Visibility | Tier | Purpose and availability |
|---|---|---|---|
| `video` | Searchable | Member | Start or continue a stateful Gemini specialist session over one public YouTube video. Registered only when `VIDEO_UNDERSTANDING_ENABLED` is true and `GEMINI_API_KEY` is set. |

`start` sends a canonical public YouTube URL and a specific question;
`ask` continues through Gemini's stored `previous_interaction_id`. Local opaque
handles are rechecked against the current user, guild, rooted conversation, and
expiry on every call. Results are structured, timestamped, and untrusted.
Sessions default to a 24-hour idle lifetime, while an hourly deletion outbox
removes known provider Interactions after expiry, transcript retention, or a
full `/privacy` deletion. See [Video understanding](video-understanding.md) for
configuration, caching, limits, provider retention, and the threat model.

## Workspace and files

All of the workspace tools are member-tier and operate only inside the current
user's per-guild sandbox. Paths, quotas, symlinks, archive expansion, downloads,
output attachments, and cleanup all follow the containment rules documented in
[Workspace Tools](workspace.md).

| Tool | Visibility | Purpose |
|------|------------|---------|
| `import_attachment` | Core | Copy an exactly named attachment from the triggering Discord message into the workspace. |
| `read_file` | Core | Read a bounded range of a UTF-8 text file with line numbers. |
| `write_file` | Core | Create or replace a text file and, by default, queue it for the reply. |
| `edit_file` | Core | Apply one exact-string replacement to a text file. |
| `multi_edit` | Core | Apply an ordered, atomic set of exact-string replacements to one file. |
| `move_file` | Core | Move or rename a workspace file or directory. |
| `delete_file` | Core | Delete a file or, when explicitly recursive, a directory tree. |
| `list_workspace` | Core | List one directory level. |
| `grep_workspace` | Core | Search UTF-8 file contents with bounded literal or regex matching. |
| `glob_workspace` | Core | Find files by case-insensitive name or glob pattern. |
| `view_image` | Core | Show a bounded PNG, JPEG, GIF, or WebP file to an image-capable model during the turn. |
| `queue_file` | Core | Add or remove a workspace/generated file from the reply's attachment queue. |
| `fetch_url` | Core | Download an SSRF-checked HTTPS URL into the workspace and queue the result. |
| `zip` | Core | Package selected workspace paths into a queued ZIP archive. |
| `extract_archive` | Searchable | Safely unpack a ZIP, TAR.GZ, or TGZ already in the workspace. |
| `extract_document_text` | Searchable | Convert bounded PDF, Office, OpenDocument, RTF, EPUB, or CSV content into readable workspace text. |

`view_image` stays registered on text-only models, but it refuses the call
cleanly unless the active provider supports image input. Note that a file
existing in the workspace is not enough to get it sent: tools that auto-queue
their output and explicit `queue_file` calls share the same bounded attachment
rail.

## Code execution

| Tool | Visibility | Tier | Purpose and availability |
|---|---|---|---|
| `run_code` | Core | Member | Run inline Python/shell code or a workspace file inside the Linux systemd/Bubblewrap/seccomp sandbox. Registered only when `CODE_EXEC_ENABLED` is true and the selected `none`, `host`, or `netns` profile passes its startup probe. |

The network mode is deployment configuration, and no individual call can change
it. A networked mode can also install validated `pip_install` requirements into
a workspace environment that survives between runs. `host` shares every route
the bot host can reach, while `netns` runs inside an operator-provisioned
namespace and proves at startup that a known-open private target is
unreachable from it. Read [Code execution](code-exec.md) before you enable any
of this.

## Durable coding tasks

These core `MEMBER` tools register only when the dedicated coding role and the
code-execution sandbox are both enabled and available:

| Tool | Purpose |
|---|---|
| `start_coding_task` | Queue multi-file or repository work with optional bounded conversation context, selected triggering-message attachments, and validated workspace starting files. Returns immediately with a durable task id, or a specific explanation when nothing was queued. |
| `coding_task_status` | Inspect an owned active or recent task without waiting for completion. |
| `coding_task_message` | Append steering that the agent receives at its next model boundary. |
| `coding_task_cancel` | Cancel a queued or running task after stopping its managed jobs. |
| `coding_task_retry_delivery` | Retry an exhausted final-report delivery after its Discord target is restored. |

The worker itself sees a narrower surface: the workspace subset plus
`coding_plan`, `coding_progress`, and the managed job start/status/cancel
controls. See [Durable coding agent](coding-agent.md).

## Persistent browser

| Tool | Visibility | Tier | Purpose and availability |
|---|---|---|---|
| `browser` | Core | Member | Run one bounded BetterWright/Playwright step in the current user's persistent profile. Registered only when `BROWSER_ENABLED` is true and the pinned runtime and the selected `host` or `netns` sandbox both pass their startup probe. |

As with code execution, the network mode is fixed by the deployment and never
by the model. Profiles, screenshots, lifecycle, VPN lease sharing, and the
vault, download, and live-view surfaces that are deliberately switched off are
all covered in [Persistent browser](browser.md).

## Visual rendering

| Tool | Visibility | Tier | Purpose and availability |
|---|---|---|---|
| `render_chart` | Searchable | Member | Render and queue one fixed 1200×675 PNG from structured bar, line, or scatter data. |
| `render_diagram` | Searchable | Member | Render and queue one fixed 1200×675 PNG from constrained Mermaid source. |

Both tools register automatically when `BROWSER_ENABLED` is true, the
persistent-browser gate passes, and the pinned Mermaid runtime is present. The
model makes one visual tool call; it never has to call `browser`. Rendering
uses a fresh offline Chromium process with no persistent profile or VPN lease,
and the host fully validates the PNG before Discord delivery. The required alt
text becomes the Discord attachment description. Supported diagrams, chart
limits, accessibility distinctions, deployment, and the threat model are in
[Visual rendering](visual-rendering.md).

## Memory and community knowledge

These tools register only once the optional Hindsight backend is ready. The
user-facing tools always derive their subject from the current Discord user and
do not accept another user's ID. `/memory opt-out` disables the current user's
memory reads and writes. The community tools resolve a separate bank for the
current guild. See [Memory](memory.md) for bank scoping, source provenance,
retention, and deletion behavior.

| Tool | Visibility | Tier | Purpose |
|------|------------|------|---------|
| `recall_user` | Core | Member | Search the current user's durable memories for relevant facts. |
| `reflect_user` | Core | Member | Synthesize an answer across the current user's memories; intended for reasoning, not simple lookup. |
| `remember_user_memory` | Core | Member | Store a durable first-party fact about the current user, anchored to the current Discord message. |
| `lookup_memory_source` | Core | Member | Show the bounded Discord source window behind one of the current user's memories. |
| `recall_community` | Core | Member | Search public knowledge taught in the current guild. |
| `reflect_community` | Core | Member | Synthesize an answer across public knowledge in the current guild. |
| `teach` | Core | Staff | Store public knowledge in the current guild's community bank. |

Automatic recall on the responding turn and optional background auto-retention
are memory features too, but they are not model-callable tools, which is why
they do not appear in the table.

## Shared and personal skills

Shared skills combine the read-only instructions shipped in `skills/builtin/`
with deployment-owned instructions in the private `SKILLS_DIR`. Built-ins are
global, their names are reserved, and model-facing listings mark them
read-only. The Discord-side staff tools manage only private instruction
documents; executable scripts and tool declarations remain private-store,
operator-authored content. A skill created through Discord is owned by the
current guild, and staff can edit or delete only skills owned exclusively by
that guild; global and multi-guild skills stay operator-managed and are listed
read-only.

The shipped instruction set covers bot identity, browser operation, workspace
and coding-task routing, embeds, and managed threads. Its `{{bot_name}}` token
is rendered from operator configuration, and no other built-in placeholders are
accepted.

Personal skills are instruction-only documents owned by one Discord user. See
the [shared skill stores guide](../bot/skills/README.md) and
[Personal Skills](personal-skills.md).

| Tool | Visibility | Tier | Purpose |
|------|------------|------|---------|
| `skill_list` | Core | Member | List shared instruction skills visible in the current guild. |
| `load_skill` | Core | Member | Load one shared skill's complete instructions and reference-file manifest. |
| `skill_file` | Activated by `load_skill` | Member | Read or search reference files bundled with a loaded shared skill. |
| `skill_create` | Core | Staff | Create a private instruction-only shared skill; built-in names are reserved. |
| `skill_edit` | Core | Staff | Patch, append to, or replace a private instruction-only skill owned by the current guild. |
| `skill_delete` | Core | Staff | Delete a private skill owned by the current guild. |
| `my_skill_get` | Core | Member | Load one personal skill belonging to the current user. |
| `my_skill_create` | Searchable | Member | Create an instruction-only personal skill for the current user. |
| `my_skill_edit` | Searchable | Member | Edit one of the current user's personal skills. |
| `my_skill_delete` | Searchable | Member | Delete one of the current user's personal skills. |

## Rich replies and managed threads

| Tool | Visibility | Tier | Purpose and availability |
|------|------------|------|--------------------------|
| `build_discord_embed` | Searchable | Member | Queue one validated rich embed for the current reply. Always registered; see [Discord Embed Builder](embeds.md). |
| `move_to_thread` | Core | Member | Move the conversation into a new managed public thread, optionally in an allowlisted target channel. Requires `THREAD_HANDOFF_ENABLED`; hidden in DMs, existing threads/forums, announcement channels, and wherever policy disables handoff. |
| `leave_thread` | Contextual core | Member | Send a final reply, then lock and archive the current managed thread. |
| `pause_thread_replies` | Contextual core | Member | Keep the managed thread open but return it to mention/reply/name invocation. |
| `resume_thread_replies` | Contextual core | Member | Restore automatic replies for the current paused managed thread. |

Changing a thread's lifecycle requires being the thread initiator, holding
staff tier, or having Discord's Manage Threads permission. The per-turn surface
exposes only the actions that make sense for the thread's current state. See
[Thread Handoff](thread-handoff.md).

## User persona overrides

These tools are all searchable and register only when `config/models.yaml`
assigns a `persona` role. They operate solely on the current user's stored
persona and require `REGULAR` tier or higher. See
[User Persona Overrides](persona.md).

| Tool | Purpose |
|------|---------|
| `persona_set` | Compile and store a safe character/style override from the current user's request. |
| `persona_show` | Show the current user's stored persona override. |
| `persona_clear` | Remove the current user's stored persona override. |

## Extensible tools

The built-in catalog is not the ceiling for a deployment. Two more surfaces
can add tools:

- **Script-backed skill tools** come from operator-authored `tools:`
  declarations and scripts in the private `SKILLS_DIR`. A tool declaration
  carries trust (`min_tier`), searchability (`availability`), guild scope
  (`guild_ids`), a timeout, arguments (`parameters`), and an explicit
  `network: true` opt-in; declared secrets (`requires_secrets:`) are
  skill-level, and captured output and output files are bounded by
  deployment-wide caps. These scripts run inside the mandatory Linux Bubblewrap
  boundary with a read-only skill/runtime, private process and filesystem
  state, a default-denied network, resource limits, and only the per-call
  workspace writable. Be aware that the network opt-in shares unrestricted host
  egress, and declared secrets remain visible to the script; see the
  [private shared store guide](../bot/skills/README.md).
- **Plugin tools** come from modules explicitly allowlisted in
  `PLUGIN_MODULES`. Built-in tools register before plugins, so a duplicate name
  fails the plugin. The exceptions are the skill-management and Hindsight
  memory tools, which register later; a plugin must not claim those names
  either. See [Plugins](plugins.md).

Both surfaces enter the same `ToolRegistry` and get the same tier, guild,
policy, activation, timeout, and dispatch enforcement as the built-ins. The
exact dynamic catalog is deployment state, and you can see it at runtime
through `browse_tools` and the startup capability logs.

## Operator reference

- [Configuration](configuration.md) covers registration gates, `blocked_tools`,
  `pinned_tools`, per-tool config fragments, and resource limits.
- [Observability](observability.md) covers the optional diagnostic event stream,
  including its bounded tool-argument/result fields and sensitive-data warning.
- [`bot/app/tools.py`](../bot/app/tools.py) is the built-in composition root.
- [`bot/tools/registry.py`](../bot/tools/registry.py) owns visibility and dispatch.
- [`bot/skills/registration.py`](../bot/skills/registration.py) owns executable skill
  tool registration.
