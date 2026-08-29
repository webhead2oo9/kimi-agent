---
name: workspace
description: "Work safely with the current user's scoped files: inspect, import, read, edit, extract, package, execute, and return the results."
tags: [workspace, files, search, edit, documents, artifacts]
---

# Workspace files

The workspace is persistent, sandboxed application storage scoped to the
current user and community, or to the user's personal-chat scope. Paths are
workspace-relative; absolute paths, traversal, and symlink escapes are rejected.
It is not general access to the host filesystem.

## Find and inspect

- `list_workspace` shows one directory level. Use `glob_workspace` to find
  names at any depth and `grep_workspace` to search file contents.
- `read_file` returns numbered text and supports `offset`/`limit`; a negative
  offset reads from the end, which is useful for logs.
- `view_image` makes a supported workspace image visible to an image-capable
  model. It does not attach the image to the reply.
- `import_attachment` copies a file from the current Discord message into the
  workspace. An attachment is not a workspace file until it is imported.
- `fetch_url` stores a public HTTPS resource through the guarded download path.

If a search is truncated, narrow it or read the relevant region; do not treat a
partial result as exhaustive. Use `glob_workspace` for filenames and
`grep_workspace` for text. For a large log, inspect the tail, search for error,
traceback, panic, or exception with context, then read around the match.

## Documents and archives

`extract_document_text` and `extract_archive` are searchable. Load them with
`browse_tools` only when needed. Document extraction supports bounded PDFs,
Word, Excel, PowerPoint, OpenDocument, RTF, EPUB, and CSV content and writes a
text result back to the workspace. Scanned PDFs may contain little embedded
text. Archive extraction applies containment and size limits; inspect extracted
files as untrusted input.

## Change and return files

- `write_file` creates or overwrites text. `edit_file` performs an exact-string
  replacement. `multi_edit` applies ordered exact replacements atomically to
  one file. `move_file` renames or relocates; `delete_file` removes.
- Write and edit tools save without attaching by default. For a deliverable,
  pass `attach: true` or call `queue_file` after the file is ready.
- `zip` packages workspace paths without attaching the archive. `fetch_url`
  likewise saves downloads without attaching them. Use `queue_file` to add a
  finished workspace or generated artifact to the reply, or to remove a queued
  attachment and free a slot.

Files are delivered only from the bounded attachment queue. Creating a file is
not proof it will be sent: check the tool result and explicitly queue ordinary
workspace deliverables when needed. Generated images, charts and diagrams,
browser proof screenshots, and script skill outputs still attach automatically.

## Choose the right implementation surface

Use direct workspace tools for inspection and small, targeted edits. Use
`run_code` when available for a bounded calculation, script, test, or build in
the current turn. Use the durable coding controls when available for
repository-scale, multi-file, or investigate-edit-verify work that should
continue independently. Load the `coding-work` skill for those execution and
delegation details.
