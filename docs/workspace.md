# Workspace tools

In ordinary guild chat, each user gets a sandboxed workspace directory **per community** (`base_dir/<owner_key>/files`, where `owner_key = <user_id>__<guild_id>`). Personal chat instead uses one `<user_id>__userapp` workspace across locations. Both are managed by `workspace/manager.py:WorkspaceManager`, and the background sweeper in `discord_adapter/lifecycle.py` handles TTL and quota enforcement. The `tools/workspace/` package exposes the file tools the model uses to read, write, search, and package files there. Most are `MEMBER`-tier core tools (always visible). `extract_document_text` and `extract_archive` are searchable and only appear through `browse_tools`.

## Quick reference

The short version:

- **One workspace per (user, guild)** in guild chat; **one per user** across `/chat` and personal DMs. The composite key is `ctx.workspace_key` and the model never sees it.
- **Path safety is enforced at one chokepoint** (`WorkspaceManager.resolve_user_file_path`): absolute paths, `..` traversal, depth over `MAX_PATH_DEPTH` (40), and any symlink in the resolved chain are rejected.
- **All workspace mutations share one per-user lock** (`UserLocks.for_user`, keyed by `ctx.workspace_key`) plus the broader workspace activity lease. The lock stops a concurrent writer racing a resolve→write path on the same workspace, which is what would make a symlink swap exploitable.
- **Quota is enforced before writing** (`WORKSPACE_TOOL_MAX_USER_BYTES`, `WORKSPACE_TOOL_MAX_FILE_BYTES`). Errors report used/limit bytes plus the remediation ("delete files you don't need").
- **Regenerable `.venv` / `.pio` / `.pio-core` trees have separate accounting** so a dependency tree weighing hundreds of MB never displaces a user's documents. Every workspace placement tool refuses them.
- **The attachment outbox is tracked.** Write tools can explicitly queue their file. `move_file` rewrites queued entries (including under moved directories), `delete_file` un-queues them, and the file state can never dangle into a failed send.
- **Document conversion is bounded** across users. PDF text accumulation stops at the configured read-output ceiling, CSV conversion caps rows/cells/fields and streams into the read-size limit. There is no per-document memory boundary yet, so impose a whole-service memory limit in the Linux service or container.
- **`grep_workspace` is time-bounded** (`WORKSPACE_TOOL_GREP_TIMEOUT_SECONDS`) using a regex engine that honors a per-match deadline and releases the GIL. Catastrophic patterns raise `GrepTimeoutError` rather than pinning the event loop.

The rest of the page covers the containment model, every tool's behavior, the regenerable env-dir accounting, the `plan` tool, and configuration.

## Owner key and scope

The workspace is keyed by **community, not just user**. A user's files in one guild are isolated from their files in another, so neither the available tool/skill surface nor any work in progress bleeds across communities, and a file created in one guild is unreachable from another. The composite key is derived by `workspace/manager.py:workspace_owner_key(user_id, guild_id)` and surfaced as the `MessageContext.workspace_key` property (`tools/registry.py`). It is flat and opaque: `<user_id>__<guild_id>`, with contexts that have no guild collapsing to `<user_id>__dm` and synthetic guild ids sanitized. That opacity is deliberate, so the sweeper, quota, and TTL never parse the key and simply apply per (user, guild). The personal user-app surface is the explicit exception: `/chat` and personal DMs override the key with `<user_id>__userapp`, so that workspace follows the user across invocation locations. `ctx.user_id` stays the real Discord id for everything else (memory banks, blocking, owner-only checks, and usage); only workspace-bound operations use `ctx.workspace_key`. The conversation-keyed `generated/<context_key>/<job>/…` tree is a separate per-conversation namespace.

## Containment model

In this document, **sandbox** means application-level path and quota containment for the model-facing workspace tools. It is not a Linux process sandbox, and it does not constrain operator plugins or another process running as the bot user. Executable skill scripts use a separate, mandatory Linux Bubblewrap boundary: only their exact per-call job directory is mounted read/write at `/workspace`, and other users' and communities' workspaces are simply absent. The supported production security target is Linux; development may run elsewhere, but the permission and adversarial guarantees described here are evaluated on Linux.

Containment is enforced at a single chokepoint. Every model-supplied path goes through `WorkspaceManager.resolve_user_file_path(ctx.workspace_key, path, …)` (`workspace/manager.py`), which rejects absolute paths, `..` traversal, paths deeper than `MAX_PATH_DEPTH` (40) parts, and **any symlink in the resolved chain**, and then re-checks `is_relative_to(root)` after `resolve()`. The depth bound is a sanity cap rather than the containment boundary itself; it is set generously so that real source trees (deep Java/mod package paths, possibly under an extracted-archive prefix) still resolve. Workspace reads, writes, output staging/delivery, and the sweeper also share one activity/maintenance lease, which is why a sweep can never delete a file that is in active use. `/privacy` **Delete my data** removes every `<user_id>__*` directory plus any generated jobs marked with the user's `.owner-user-id`, under that same maintenance lease. See [privacy.md](privacy.md).

Queued files are copied into a generated delivery job before output moderation, and Discord receives that immutable snapshot. No tool ever constructs a path from raw arguments or accepts a user or target id as a parameter. The owner key is always `ctx.workspace_key`, set by the runtime and never by the model. Explicit internal `create_job_dir` IDs must already be one unchanged safe path segment; omitted IDs continue to use a generated UUID.

On POSIX, newly created workspace base, owner, files, jobs, and generated directories are private (`0700`), and newly written generated-job owner markers are `0600`. Existing trees are left untouched: this hardening applies when directories are created and is not retroactive.

Every tool inherits the same recipe:

- **Walks skip symlinks per entry** before any read/stat (`run_grep_walk`, `run_glob_walk`, `user_files_size`).
- **Writes re-check `is_symlink()`/`is_file()` at write time** as a TOCTOU guard, under the per-workspace lock.
- **Quota and size are enforced before writing.** `quota_ok` runs against `max_user_bytes` and `max_file_bytes` on the final payload.
- **Errors are scrubbed** through `scrub_user_paths` so absolute server paths never reach Discord.
- **Errors self-correct.** Malformed arguments raise precise messages instead of silently coercing (`offset: "abc"` errors instead of reading from line 1; `attach: "flase"` errors instead of quietly un-attaching a deliverable). Quota failures report used/limit bytes plus the remediation ("delete files you don't need"); a literal grep that matches nothing but looks like a regex gets a `hint` to pass `regex: true`; and `read_file` past EOF says so instead of returning the last line.
- **Entry count is capped** (`max_workspace_entries`, default 20k). Byte quotas alone leave the file count unbounded, and every quota walk, grep, and sweep is O(entries). New-entry writes, imports, and extracts past the cap are refused; existing files can still be edited and deleted.
- **The attachment outbox tracks mutations.** Write tools can explicitly queue their file; `move_file` rewrites queued entries (including everything under a moved directory) and `delete_file` un-queues them (reported as `"unattached": true`), so the file state can never dangle and fail the reply's staging at the Discord boundary.
- **Heavy walks are offloaded** to `asyncio.to_thread` and bounded by a result cap, so a deep tree cannot stall the shared event loop.
- **Document conversion is bounded.** Office conversions are serialized across users; anydoc enforces package expansion and model limits, and CSV conversion additionally caps rows, cells, fields, and generated Markdown while streaming into the read-size limit.
- **`grep_workspace` regex matching is time-bounded** (`WORKSPACE_TOOL_GREP_TIMEOUT_SECONDS`). The tool matches with the `regex` engine, which honors a per-match deadline and releases the GIL, so a member's catastrophic pattern raises `GrepTimeoutError` (surfaced as a time-budget error) instead of pinning the event loop. Stdlib `re` cannot be interrupted mid-match, which is why it isn't used. `looks_catastrophic` still fast-rejects the obvious nested-quantifier shapes.

### A typical workspace turn

Most turns follow a simple shape:

1. **Look around**: `list_workspace` to see the top level, `glob_workspace` to find a class of files, or `grep_workspace` to search inside them.
2. **Read**: `read_file` with `offset` and `limit` for non-trivial files. Negative `offset` reads from the end.
3. **Edit**: `edit_file` for a single change, `multi_edit` for an ordered batch, `write_file` to overwrite.
4. **Deliver**: `queue_file(action: "add", path: "report.md")` to attach a finished artifact to the next reply, or include `attach: true` on the write call that produced it.
5. **Clean up**: `delete_file` (with `recursive: true` for trees) when intermediate files are no longer needed.

`fetch_url` and `zip` never auto-attach, so a `queue_file` after them is required to deliver their output.

## Regenerable env dirs (`.venv` / `.pio` / `.pio-core`)

Workspace environment directories named `.venv`, `.pio`, and `.pio-core` (`workspace/manager.py:ENV_DIR_NAMES`) are treated as a separate, regenerable class, so that a dependency tree weighing hundreds of MB never disrupts a user's documents. The `run_code` tool may create them inside its Linux sandbox; ordinary workspace placement tools still refuse the names.

- **Separate bounded accounting.** Their bytes are excluded from `user_files_size` (the document quota the write tools enforce) and counted instead by the sweeper against `CODE_EXEC_ENV_DIR_MAX_MB`. Every environment entry (files, directories, links, and special nodes, including zero-byte files) independently counts against `CODE_EXEC_ENV_DIR_MAX_FILES`, and an overage removes the complete regenerable roots, since preserving an inode DoS would be worse. A subtree that cannot be inspected fails closed as a quota violation rather than disappearing from the count.
- **Swept as whole units.** The TTL/oversize sweep removes an entire env dir at once (a whole `.venv` that aged out, any single root crossing the entry cap, or the oldest env dirs pruned until the combined byte and entry allowances are restored). It never enters the per-file document passes, so it cannot leave a broken half-environment behind. Deletion restores owner access on mode-000 directories, then walks the tree fd-relative with no-follow opens, checking filesystem identity as it goes and bounding how deep it will descend. Attacker-controlled depth, links, and rename races therefore cannot redirect cleanup outside the owned tree.
- **Hidden from listings and archives.** `glob_workspace`, `grep_workspace`, `list_workspace`, and `zip` skip them, so their churn never floods results. Explicit paths still resolve, which lets an exact built artifact under an env directory be queued for delivery.
- **Fenced from every placement path.** Because their bytes are excluded from the doc quota, any tool that can *place* files must refuse env-dir targets or the quota becomes evadable. `write_file`/`edit_file`/`multi_edit` (via `ensure_quota`), `move_file` (both source and destination), `extract_archive`, and `fetch_url` all reject with a distinct `EnvDirWriteError` message that is never masked as a quota failure, so the model cannot flail into a delete-and-retry loop.

## Tools

| Tool | Purpose |
|------|---------|
| `read_file` | Read a text file with line numbers (offset/limit, negative offset = from end). Returns plain text, not JSON: a `path: lines A-B of N` header plus numbered lines. |
| `write_file` | Create or overwrite a text file without attaching it by default (`attach: true` opts in, see below). |
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
| `zip` | Package workspace files into a zip without attaching it (`paths: ["."]` archives everything; env dirs and in-flight `.part` temps are skipped). |
| `fetch_url` | Fetch a web page into the workspace without attaching it via the SSRF-safe download path. An explicit `filename` never overwrites an existing file. The download runs outside the maintenance barrier (per-user lock only, temp in the system tempdir), so a slow origin cannot stall other users' workspace tools. |

Native document parsing is serialized across the runtime, and cancellation keeps the parser slot occupied until the underlying worker actually finishes. PDF page count and cumulative saved text are bounded, but PyMuPDF materializes the current page before the text ceiling can be applied. Until parsing is isolated in a disposable worker, the repository provides no per-document memory boundary, so as the operator you must impose a whole-service memory limit in the Linux service or container.

### `glob_workspace`

This is name-based file discovery, complementing `grep_workspace` (which searches contents) and `list_workspace` (one level only). It mirrors `run_grep_walk`'s bounded `rglob` plus per-entry symlink skip, but matches **names** rather than file bodies, so it never reads file contents.

- The pattern is matched case-insensitively (via `fnmatch`) against both the workspace-relative posix path and the bare basename. Because `*` spans `/`, `*.py` finds `.py` files at any depth, and a bare `config.json` finds that file anywhere.
- `path` scopes the search to a subdirectory; it defaults to the whole workspace.
- Since `*` already spans `/`, `**` is never needed. A leading `**/` is stripped rather than punished, because models habitually send `**/*.py`, which would otherwise silently miss root-level files.
- Results are capped at `glob_max_results` (default 200, `WORKSPACE_TOOL_GLOB_MAX_RESULTS`), and a `truncated` flag is set when the cap stops the walk before every entry is inspected.
- It returns matching file paths only; symlink entries are skipped.

### `multi_edit`

This applies an ordered list of exact-string `edits` to one file in a single call, modeled on `edit_file`. The semantics are:

- Edits apply **in order, each to the accumulated result**.
- It is **all-or-nothing**. Every edit is applied in memory first, and if any edit's `old_string` is missing or ambiguous (matches more than once without `replace_all`), the whole call fails with `edit N: …` and the file is left **unchanged**, never partially edited.
- Each `old_string` must match exactly once unless that edit sets `replace_all`.
- The same per-user lock, binary/UTF-8 rejection, `max_file_bytes`, and `quota_ok` checks as `edit_file` apply to the final payload.
- Calls are capped at `multi_edit_max_ops` edits (default 50, `WORKSPACE_TOOL_MULTI_EDIT_MAX_OPS`).

### Attachment opt-in (`attach: true`)

`write_file`, `edit_file`, and `multi_edit` save the touched file without queueing it by default. Each accepts `attach: true` when the file is already the finished deliverable; `queue_file` can attach any workspace file explicitly later. `zip`, `fetch_url`, and `run_code` never auto-queue their outputs, so a separate `queue_file` call is required. Each write tool's `attached` response field reports whether the file is **queued on the reply after the call**. A file already queued stays queued (`attach: false` or an omitted `attach` skips adding, but does not remove), and changing an already-queued path reports `attached: true`. When a file is saved but not queued, the result includes an `attachment_hint` reminding the model how to deliver it.

### Removing a queued attachment (`queue_file` `action: remove`)

The attachment queue is capped (`workspace_tool_max_attachments`, default 5), and explicit write attachments plus automatically attached generated outputs share that cap with `queue_file` calls. `queue_file` with `action: "remove"` un-attaches a queued file and frees its slot, which beats forcing an overwrite of an already-queued path (that would ship the deliverable under a stale filename).

Internally, the queue is the file portion of the frozen per-reply `TurnOutbox`:
paths, optional Discord descriptions, opaque remove IDs and their monotonic
counter, and allowed roots are replaced together through
`MessageContext.update_outbox`. Final delivery staging copies files under the
workspace activity lease and rewrites paths, descriptions, and remove-ID
targets as one new snapshot. `ConversationContext`, `TurnResult`, and the
surface adapter therefore see one coherent value instead of parallel lists.

- `path` accepts the same forms as `add`: a workspace-relative path, a `generated/...` artifact path, an exact absolute path already known internally, or a bare filename. Script-backed skills expose an opaque per-turn `remove_id` alongside each attached basename, and that ID is also accepted as `path`, so duplicate skill-output basenames remain removable without exposing job-output paths. A bare filename must match exactly one queued entry; an ambiguous call returns the matching `remove_id` values.
- Removal works even if the file was deleted from disk after queueing (a dangling entry would otherwise be silently skipped at send time).
- The file backing a pending embed image cannot be removed; the embed must be rebuilt with a different (or no) image first.
- The response mirrors `add`: `removed` plus the post-call `queued_files` list.

### `view_image`

This lets the model look at an image already in the workspace (for example one pulled in via `import_attachment` or unpacked from an archive) on demand, not just the images attached to the triggering message. Because tool results are text-only at the provider boundary, the image cannot ride back as a tool result, so the tool takes a different route:

- It resolves the path through the usual containment chokepoint, enforces `view_image_max_bytes` (default 5 MB, `WORKSPACE_TOOL_VIEW_IMAGE_MAX_BYTES`), and **sniffs the media type from magic bytes** (png/jpeg/gif/webp). It never trusts the extension, since workspace files can be attacker-uploaded.
- It base64-encodes the image onto the per-turn `MessageContext.pending_view_images` rail and returns a small text confirmation, respecting the string tool contract.
- `agent/core.py` drains that rail after tool dispatch into **one synthetic untrusted user-role message**, the same user-role image path Discord attachments use, so the model sees the image on its next step. This works on every image-capable provider without per-provider serializer changes.
- It is gated on `MessageContext.images_supported` (set from the active provider's `IMAGE_INPUT` capability). On a text-only provider the tool refuses cleanly instead of aborting the turn, and `agent/core.py`'s capability check independently validates any injected image part. It is bounded to `view_image_max_per_turn` images per reply (default 4, `WORKSPACE_TOOL_VIEW_IMAGE_MAX_PER_TURN`); that cap is snapshotted into the turn's shared `TurnBudget`, and each accepted image consumes `BudgetName.VIEW_IMAGES`.
- The injected image is in-turn only. `turn_messages` is local to the ReAct loop and is never written to the SQLite transcript, so images do not accumulate across turns.

## The `plan` tool (in-turn checklist)

`tools/plan.py` registers `plan`, a general per-turn affordance rather than a file tool. The model uses it on multi-step replies to lay out an ordered checklist and update step statuses (`pending`/`in_progress`/`completed`) as it goes. Think of it as the bot's own working memory for **one reply**:

- The handler stashes the checklist on `MessageContext.plan` and echoes it back so the model can re-read it across ReAct iterations.
- `MessageContext.plan` is per-turn scratch. It dies when the turn returns and sits structurally outside both the final-reply `TurnOutbox` and the SQLite-persisted transcript, since only real Discord messages are persisted; tool results and `MessageContext` fields are not.
- **The user sees it live.** After each dispatch that rebinds the plan, `agent/core.py` emits it through `agent/activity.py:emit_plan_update` (`SupportsPlanUpdates`, the sibling of the narration-step protocol), and the mention/reply activity surface renders it as muted `-#` checklist lines below the narration block: `✓ N done` collapse, `→` marks the current step, and `○` marks the next pending steps. `_format_plan_block`'s degradation ladder self-caps the block so it always fits the 2,000-char message. The block is live-only: it survives the stale "still thinking…" render but is dropped from the finished activity log, and a plan-only turn still deletes the throwaway status message. On moderated tiers, `_ModeratedActivityReporter` output-checks step contents before forwarding.
- **It survives compaction.** Both compactor entry points take the current plan and re-append it verbatim (as the tool-echo JSON) to the progress note; see [compaction.md](compaction.md).
- It is not remembered across turns, and it is bounded by `MAX_PLAN_STEPS` (30) and `MAX_PLAN_STEP_CHARS` (200; overlong content is clipped).

A typical plan update looks like:

```json
{
  "steps": [
    {"status": "completed", "step": "Read the failing test"},
    {"status": "in_progress", "step": "Identify the regression"},
    {"status": "pending", "step": "Patch the source"},
    {"status": "pending", "step": "Re-run the test suite"}
  ]
}
```

Each call replaces the previous checklist. The handler echoes the new state in its result so the model sees the updated version in the next iteration.

## Failure modes

The error surface is what the model sees in tool results. A few common shapes:

- **Quota exceeded.** The error reports used/limit bytes plus the remediation "delete files you don't need". The call refuses and the file is not written. The model can list the workspace, decide what to drop, and retry.
- **Symlink in path.** `resolve_user_file_path` rejects with a clear "symlink found in path" message. The model should rename the link or work around it.
- **Path too deep.** Anything over `MAX_PATH_DEPTH` (40) parts is refused with the exact depth. Move the file or pick a shorter path.
- **Regenerable env dir.** `EnvDirWriteError` instead of a quota failure, so the model doesn't enter a delete-and-retry loop. Pick a different path or use `run_code` to manage the env dir inside the sandbox.
- **Entry cap.** `max_workspace_entries` is exceeded: the call refuses, but existing files are still editable. Delete intermediate files before creating new ones.
- **Bad arguments.** `offset: "abc"` errors instead of reading from line 1; `attach: "flase"` errors instead of silently un-attaching a deliverable; `read_file` past EOF says so instead of returning the last line. The model corrects the argument and retries.
- **Grep timeout.** `GrepTimeoutError` surfaces as a time-budget error. The model should narrow the pattern (less catastrophic shape) or scope `path` to a smaller directory.
- **Image gating.** `view_image` on a text-only provider refuses cleanly instead of aborting the turn. The model should describe the image differently or stop using `view_image`.

## Configuration

The workspace has separate admission and retention controls. `WORKSPACE_TOOL_MAX_USER_BYTES` is the ordinary-document admission ceiling used by write tools and the code-execution sandbox. `WORKSPACE_MAX_SIZE_MB` is the background sweeper's retained-size target: outside an active workspace lease, the sweeper removes the oldest non-environment files until the tree returns to that target. It is not the in-flight sandbox byte ceiling. Regenerable `.venv`/`.pio` trees remain outside both ordinary-document calculations and use their own byte and entry allowances.

The caps live in `WorkspaceToolConfig` (`tools/workspace/config.py`), plumbed from `Settings` via `app/tools.py:_workspace_tool_config` and documented in `.env.example`. The relevant keys include `WORKSPACE_TOOL_MAX_FILE_BYTES`, `WORKSPACE_TOOL_MAX_USER_BYTES`, the grep family (`WORKSPACE_TOOL_DEFAULT_GREP_RESULTS`, `WORKSPACE_TOOL_MAX_GREP_RESULTS`, `WORKSPACE_TOOL_MAX_GREP_CONTEXT`, `WORKSPACE_TOOL_MAX_GREP_LINE_CHARS`, `WORKSPACE_TOOL_MAX_GREP_PATTERN_CHARS`, `WORKSPACE_TOOL_GREP_TIMEOUT_SECONDS`), `WORKSPACE_TOOL_GLOB_MAX_RESULTS`, `WORKSPACE_TOOL_MULTI_EDIT_MAX_OPS`, `WORKSPACE_TOOL_VIEW_IMAGE_MAX_BYTES` / `WORKSPACE_TOOL_VIEW_IMAGE_MAX_PER_TURN`, `WORKSPACE_TOOL_MAX_ATTACHMENTS`, `WORKSPACE_TOOL_MAX_ENTRIES` (the `max_workspace_entries` cap above), and the env-dir allowances `CODE_EXEC_ENV_DIR_MAX_MB` / `CODE_EXEC_ENV_DIR_MAX_FILES`.
