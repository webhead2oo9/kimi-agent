# Workspace tools

Each user gets a sandboxed workspace directory **per community**
(`base_dir/<owner_key>/files`, where `owner_key = <user_id>__<guild_id>`) managed
by `workspace/manager.py:WorkspaceManager`, with TTL/quota sweeping driven by the
background sweeper in `discord_adapter/lifecycle.py`. The `tools/workspace/` package exposes the
file tools the model uses to read, write, search, and package files there. All tools
are `MEMBER`-tier core tools (always visible), except `extract_document_text` and
`extract_archive`, which are searchable/browse-tools-only.

## Owner key (per-(user, guild) scoping)

The workspace is keyed by **community, not just user**: a user's files in one guild
are isolated from their files in another, so the available tool/skill surface and the
work-in-progress never bleed across communities, and a file created in one guild is
unreachable from another. The composite is derived by
`workspace/manager.py:workspace_owner_key(user_id, guild_id)` and surfaced as the
`MessageContext.workspace_key` property (`tools/registry.py`). It is flat and opaque:
`<user_id>__<guild_id>`, with contexts that have no guild collapsing to
`<user_id>__dm` and synthetic guild ids sanitized, so the sweeper, quota, and
TTL never parse it and apply per (user, guild). `ctx.user_id` stays the real
Discord id for everything else (memory banks, blocking, owner-only checks, and
usage); only workspace-bound reads use `ctx.workspace_key`. The
conversation-keyed `generated/<context_key>/<job>/…` tree is a separate
per-conversation namespace.

## Containment model

Here, **sandbox** means application-level path and quota containment for the model-facing
workspace tools. It is not a Linux process sandbox and does not constrain operator plugins,
or another process running as the bot user. Executable skill scripts use a separate mandatory
Linux Bubblewrap boundary: only their exact per-call job directory is mounted read/write at
`/workspace`; other users' and communities' workspaces are absent. The supported production
security target is Linux; development may run elsewhere, but the permission and adversarial
guarantees in this document are evaluated on Linux.

Containment is enforced at one chokepoint: every model-supplied path is resolved
through `WorkspaceManager.resolve_user_file_path(ctx.workspace_key, path, …)`
(`workspace/manager.py`). It rejects absolute paths, `..` traversal, paths deeper than
`MAX_PATH_DEPTH` (40) parts, and **any symlink in the resolved chain**, and re-checks
`is_relative_to(root)` after `resolve()`. The depth bound is a sanity cap, not the
containment boundary; it is set generously so real source trees (deep Java/mod
package paths, optionally under an extracted-archive prefix) resolve. Workspace
reads, writes, output staging/delivery, and the sweeper also share one
activity/maintenance lease. A sweep therefore cannot delete an active file.
`/privacy` **Delete my data** removes every `<user_id>__*` directory plus
generated jobs marked with the user's `.owner-user-id` under the same
maintenance lease. See [privacy.md](privacy.md).
Queued files are copied into a generated delivery job before output moderation;
Discord receives that immutable snapshot. No tool ever constructs a
path from raw arguments or accepts a user/target id as a parameter. The owner key is always
`ctx.workspace_key`, set by the runtime, never by the model. Explicit internal
`create_job_dir` IDs must already be one unchanged safe path segment; omitted IDs continue
to use a generated UUID.

On POSIX, newly created workspace base, owner, files, jobs, and generated directories are
private (`0700`), and newly written generated-job owner markers are `0600`. Existing trees
are left untouched; this hardening applies when directories are created and is not retroactive.

Every tool inherits the same recipe:

- **Walks skip symlinks per entry** before any read/stat (`run_grep_walk`,
  `run_glob_walk`, `user_files_size`).
- **Writes re-check `is_symlink()`/`is_file()` at write time** (TOCTOU guard), under
  the per-workspace lock (`UserLocks.for_user`, keyed by `ctx.workspace_key`).
- **Quota and size are enforced before writing.** `quota_ok` runs against
  `max_user_bytes` and `max_file_bytes` on the final payload.
- **Errors are scrubbed** through `scrub_user_paths` so absolute server paths never
  reach Discord.
- **Errors self-correct.** Malformed arguments raise precise messages instead of
  silently coercing (`offset: "abc"` errors instead of reading from line 1;
  `attach: "flase"` errors instead of quietly un-attaching a deliverable); quota
  failures report used/limit bytes plus the remediation ("delete files you no
  longer need"); a literal grep that matches nothing but looks like a regex gets
  a `hint` to pass `regex: true`; `read_file` past EOF says so instead of
  returning the last line.
- **Entry count is capped** (`max_workspace_entries`, default 20k): byte quotas
  alone leave file count unbounded, and every quota walk, grep, and sweep is
  O(entries). New-entry writes/imports/extracts past the cap are refused;
  existing files can still be edited and deleted.
- **The attachment rail tracks mutations.** Write tools auto-queue their file;
  `move_file` rewrites queued entries (including everything under a moved
  directory) and `delete_file` un-queues them (reported as `"unattached": true`),
  so the rail can never dangle and fail the reply's file staging at the Discord
  boundary.
- **Heavy walks are offloaded** to `asyncio.to_thread` and bounded by a result cap,
  so a deep tree cannot stall the shared event loop.
- **Document conversion is bounded.** Office conversions are serialized across users;
  anydoc enforces package expansion/model limits, and CSV conversion additionally caps
  rows, cells, fields, and generated Markdown while streaming into the read-size limit.
- **`grep_workspace` regex matching is time-bounded** (`WORKSPACE_TOOL_GREP_TIMEOUT_SECONDS`):
  the tool matches with the `regex` engine, which honors a per-match deadline and releases
  the GIL, so a member's catastrophic pattern raises `GrepTimeoutError` (surfaced as a
  time-budget error) instead of pinning the event loop. Stdlib `re` cannot be interrupted
  mid-match. `looks_catastrophic` still fast-rejects the obvious nested-quantifier shapes.

## Regenerable env dirs (`.venv` / `.pio` / `.pio-core`)

Workspace environment directories named `.venv`, `.pio`, and `.pio-core`
(`workspace/manager.py:ENV_DIR_NAMES`) are treated as a separate, regenerable
class so a hundreds-of-MB dependency tree never disrupts documents. The
`run_code` tool may create them inside its Linux sandbox; ordinary workspace
placement tools still refuse the names:

- **Separate bounded accounting.** Their bytes are excluded from
  `user_files_size` (the document quota the write tools enforce) and counted by
  the sweeper against `CODE_EXEC_ENV_DIR_MAX_MB`. Every environment entry
  (files, directories, links, and special nodes, including zero-byte files)
  independently counts against `CODE_EXEC_ENV_DIR_MAX_FILES`; an overage
  removes the complete regenerable roots; preserving an inode DoS is worse.
  A subtree that cannot be inspected fails closed as a quota violation instead
  of disappearing from the count.
- **Swept as whole units.** The TTL/oversize sweep removes an entire env dir at
  once (whole `.venv` aged out, any single root crossing the entry cap, or oldest
  env dirs pruned until the combined byte and entry allowances are restored)
  and never enters the per-file document passes, so it cannot leave a broken
  half-environment. Deletion restores owner access on mode-000 directories, then
  walks the tree fd-relative with no-follow opens, checking filesystem identity
  as it goes and bounding how deep it will descend. Attacker-controlled depth,
  links, and rename races therefore cannot redirect cleanup outside the owned
  tree.
- **Hidden from listings and archives.** `glob_workspace`, `grep_workspace`,
  `list_workspace`, and `zip` skip them, so their churn never floods results.
  Explicit paths still resolve, allowing an exact built artifact under an env
  directory to be queued for delivery.
- **Fenced from every placement path.** Because their bytes are excluded from the
  doc quota, any tool that can *place* files must refuse env-dir targets or the
  quota is evadable: `write_file`/`edit_file`/`multi_edit` (via `ensure_quota`),
  `move_file` (both source and destination), `extract_archive`, and `fetch_url`
  all reject with a distinct `EnvDirWriteError` message, never masked as a
  quota failure, so the model cannot flail into delete-and-retry.

## Tools

| Tool | Purpose |
|------|---------|
| `read_file` | Read a text file with line numbers (offset/limit, negative offset = from end). Returns plain text, not JSON: a `path: lines A-B of N` header plus numbered lines. |
| `write_file` | Create or overwrite a text file and queue it for attachment (`attach: false` opts out, see below). |
| `edit_file` | Replace one exact-match string (`replace_all` for all occurrences). |
| `multi_edit` | Apply several exact-match edits to one file in a single call (see below). |
| `move_file` | Move or rename a file or directory (dest must not exist; parents created; same per-user lock and symlink/containment checks as the write tools; env dirs refused on both ends; queued attachments follow the move). |
| `delete_file` | Delete a file or (with `recursive`) a directory tree, capped by entry count; drops stale attachment-rail entries, and unlinks a symlink found in the workspace (the link itself, never followed). |
| `list_workspace` | List one level of a directory. |
| `glob_workspace` | Find files by name/glob across the tree (see below). |
| `grep_workspace` | Search file contents by literal text or explicit regex. |
| `view_image` | Show a workspace image (png/jpeg/gif/webp) to the vision model (see below). |
| `import_attachment` | Pull a file attached to the current message into the workspace. |
| `queue_file` | Manage the reply's attachments: `action: add` (default) attaches an existing workspace file or generated artifact; `action: remove` takes a queued file back off the reply, freeing its slot (see below). |
| `extract_document_text` | Extract text from a bounded-page PDF or office document (Word, Excel, PowerPoint, OpenDocument, RTF, EPUB, CSV) into the workspace. PDF text accumulation stops at the configured read-output ceiling. |
| `extract_archive` | Safely extract an archive (registered in `tools/workspace/archive_tools.py`; extractor in `tools/archive.py`). |
| `zip` | Package workspace files into a zip (`paths: ["."]` archives everything; env dirs and in-flight `.part` temps are skipped). |
| `fetch_url` | Fetch a web page into the workspace via the SSRF-safe download path. An explicit `filename` never overwrites an existing file. The download runs outside the maintenance barrier (per-user lock only, temp in the system tempdir), so a slow origin cannot stall other users' workspace tools. |

Native document parsing is serialized across the runtime, and cancellation keeps the
parser slot occupied until the underlying worker actually finishes. PDF page count and
cumulative saved text are bounded, but PyMuPDF materializes the current page before the
text ceiling can be applied. Until parsing is isolated in a disposable worker, the repository
provides no per-document memory boundary; the operator must impose a whole-service memory
limit in the Linux service or container.

### `glob_workspace`

Name-based file discovery, complementing `grep_workspace` (which searches contents)
and `list_workspace` (one level only). It mirrors `run_grep_walk`'s bounded
`rglob` + per-entry symlink skip, but matches **names**, not file bodies, so it never
reads file contents.

- The pattern is matched case-insensitively (via `fnmatch`) against both the
  workspace-relative posix path and the bare basename. Because `*` spans `/`,
  `*.py` finds `.py` files at any depth; a bare `config.json` finds that file
  anywhere.
- `path` scopes the search to a subdirectory; it defaults to the whole workspace.
- `*` already spans `/`, so `**` is never needed; a leading `**/` is stripped
  instead of punished (models habitually send `**/*.py`, which would otherwise
  silently miss root-level files).
- Results are capped at `glob_max_results` (default 200, `WORKSPACE_TOOL_GLOB_MAX_RESULTS`);
  a `truncated` flag is set when the cap stops the walk before every entry is
  inspected.
- Returns matching file paths only; symlink entries are skipped.

### `multi_edit`

Applies an ordered list of exact-string `edits` to one file in a single call,
modeled on `edit_file`. Semantics:

- Edits apply **in order, each to the result of the previous** edit.
- It is **all-or-nothing**: every edit is applied in memory first, and if any edit's
  `old_string` is missing or ambiguous (matches more than once without
  `replace_all`), the whole call fails with `edit N: …` and the file is left
  **unchanged**, never partially edited.
- Each `old_string` must match exactly once unless that edit sets `replace_all`.
- The same per-user lock, binary/UTF-8 rejection, `max_file_bytes`, and `quota_ok`
  checks as `edit_file` apply to the final payload.
- Capped at `multi_edit_max_ops` edits per call (default 50,
  `WORKSPACE_TOOL_MULTI_EDIT_MAX_OPS`).

### Attachment opt-out (`attach: false`)

`write_file`, `edit_file`, and `multi_edit` auto-queue the touched file onto the
reply's attachment rail by default, which is the right behavior for "write me a
script".
For iterative work (write a scratch script, run it, fix it, write the real
answer), each of those three tools accepts `attach: false` so intermediate files
never ride the final reply or consume the attachment budget (`zip` and
`fetch_url` also auto-queue but have no opt-out; remove theirs with
`queue_file` `action: remove`). The opt-out is reversible:
`queue_file` attaches any workspace file explicitly. Each tool's `attached`
response field reports whether the file is **now queued on the reply**. A file
queued by an earlier call stays queued (`attach: false` skips adding, it does
not remove), and re-writing an already-queued path reports `attached: true`.

### Removing a queued attachment (`queue_file` `action: remove`)

The attachment rail is capped (`workspace_tool_max_attachments`, default 5) and
auto-queueing (write tools and generated artifacts) shares the cap with explicit
`queue_file` calls, so incidental files can occupy every slot before the real
deliverable exists. `queue_file` with `action: "remove"` un-attaches a queued
file, freeing its slot rather than forcing an overwrite of an already-queued path
(which would ship the deliverable under a stale filename):

- `path` accepts the same forms as `add`: a workspace-relative path, a
  `generated/...` artifact path, an exact absolute path already known internally,
  or a bare filename. Script-backed skills expose an opaque per-turn `remove_id`
  alongside each attached basename; that ID is also accepted as `path`, so
  duplicate skill-output basenames remain removable without exposing job-output
  paths. A bare filename must match exactly one queued entry; an ambiguous call
  returns the matching `remove_id` values.
- Removal works even if the file was deleted from disk after queueing (a dangling
  entry would otherwise be silently skipped at send time).
- The file backing a pending embed image cannot be removed; the embed must be
  rebuilt with a different (or no) image first.
- The response mirrors `add`: `removed` plus the post-call `queued_files` list.

### `view_image`

Lets the model look at an image already in the workspace (e.g. one pulled in via
`import_attachment` or unpacked from an archive) on demand, not just the
images attached to the triggering message. Because tool results are text-only at the
provider boundary, the image cannot ride back as a tool result; instead:

- The tool resolves the path through the usual containment chokepoint, enforces
  `view_image_max_bytes` (default 5 MB, `WORKSPACE_TOOL_VIEW_IMAGE_MAX_BYTES`), and
  **sniffs the media type from magic bytes** (png/jpeg/gif/webp) and never trusts the
  extension, since workspace files can be attacker-uploaded.
- It base64-encodes the image onto the per-turn `MessageContext.pending_view_images`
  rail and returns a small text confirmation (respecting the string tool contract).
- `agent/core.py` drains that rail after tool dispatch into **one synthetic
  untrusted user-role message**, the same user-role image path Discord attachments
  use, so the model sees the image on its next step. This works on every
  image-capable provider without per-provider serializer changes.
- It is gated on `MessageContext.images_supported` (set from the active provider's
  `IMAGE_INPUT` capability): on a text-only provider the tool refuses cleanly instead
  of aborting the turn, and `agent/core.py`'s capability check independently validates
  any injected image part. Bounded to `view_image_max_per_turn` images per reply
  (default 4, `WORKSPACE_TOOL_VIEW_IMAGE_MAX_PER_TURN`).
- The injected image is in-turn only: `turn_messages` is local to the ReAct loop and
  is never written to the SQLite transcript, so images do not accumulate across turns.

## The `plan` tool (in-turn checklist)

`tools/plan.py` registers `plan`, a general per-turn affordance (not a file tool).
The model uses it on multi-step replies to lay out an ordered checklist and update
step statuses (`pending`/`in_progress`/`completed`) as it goes. It is the bot's own
working memory for **one reply**:

- The handler stashes the checklist on `MessageContext.plan` and echoes it back so
  the model can re-read it across ReAct iterations.
- `MessageContext.plan` is per-turn scratch. It dies when the turn returns and is
  structurally outside the SQLite-persisted transcript (only real Discord messages
  are persisted; tool results and `MessageContext` fields are not).
- **The user sees it live.** After each dispatch that rebinds the plan,
  `agent/core.py` emits it through `agent/activity.py:emit_plan_update`
  (`SupportsPlanUpdates`, the sibling of the narration-step protocol), and the
  mention/reply activity surface renders it as muted `-#` checklist lines below
  the narration block: `✓ N done` collapse, `→` current step, `○` next pending
  steps,
  self-capped by `_format_plan_block`'s degradation ladder so it always fits the
  2,000-char message. The block is live-only: it survives the stale
  "still thinking…" render but is dropped from the finished activity log, and a
  plan-only turn still deletes the throwaway status message. On moderated tiers,
  `_ModeratedActivityReporter` output-checks step contents before forwarding.
- **It survives compaction.** Both compactor entry points take the current plan and
  re-append it verbatim (as the tool-echo JSON) to the progress note; see
  `docs/compaction.md`.
- Not remembered across turns; bounded by `MAX_PLAN_STEPS` (30) and
  `MAX_PLAN_STEP_CHARS` (200, overlong content is clipped).

## Configuration

Caps live in `WorkspaceToolConfig` (`tools/workspace/config.py`), plumbed from
`Settings` via `app/tools.py:_workspace_tool_config` and documented in
`.env.example`. The relevant keys include `WORKSPACE_TOOL_MAX_FILE_BYTES`,
`WORKSPACE_TOOL_MAX_USER_BYTES`, the grep family
(`WORKSPACE_TOOL_DEFAULT_GREP_RESULTS`, `WORKSPACE_TOOL_MAX_GREP_RESULTS`,
`WORKSPACE_TOOL_MAX_GREP_CONTEXT`, `WORKSPACE_TOOL_MAX_GREP_LINE_CHARS`,
`WORKSPACE_TOOL_MAX_GREP_PATTERN_CHARS`, `WORKSPACE_TOOL_GREP_TIMEOUT_SECONDS`),
`WORKSPACE_TOOL_GLOB_MAX_RESULTS`, `WORKSPACE_TOOL_MULTI_EDIT_MAX_OPS`,
`WORKSPACE_TOOL_VIEW_IMAGE_MAX_BYTES` / `WORKSPACE_TOOL_VIEW_IMAGE_MAX_PER_TURN`,
`WORKSPACE_TOOL_MAX_ATTACHMENTS`, `WORKSPACE_TOOL_MAX_ENTRIES` (the
`max_workspace_entries` cap above), and the env-dir allowances
`CODE_EXEC_ENV_DIR_MAX_MB` / `CODE_EXEC_ENV_DIR_MAX_FILES`.
