from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

from workspace import WorkspaceKey, ENV_DIR_NAMES, WorkspaceManager, WorkspacePathSymlinkError
from tools.downloads import safe_filename
from tools.output_queue import (
    AttachmentLimitError,
    enqueue_context_generated_file,
    enqueue_workspace_file,
    match_already_queued,
    match_output_file_remove_id,
    output_file_remove_id,
    queued_file_paths,
    requeue_moved_output,
    unqueue_output_file,
    unqueue_removed_outputs,
)
from tools.registry import MessageContext, ToolRegistry
from trust.tiers import TrustTier

from .common import (
    ATTACHMENT_HINT,
    UserLocks,
    as_bool,
    available_destination,
    clamped_int,
    delete_tree_with_entry_cap,
    ensure_not_env_dir,
    ensure_quota,
    format_quota_error,
    quota_ok,
    read_text_file,
    scrub_user_paths,
    tool_error,
    try_enqueue_workspace_file,
    workspace_activity,
)
from .config import DEFAULT_READ_LINE_LIMIT, WorkspaceToolConfig


@dataclass(frozen=True)
class FileToolDeps:
    """Dependencies shared by file-tool handlers.

    Registration binds them with ``functools.partial``, keeping handlers
    independently typed and testable.
    """

    workspace_manager: WorkspaceManager
    config: WorkspaceToolConfig
    locks: UserLocks

    def scrubbed_error(self, e: object, workspace_key: WorkspaceKey) -> str:
        # OSError text includes absolute server paths; never echo those.
        return tool_error(scrub_user_paths(str(e), self.workspace_manager, workspace_key))


def _read_file_sync(deps: FileToolDeps, args: dict, ctx: MessageContext) -> str:
    path_arg = str(args.get("path", "")).strip()
    if not path_arg:
        return tool_error("path is required")
    try:
        path = deps.workspace_manager.resolve_user_file_path(
            ctx.workspace_key,
            path_arg,
            must_exist=True,
        )
        if not path.is_file():
            return tool_error("path is not a file")
        content = read_text_file(path, deps.config)
        lines = content.splitlines()
        total = len(lines)
        offset = clamped_int(args.get("offset"), name="offset", default=1, minimum=-total or -1)
        limit = clamped_int(
            args.get("limit"),
            name="limit",
            default=DEFAULT_READ_LINE_LIMIT,
            minimum=1,
        )
        rel = deps.workspace_manager.relative_user_file_path(ctx.workspace_key, path)
        if total and offset > total:
            return tool_error(f"offset {offset} is past the end of {rel} ({total} lines)")
        start = total + offset if offset < 0 else max(offset - 1, 0)
        start = min(max(start, 0), max(total - 1, 0))
        selected = lines[start : start + limit]
        # Plain text, not JSON: models consume real newlines far more
        # reliably (and cheaply) than \n-escaped strings, and the header
        # line keeps the path + range machine-readable enough.
        if not selected:
            return f"{rel}: empty file"
        numbered = "\n".join(
            f"{line_number}: {line}" for line_number, line in enumerate(selected, start=start + 1)
        )
        truncated = len(numbered) > deps.config.max_text_chars
        if truncated:
            numbered = numbered[: deps.config.max_text_chars]
        # Count lines after the char cap so the header never claims more
        # than the body actually shows (the last line may be partial).
        shown = numbered.count("\n") + 1
        header = f"{rel}: lines {start + 1}-{start + shown} of {total}"
        if truncated:
            numbered += "\n[TRUNCATED]"
        return header + "\n" + numbered
    except Exception as e:
        return deps.scrubbed_error(e, ctx.workspace_key)


async def _read_file(deps: FileToolDeps, args: dict, ctx: MessageContext) -> str:
    async with workspace_activity(deps.locks, ctx):
        return await asyncio.to_thread(_read_file_sync, deps, args, ctx)


def _write_file_sync(
    deps: FileToolDeps,
    path_arg: str,
    content: str,
    workspace_key: WorkspaceKey,
) -> tuple[Path, str, int] | str:
    """Validate and write one workspace file while off the shared event loop."""

    payload = content.encode("utf-8")
    if len(payload) > deps.config.max_file_bytes:
        return tool_error(f"File exceeds maximum size of {deps.config.max_file_bytes} bytes")
    path = deps.workspace_manager.resolve_user_file_path(workspace_key, path_arg)
    ensure_quota(
        deps.workspace_manager,
        workspace_key,
        new_size=len(payload),
        destination=path,
        temp_path=None,
        max_user_bytes=deps.config.max_user_bytes,
        max_entries=deps.config.max_workspace_entries,
    )
    # Write-time re-check (documented defense-in-depth on every write path): a
    # target swapped to a symlink/dir after resolution is refused rather than
    # followed or crashed on.
    if path.exists() and (path.is_symlink() or not path.is_file()):
        return tool_error("path is not a file")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    rel = deps.workspace_manager.relative_user_file_path(workspace_key, path)
    return path, rel, len(payload)


async def _write_file(deps: FileToolDeps, args: dict, ctx: MessageContext) -> str:
    path_arg = str(args.get("path", "")).strip()
    content_arg = args.get("content")
    try:
        attach = as_bool(args.get("attach"), name="attach", default=False)
    except ValueError as e:
        return tool_error(str(e))
    if not path_arg:
        return tool_error("path is required")
    if not isinstance(content_arg, str):
        return tool_error("content must be a string")
    try:
        async with workspace_activity(deps.locks, ctx):
            outcome = await asyncio.to_thread(
                _write_file_sync,
                deps,
                path_arg,
                content_arg,
                ctx.workspace_key,
            )
            if isinstance(outcome, str):
                return outcome
            path, rel, size_bytes = outcome
            if attach:
                try_enqueue_workspace_file(ctx, deps.workspace_manager, path, deps.config)
            attached = _is_on_attachment_rail(ctx, deps.workspace_manager, rel)
            result: dict[str, object] = {
                "path": rel,
                "size_bytes": size_bytes,
                "attached": attached,
            }
            if not attached:
                result["attachment_hint"] = ATTACHMENT_HINT
            return json.dumps(result)
    except Exception as e:
        return deps.scrubbed_error(e, ctx.workspace_key)


def _display_queued_path(deps: FileToolDeps, ctx: MessageContext, entry: str) -> str:
    path = Path(entry)
    try:
        return deps.workspace_manager.relative_user_file_path(ctx.workspace_key, path)
    except ValueError:
        pass
    try:
        return deps.workspace_manager.relative_generated_file_path(path)
    except ValueError:
        return path.name


def _remove_queued_file(deps: FileToolDeps, ctx: MessageContext, path_arg: str) -> str:
    requested = Path(path_arg)
    entry = match_output_file_remove_id(ctx, path_arg)
    candidates: list[str] = []
    if entry is None and requested.is_absolute():
        candidates.append(str(requested))
    elif entry is None:
        with contextlib.suppress(Exception):
            candidates.append(
                str(deps.workspace_manager.resolve_user_file_path(ctx.workspace_key, path_arg))
            )
        if ctx.context_key and path_arg.startswith("generated/"):
            with contextlib.suppress(Exception):
                candidates.append(
                    str(
                        deps.workspace_manager.resolve_context_generated_file(
                            path_arg,
                            context_key=ctx.context_key,
                        ).path
                    )
                )
    entry = entry or next((c for c in candidates if c in ctx.output_files), None)
    if entry is None and not requested.is_absolute() and requested.name == path_arg:
        matches = [q for q in ctx.output_files if Path(q).name == path_arg]
        if len(matches) > 1:
            return json.dumps(
                {
                    "error": (
                        f"'{path_arg}' matches more than one attached file; pass "
                        "one of the matching remove_id values as path"
                    ),
                    "matches": [
                        {
                            "path": _display_queued_path(deps, ctx, match),
                            "remove_id": output_file_remove_id(ctx, match),
                        }
                        for match in matches
                    ],
                }
            )
        entry = matches[0] if matches else None
    if entry is None:
        return json.dumps(
            {
                "path": requested.name if requested.is_absolute() else path_arg,
                "removed": False,
                "queued_files": queued_file_paths(
                    ctx,
                    deps.workspace_manager,
                    ctx.workspace_key,
                ),
                "error": "file is not attached",
            }
        )
    embed = ctx.embed_attachment
    if embed is not None and str(Path(embed.path).resolve(strict=False)) == entry:
        return tool_error(
            "that file is the pending embed's image; rebuild the embed with a "
            "different image (or none) before removing it"
        )
    unqueue_output_file(ctx, entry)
    return json.dumps(
        {
            "path": _display_queued_path(deps, ctx, entry),
            "removed": True,
            "queued_files": queued_file_paths(ctx, deps.workspace_manager, ctx.workspace_key),
        }
    )


async def _queue_file_unlocked(deps: FileToolDeps, args: dict, ctx: MessageContext) -> str:
    path_arg = str(args.get("path", "")).strip()
    action = str(args.get("action") or "add").strip().lower()
    if action not in {"add", "remove"}:
        return tool_error("action must be 'add' or 'remove'")
    if not path_arg:
        return tool_error("path is required")
    if action == "remove":
        return _remove_queued_file(deps, ctx, path_arg)
    if Path(path_arg).is_absolute():
        already = match_already_queued(ctx, path_arg)
        if already is not None:
            return _already_attached_payload(ctx, already, deps.workspace_manager)
        return tool_error("Path must be relative to your workspace")
    try:
        try:
            path = deps.workspace_manager.resolve_user_file_path(
                ctx.workspace_key,
                path_arg,
                must_exist=True,
            )
        except FileNotFoundError:
            already = match_already_queued(ctx, path_arg)
            if already is not None:
                return _already_attached_payload(ctx, already, deps.workspace_manager)
            if not path_arg.startswith("generated/"):
                raise
        else:
            if path.is_symlink() or not path.is_file():
                return tool_error("path is not a file")
            relative_path = deps.workspace_manager.relative_user_file_path(
                ctx.workspace_key,
                path,
            )
            enqueue_workspace_file(
                ctx,
                deps.workspace_manager,
                path,
                max_attachments=deps.config.max_attachments,
            )
            return json.dumps(
                {
                    "path": relative_path,
                    "queued": True,
                    "queued_files": queued_file_paths(
                        ctx,
                        deps.workspace_manager,
                        ctx.workspace_key,
                    ),
                }
            )

        queued = enqueue_context_generated_file(
            ctx,
            deps.workspace_manager,
            path_arg,
            max_attachments=deps.config.max_attachments,
        )
        return json.dumps(
            {
                "path": deps.workspace_manager.relative_generated_file_path(queued.path),
                "queued": True,
                "queued_files": queued_file_paths(
                    ctx,
                    deps.workspace_manager,
                    ctx.workspace_key,
                ),
            }
        )
    except AttachmentLimitError as e:
        return json.dumps(
            {
                "path": path_arg,
                "queued": False,
                "queued_files": queued_file_paths(
                    ctx,
                    deps.workspace_manager,
                    ctx.workspace_key,
                ),
                "error": str(e),
            }
        )
    except Exception as e:
        return deps.scrubbed_error(e, ctx.workspace_key)


async def _queue_file(deps: FileToolDeps, args: dict, ctx: MessageContext) -> str:
    async with workspace_activity(deps.locks, ctx):
        return await _queue_file_unlocked(deps, args, ctx)


def _list_workspace_sync(deps: FileToolDeps, args: dict, ctx: MessageContext) -> str:
    try:
        root = deps.workspace_manager.resolve_user_file_path(
            ctx.workspace_key,
            str(args.get("path", "")).strip(),
            allow_root=True,
            must_exist=bool(args.get("path")),
        )
        if not root.is_dir():
            return tool_error("path is not a directory")
        entries = []
        for child in sorted(
            root.iterdir(),
            key=lambda p: (not p.is_dir(), p.name.lower()),
        ):
            if child.is_symlink():
                continue
            # Hide regenerable env dirs (.venv/.pio); explicit paths still list.
            if child.name in ENV_DIR_NAMES:
                continue
            try:
                if child.is_dir():
                    entries.append(
                        {
                            "name": child.name,
                            "type": "directory",
                            "size_bytes": 0,
                        }
                    )
                elif child.is_file():
                    entries.append(
                        {
                            "name": child.name,
                            "type": "file",
                            "size_bytes": child.stat().st_size,
                        }
                    )
            except OSError:
                continue
        return json.dumps(
            {
                "path": deps.workspace_manager.relative_user_file_path(
                    ctx.workspace_key,
                    root,
                ),
                "entries": entries,
            }
        )
    except Exception as e:
        return deps.scrubbed_error(e, ctx.workspace_key)


async def _list_workspace(deps: FileToolDeps, args: dict, ctx: MessageContext) -> str:
    async with workspace_activity(deps.locks, ctx):
        return await asyncio.to_thread(_list_workspace_sync, deps, args, ctx)


async def _delete_file(deps: FileToolDeps, args: dict, ctx: MessageContext) -> str:
    path_arg = str(args.get("path", "")).strip()
    try:
        recursive = as_bool(args.get("recursive"), name="recursive", default=False)
    except ValueError as e:
        return tool_error(str(e))
    if not path_arg:
        return tool_error("path is required")
    try:
        async with workspace_activity(deps.locks, ctx):
            try:
                path = deps.workspace_manager.resolve_user_file_path(
                    ctx.workspace_key,
                    path_arg,
                    must_exist=True,
                )
            except WorkspacePathSymlinkError:
                # A host-side process can leave symlinks the normal resolver
                # refuses; unlink the link itself (never followed) so cleanup
                # is not a dead end.
                link = deps.workspace_manager.resolve_user_symlink_entry(
                    ctx.workspace_key, path_arg
                )
                link.unlink()
                return json.dumps({"path": path_arg, "deleted": True, "was_symlink": True})
            root = deps.workspace_manager.user_files_dir(ctx.workspace_key).resolve()
            if path == root:
                return tool_error("Cannot delete the workspace root")
            rel = deps.workspace_manager.relative_user_file_path(ctx.workspace_key, path)
            if path.is_dir():
                if recursive:
                    entry_count = await asyncio.to_thread(
                        delete_tree_with_entry_cap,
                        path,
                        deps.config.max_zip_entries,
                    )
                    if entry_count > deps.config.max_zip_entries:
                        return tool_error(
                            f"{rel} has {entry_count} entries; recursive delete "
                            f"limit is {deps.config.max_zip_entries}"
                        )
                else:
                    path.rmdir()
            elif path.is_file():
                path.unlink()
            else:
                return tool_error("path is not a file or empty directory")
            # A queued attachment pointing at a deleted path would fail the
            # whole reply's file staging; drop stale entries and say so.
            unqueued = unqueue_removed_outputs(ctx, path)
            payload: dict[str, object] = {"path": rel, "deleted": True}
            if unqueued:
                payload["unattached"] = True
            return json.dumps(payload)
    except OSError as e:
        return tool_error(
            "Could not delete path: "
            + scrub_user_paths(str(e), deps.workspace_manager, ctx.workspace_key)
        )
    except Exception as e:
        return deps.scrubbed_error(e, ctx.workspace_key)


async def _edit_file(deps: FileToolDeps, args: dict, ctx: MessageContext) -> str:
    path_arg = str(args.get("path", "")).strip()
    old_string = args.get("old_string")
    new_string = args.get("new_string")
    try:
        replace_all = as_bool(args.get("replace_all"), name="replace_all", default=False)
        attach = as_bool(args.get("attach"), name="attach", default=False)
    except ValueError as e:
        return tool_error(str(e))
    if not path_arg:
        return tool_error("path is required")
    if not isinstance(old_string, str) or not isinstance(new_string, str):
        return tool_error("old_string and new_string must be strings")
    if old_string == "":
        return tool_error("old_string must not be empty")
    if old_string == new_string:
        return tool_error("old_string and new_string are identical, so nothing would change")
    try:
        async with workspace_activity(deps.locks, ctx):
            path, rel, outcome = await asyncio.to_thread(
                _edit_file_sync,
                deps,
                ctx.workspace_key,
                path_arg,
                old_string,
                new_string,
                replace_all,
            )
            if "error" in outcome:
                return tool_error(str(outcome["error"]))
            if attach:
                try_enqueue_workspace_file(ctx, deps.workspace_manager, path, deps.config)
            attached = _is_on_attachment_rail(ctx, deps.workspace_manager, rel)
            result: dict[str, object] = {**outcome, "attached": attached}
            if not attached:
                result["attachment_hint"] = ATTACHMENT_HINT
            return json.dumps(result)
    except Exception as e:
        return deps.scrubbed_error(e, ctx.workspace_key)


async def _import_attachment(deps: FileToolDeps, args: dict, ctx: MessageContext) -> str:
    filename = str(args.get("filename", "")).strip()
    dest_arg = args.get("dest")
    if not filename:
        return tool_error("filename must not be empty")
    if not ctx.attachments:
        return tool_error("no files attached to this message")
    matches = [a for a in ctx.attachments if a.filename == filename]
    if not matches:
        available = ", ".join(a.filename for a in ctx.attachments)
        return tool_error(f"no attachment named {filename} on this message; available: {available}")
    if len(matches) > 1:
        return tool_error(
            f"multiple attachments named {filename}; rename and re-send so it's unambiguous"
        )
    attachment = matches[0]
    try:
        # Network read happens before the lease: holding every workspace's
        # maintenance barrier hostage to a slow Discord CDN read stalls
        # unrelated users' tools.
        try:
            payload = await read_attachment_payload(
                attachment,
                max_import_bytes=deps.config.max_import_bytes,
            )
        except ValueError as e:
            return tool_error(str(e))
        async with workspace_activity(deps.locks, ctx):
            outcome = await asyncio.to_thread(
                import_attachment_payload_sync,
                deps,
                ctx.workspace_key,
                dest_arg,
                filename,
                payload,
            )
            if "error" in outcome:
                return tool_error(str(outcome["error"]))
            return json.dumps(outcome)
    except Exception as e:
        return deps.scrubbed_error(e, ctx.workspace_key)


async def read_attachment_payload(attachment: Any, *, max_import_bytes: int) -> bytes:
    """Read one importable attachment with the workspace tool's byte limits."""

    filename = str(attachment.filename)
    if attachment.size > max_import_bytes:
        raise ValueError(
            f"attachment {filename} is {attachment.size} bytes, "
            f"over the {max_import_bytes} byte import limit"
        )
    try:
        payload = await attachment.read()
    except Exception as exc:
        raise ValueError(f"failed to read attachment {filename}: {exc}") from exc
    if len(payload) > max_import_bytes:
        raise ValueError(
            f"attachment {filename} is {len(payload)} bytes, "
            f"over the {max_import_bytes} byte import limit"
        )
    return payload


def import_attachment_payload_sync(
    deps: FileToolDeps,
    workspace_key: WorkspaceKey,
    dest_arg: object,
    filename: str,
    payload: bytes,
) -> dict[str, object]:
    """Finalize an already-downloaded Discord attachment off the event loop."""

    if isinstance(dest_arg, str) and dest_arg.strip():
        destination = deps.workspace_manager.resolve_user_file_path(
            workspace_key,
            dest_arg.strip(),
        )
        if destination.exists():
            return {
                "error": f"{dest_arg.strip()} already exists (choose another dest or delete it)"
            }
    else:
        destination = available_destination(
            deps.workspace_manager,
            workspace_key,
            f"imports/{safe_filename(filename)}",
        )
    if not quota_ok(
        deps.workspace_manager,
        workspace_key,
        new_size=len(payload),
        destination=destination,
        temp_path=None,
        max_user_bytes=deps.config.max_user_bytes,
        max_entries=deps.config.max_workspace_entries,
    ):
        used = deps.workspace_manager.user_files_size(workspace_key)
        return {
            "error": f"importing {filename} " + format_quota_error(used, deps.config.max_user_bytes)
        }
    user_root = deps.workspace_manager.user_files_dir(workspace_key)
    temp_path = user_root / f".import-{uuid.uuid4().hex}.part"
    try:
        temp_path.write_bytes(payload)
        if not quota_ok(
            deps.workspace_manager,
            workspace_key,
            new_size=len(payload),
            destination=destination,
            temp_path=temp_path,
            max_user_bytes=deps.config.max_user_bytes,
            max_entries=deps.config.max_workspace_entries,
        ):
            used = deps.workspace_manager.user_files_size(workspace_key)
            return {
                "error": f"importing {filename} "
                + format_quota_error(used, deps.config.max_user_bytes)
            }
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp_path.replace(destination)
    finally:
        temp_path.unlink(missing_ok=True)
    return {
        "path": deps.workspace_manager.relative_user_file_path(workspace_key, destination),
        "size_bytes": len(payload),
    }


async def _multi_edit(deps: FileToolDeps, args: dict, ctx: MessageContext) -> str:
    path_arg = str(args.get("path", "")).strip()
    edits = args.get("edits")
    try:
        attach = as_bool(args.get("attach"), name="attach", default=False)
    except ValueError as e:
        return tool_error(str(e))
    if not path_arg:
        return tool_error("path is required")
    if not isinstance(edits, list) or not edits:
        return tool_error("edits must be a non-empty list")
    if len(edits) > deps.config.multi_edit_max_ops:
        return tool_error(
            f"multi_edit accepts at most {deps.config.multi_edit_max_ops} edits per call"
        )
    # Validate the shape of every edit up front, before touching the file, so
    # a malformed hunk can never cause a partial in-memory apply.
    parsed: list[tuple[str, str, bool]] = []
    for index, edit in enumerate(edits, start=1):
        if not isinstance(edit, dict):
            return tool_error(f"edit {index}: must be an object")
        old_string = edit.get("old_string")
        new_string = edit.get("new_string")
        try:
            replace_all = as_bool(
                edit.get("replace_all"),
                name=f"edit {index}: replace_all",
                default=False,
            )
        except ValueError as e:
            return tool_error(str(e))
        if not isinstance(old_string, str) or not isinstance(new_string, str):
            return tool_error(f"edit {index}: old_string and new_string must be strings")
        if old_string == "":
            return tool_error(f"edit {index}: old_string must not be empty")
        if old_string == new_string:
            return tool_error(
                f"edit {index}: old_string and new_string are identical, so nothing would change"
            )
        parsed.append((old_string, new_string, replace_all))
    try:
        async with workspace_activity(deps.locks, ctx):
            path = deps.workspace_manager.resolve_user_file_path(
                ctx.workspace_key,
                path_arg,
                must_exist=True,
            )
            rel = deps.workspace_manager.relative_user_file_path(ctx.workspace_key, path)
            # Offload the read-modify-write off the event loop: up to
            # multi_edit_max_ops passes of count+replace over a file as large
            # as max_file_bytes is CPU the shared loop must not absorb, just
            # like the grep/glob walks. Stays inside the per-user lock so the
            # whole apply is still serialized against other writes.
            outcome = await asyncio.to_thread(
                apply_multi_edit_to_file,
                path,
                rel,
                parsed,
                deps.config,
                workspace_manager=deps.workspace_manager,
                workspace_key=ctx.workspace_key,
            )
            if "error" in outcome:
                return tool_error(str(outcome["error"]))
            if attach:
                try_enqueue_workspace_file(ctx, deps.workspace_manager, path, deps.config)
            attached = _is_on_attachment_rail(ctx, deps.workspace_manager, rel)
            result: dict[str, object] = {**outcome, "attached": attached}
            if not attached:
                result["attachment_hint"] = ATTACHMENT_HINT
            return json.dumps(result)
    except Exception as e:
        return deps.scrubbed_error(e, ctx.workspace_key)


async def _move_file(deps: FileToolDeps, args: dict, ctx: MessageContext) -> str:
    src_arg = str(args.get("path", "")).strip()
    dest_arg = str(args.get("dest", "")).strip()
    if not src_arg:
        return tool_error("path is required")
    if not dest_arg:
        return tool_error("dest is required")
    try:
        async with workspace_activity(deps.locks, ctx):
            src = deps.workspace_manager.resolve_user_file_path(
                ctx.workspace_key,
                src_arg,
                must_exist=True,
            )
            root = deps.workspace_manager.user_files_dir(ctx.workspace_key).resolve()
            if src == root:
                return tool_error("Cannot move the workspace root")
            if src.is_symlink() or not (src.is_file() or src.is_dir()):
                return tool_error("path is not a file or directory")
            dest = deps.workspace_manager.resolve_user_file_path(ctx.workspace_key, dest_arg)
            # Moves can place files as effectively as writes: keep both ends
            # out of the reserved environment dirs or the doc quota (which
            # excludes env-dir bytes) becomes evadable via rename.
            ensure_not_env_dir(deps.workspace_manager, ctx.workspace_key, src)
            ensure_not_env_dir(deps.workspace_manager, ctx.workspace_key, dest)
            if dest == src:
                return tool_error("path and dest are the same")
            if dest.exists():
                return tool_error(f"{dest_arg} already exists (choose another dest or delete it)")
            if src.is_dir() and dest.is_relative_to(src):
                return tool_error("Cannot move a directory inside itself")
            rel_src = deps.workspace_manager.relative_user_file_path(ctx.workspace_key, src)
            dest.parent.mkdir(parents=True, exist_ok=True)
            src.rename(dest)
            # Keep the attachment rail pointing at live paths (a dangling
            # queued entry fails the whole reply's file staging).
            requeued = requeue_moved_output(ctx, src, dest)
            rel_dest = deps.workspace_manager.relative_user_file_path(ctx.workspace_key, dest)
            payload: dict[str, object] = {
                "from": rel_src,
                "to": rel_dest,
                "moved": True,
            }
            if requeued:
                payload["attachments_updated"] = requeued
            return json.dumps(payload)
    except Exception as e:
        return deps.scrubbed_error(e, ctx.workspace_key)


def register_file_tools(
    registry: ToolRegistry,
    workspace_manager: WorkspaceManager,
    config: WorkspaceToolConfig,
    locks: UserLocks,
) -> None:
    deps = FileToolDeps(workspace_manager=workspace_manager, config=config, locks=locks)
    registry.register(
        name="import_attachment",
        description=(
            "Save a file attached to the current Discord message into your workspace, "
            "selected by its exact filename."
        ),
        parameters={
            "type": "object",
            "properties": {
                "filename": {
                    "type": "string",
                    "description": "Exact name of the attached file to import.",
                },
                "dest": {
                    "type": "string",
                    "description": (
                        "Optional workspace-relative destination path. "
                        "Defaults to a sanitized unused path under imports/."
                    ),
                },
            },
            "required": ["filename"],
        },
        handler=partial(_import_attachment, deps),
        min_tier=TrustTier.MEMBER,
    )
    registry.register(
        name="edit_file",
        description=(
            "Replace text in an existing workspace text file by exact string match. "
            "old_string must match exactly once unless replace_all is true. The changed "
            "file is not attached by default; pass attach: true or call queue_file later."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workspace-relative file path."},
                "old_string": {"type": "string", "description": "Exact text to replace."},
                "new_string": {"type": "string", "description": "Replacement text."},
                "replace_all": {
                    "type": "boolean",
                    "description": (
                        "Replace every occurrence instead of requiring a unique match."
                    ),
                },
                "attach": _attach_property(),
            },
            "required": ["path", "old_string", "new_string"],
        },
        handler=partial(_edit_file, deps),
        min_tier=TrustTier.MEMBER,
    )
    registry.register(
        name="multi_edit",
        description=(
            "Apply several exact-string edits to one workspace text file in a "
            "single call. Edits apply in order, each to the result of the "
            "previous; if any edit's old_string is missing or ambiguous the "
            "whole call fails and the file is left unchanged. Each old_string "
            "must match exactly once unless that edit sets replace_all. The changed file "
            "is not attached by default; pass attach: true or call queue_file later."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workspace-relative file path."},
                "edits": {
                    "type": "array",
                    "description": "Ordered list of edits to apply to the file.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "old_string": {
                                "type": "string",
                                "description": "Exact text to replace.",
                            },
                            "new_string": {
                                "type": "string",
                                "description": "Replacement text.",
                            },
                            "replace_all": {
                                "type": "boolean",
                                "description": (
                                    "Replace every occurrence of old_string in this "
                                    "edit instead of requiring a unique match."
                                ),
                            },
                        },
                        "required": ["old_string", "new_string"],
                    },
                },
                "attach": _attach_property(),
            },
            "required": ["path", "edits"],
        },
        handler=partial(_multi_edit, deps),
        min_tier=TrustTier.MEMBER,
    )
    registry.register(
        name="read_file",
        description=(
            "Read a text file from your workspace. Returns a 'path: lines A-B "
            "of N' header followed by numbered lines."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Workspace-relative file path.",
                },
                "offset": {
                    "type": "integer",
                    "description": (
                        "First line to read; negative counts from the end (-200 = last 200 lines)."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of lines to read.",
                },
            },
            "required": ["path"],
        },
        handler=partial(_read_file, deps),
        min_tier=TrustTier.MEMBER,
        untrusted=True,
    )
    registry.register(
        name="write_file",
        description=(
            "Create or overwrite a text file in your workspace. Files are not attached "
            "by default; pass attach: true to include a deliverable with the final reply, "
            "or call queue_file later."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Workspace-relative file path.",
                },
                "content": {"type": "string", "description": "Text content to write."},
                "attach": _attach_property(),
            },
            "required": ["path", "content"],
        },
        handler=partial(_write_file, deps),
        min_tier=TrustTier.MEMBER,
    )
    registry.register(
        name="move_file",
        description=(
            "Move or rename a file or directory inside your workspace. The "
            "destination must not already exist; parent directories are created."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Workspace-relative source path.",
                },
                "dest": {
                    "type": "string",
                    "description": "Workspace-relative destination path.",
                },
            },
            "required": ["path", "dest"],
        },
        handler=partial(_move_file, deps),
        min_tier=TrustTier.MEMBER,
    )
    registry.register(
        name="queue_file",
        description=(
            "Manage which files are included with the final Discord response. action=add (the "
            "default) attaches an existing workspace file or generated artifact; "
            "action=remove takes a previously attached file back off the reply, "
            "freeing its slot. At most "
            f"{config.max_attachments} files can be attached to one reply "
            "(explicit write-tool attachments and files attached automatically by "
            "generation and rendering tools share this limit); at "
            "the limit, remove a file you attached by mistake to make room for "
            "the one the user actually needs."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Workspace-relative file path or generated/... artifact path. "
                        "For remove, this may instead be a remove_id returned by an "
                        "attached skill output."
                    ),
                },
                "action": {
                    "type": "string",
                    "enum": ["add", "remove"],
                    "description": ("add (default) attaches the file; remove un-attaches it."),
                },
            },
            "required": ["path"],
        },
        handler=partial(_queue_file, deps),
        min_tier=TrustTier.MEMBER,
    )
    registry.register(
        name="list_workspace",
        description="List one level of files and directories in your workspace.",
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Workspace-relative directory path.",
                }
            },
        },
        handler=partial(_list_workspace, deps),
        min_tier=TrustTier.MEMBER,
    )
    registry.register(
        name="delete_file",
        description=(
            "Delete one workspace file or directory. A directory must be empty unless "
            "recursive is true, which removes it and all its contents."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Workspace-relative path to delete.",
                },
                "recursive": {
                    "type": "boolean",
                    "description": (
                        "Delete a non-empty directory and everything inside it. "
                        "Ignored for files. Default false."
                    ),
                },
            },
            "required": ["path"],
        },
        handler=partial(_delete_file, deps),
        min_tier=TrustTier.MEMBER,
    )


def _attach_property() -> dict[str, object]:
    return {
        "type": "boolean",
        "description": (
            "Attach the file to the final reply (default false). Set true only for "
            "a deliverable; queue_file can attach it later."
        ),
    }


def _already_attached_payload(
    ctx: MessageContext,
    queued: str,
    workspace_manager: WorkspaceManager,
) -> str:
    return json.dumps(
        {
            "path": Path(queued).name,
            "queued": True,
            "already_attached": True,
            "queued_files": queued_file_paths(ctx, workspace_manager, ctx.workspace_key),
        }
    )


def _is_on_attachment_rail(
    ctx: MessageContext,
    workspace_manager: WorkspaceManager,
    rel: str,
) -> bool:
    """Whether the file will ride the final reply, not just "queued by this call".

    A file queued by an earlier call stays queued (attach=false skips adding, it
    does not remove), and re-queueing an already-queued path is not a failure, so
    the response field must reflect the rail's current state.
    """
    return rel in queued_file_paths(ctx, workspace_manager, ctx.workspace_key)


def _projected_edit_size(
    current_size_bytes: int,
    old_string: str,
    new_string: str,
    replacements: int,
) -> int:
    return current_size_bytes + replacements * (
        len(new_string.encode("utf-8")) - len(old_string.encode("utf-8"))
    )


def _edit_file_sync(
    deps: FileToolDeps,
    workspace_key: WorkspaceKey,
    path_arg: str,
    old_string: str,
    new_string: str,
    replace_all: bool,
) -> tuple[Path, str, dict[str, object]]:
    """Resolve and apply one exact-string edit off the shared event loop."""

    path = deps.workspace_manager.resolve_user_file_path(
        workspace_key,
        path_arg,
        must_exist=True,
    )
    rel = deps.workspace_manager.relative_user_file_path(workspace_key, path)
    outcome = apply_edit_to_file(
        path,
        rel,
        old_string,
        new_string,
        replace_all,
        deps.config,
        workspace_manager=deps.workspace_manager,
        workspace_key=workspace_key,
    )
    return path, rel, outcome


def apply_edit_to_file(
    path: Path,
    rel: str,
    old_string: str,
    new_string: str,
    replace_all: bool,
    config: WorkspaceToolConfig,
    *,
    workspace_manager: WorkspaceManager,
    workspace_key: WorkspaceKey,
) -> dict[str, object]:
    """Bounded synchronous read-modify-write for ``edit_file``."""

    if path.is_symlink() or not path.is_file():
        return {"error": "path is not a file"}
    if path.stat().st_size > config.max_file_bytes:
        return {"error": f"{rel} is larger than the {config.max_file_bytes} byte edit limit"}
    data = path.read_bytes()
    if b"\x00" in data:
        return {"error": f"{rel} is not a text file"}
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError:
        return {"error": f"{rel} is not a text file"}
    count = content.count(old_string)
    if count == 0:
        return {"error": f"old_string not found in {rel}"}
    if count > 1 and not replace_all:
        return {
            "error": (
                f"old_string found {count} times in {rel}; make it unique or pass replace_all=true"
            )
        }
    replacements = count if replace_all else 1
    projected_size_bytes = _projected_edit_size(
        len(data),
        old_string,
        new_string,
        replacements,
    )
    if projected_size_bytes > config.max_file_bytes:
        return {"error": f"edited file would exceed the {config.max_file_bytes} byte file limit"}
    updated = (
        content.replace(old_string, new_string)
        if replace_all
        else content.replace(old_string, new_string, 1)
    )
    payload = updated.encode("utf-8")
    if len(payload) > config.max_file_bytes:
        return {"error": f"edited file would exceed the {config.max_file_bytes} byte file limit"}
    if not quota_ok(
        workspace_manager,
        workspace_key,
        new_size=len(payload),
        destination=path,
        temp_path=None,
        max_user_bytes=config.max_user_bytes,
    ):
        used = workspace_manager.user_files_size(workspace_key)
        return {"error": "edited file " + format_quota_error(used, config.max_user_bytes)}
    path.write_bytes(payload)
    return {
        "path": rel,
        "replacements": replacements,
        "size_bytes": len(payload),
    }


def apply_multi_edit_to_file(
    path: Path,
    rel: str,
    parsed: list[tuple[str, str, bool]],
    config: WorkspaceToolConfig,
    *,
    workspace_manager: WorkspaceManager,
    workspace_key: WorkspaceKey,
) -> dict[str, object]:
    """Synchronous read-modify-write for multi_edit, run off the event loop.

    Returns ``{"error": msg}`` for any user-facing failure (leaving the file
    untouched), else ``{"path", "edits", "replacements", "size_bytes"}``. The
    caller holds the per-user lock around the to_thread call, so this stays
    serialized against other writes while not blocking the shared loop.

    Two bounds matter here. (1) The is_symlink/is_file re-check happens right
    before the read so a target swapped to a symlink after path resolution is
    still refused (TOCTOU). (2) Each edit's *projected* size is checked BEFORE
    applying it, so a growing replace_all can never build a working buffer past
    max_file_bytes. Without this, chained replacements amplify transient memory
    to GB scale before the final size check.
    """
    if path.is_symlink() or not path.is_file():
        return {"error": "path is not a file"}
    if path.stat().st_size > config.max_file_bytes:
        return {"error": f"{rel} is larger than the {config.max_file_bytes} byte edit limit"}
    data = path.read_bytes()
    if b"\x00" in data:
        return {"error": f"{rel} is not a text file"}
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError:
        return {"error": f"{rel} is not a text file"}
    # Apply every edit in memory, each to the result of the previous; abort the
    # whole call with no write if any hunk is missing, ambiguous, or would grow
    # the file past the limit, so the file is never left partially edited.
    current_size_bytes = len(data)
    total_replacements = 0
    for index, (old_string, new_string, replace_all) in enumerate(parsed, start=1):
        count = content.count(old_string)
        if count == 0:
            return {"error": f"edit {index}: old_string not found in {rel}"}
        if count > 1 and not replace_all:
            return {
                "error": (
                    f"edit {index}: old_string found {count} times in {rel}; "
                    "make it unique or pass replace_all=true"
                )
            }
        replacements = count if replace_all else 1
        projected_size_bytes = _projected_edit_size(
            current_size_bytes,
            old_string,
            new_string,
            replacements,
        )
        if projected_size_bytes > config.max_file_bytes:
            return {
                "error": (
                    f"edit {index}: result would exceed the {config.max_file_bytes} byte file limit"
                )
            }
        if replace_all:
            content = content.replace(old_string, new_string)
        else:
            content = content.replace(old_string, new_string, 1)
        current_size_bytes = projected_size_bytes
        total_replacements += replacements
    payload = content.encode("utf-8")
    if len(payload) > config.max_file_bytes:
        return {"error": f"edited file would exceed the {config.max_file_bytes} byte file limit"}
    if not quota_ok(
        workspace_manager,
        workspace_key,
        new_size=len(payload),
        destination=path,
        temp_path=None,
        max_user_bytes=config.max_user_bytes,
    ):
        used = workspace_manager.user_files_size(workspace_key)
        return {"error": ("edited file " + format_quota_error(used, config.max_user_bytes))}
    path.write_bytes(payload)
    return {
        "path": rel,
        "edits": len(parsed),
        "replacements": total_replacements,
        "size_bytes": len(payload),
    }
