from __future__ import annotations

import asyncio
import json
import shutil
import uuid
import zipfile
from collections.abc import Iterable
from pathlib import Path

from workspace import WorkspaceKey, WorkspaceManager
from tools.archive import (
    ArchiveError,
    ExtractLimits,
    archive_kind,
    default_dest_name,
    safe_extract,
)
from tools.registry import MessageContext, ToolRegistry
from trust.tiers import TrustTier

from .common import (
    UserLocks,
    as_bool,
    count_entries_up_to,
    ensure_not_env_dir,
    format_quota_error,
    in_env_dir,
    quota_ok,
    scrub_user_paths,
    tool_error,
    try_enqueue_workspace_file,
    workspace_activity,
)
from .config import WorkspaceToolConfig


def register_archive_tools(
    registry: ToolRegistry,
    workspace_manager: WorkspaceManager,
    config: WorkspaceToolConfig,
    locks: UserLocks,
) -> None:
    async def _zip(args: dict, ctx: MessageContext) -> str:
        paths_arg = args.get("paths")
        output_arg = str(args.get("output", "")).strip()
        if (
            not isinstance(paths_arg, list)
            or not paths_arg
            or not all(isinstance(p, str) for p in paths_arg)
        ):
            return tool_error("paths must be a non-empty array of strings")
        if not output_arg:
            return tool_error("output is required")
        if not output_arg.endswith(".zip"):
            return tool_error("output must end in .zip")
        try:
            async with workspace_activity(locks, ctx):
                root = workspace_manager.user_files_dir(ctx.workspace_key).resolve()
                output_path = workspace_manager.resolve_user_file_path(
                    ctx.workspace_key,
                    output_arg,
                )
                if output_path.exists():
                    return tool_error(
                        f"output already exists: {output_arg} (delete it or choose another name)"
                    )
                # The rglob/stat scan, DEFLATE write, and quota size walk are
                # disk/CPU-bound (up to max_user_bytes of input); run them off
                # the event loop like _extract_archive's safe_extract, so a
                # large zip cannot stall every concurrent conversation.
                outcome = await asyncio.to_thread(
                    _build_zip,
                    workspace_manager,
                    ctx.workspace_key,
                    [str(p) for p in paths_arg],
                    output_path,
                    root,
                    config,
                )
                if isinstance(outcome, str):
                    return outcome
                entry_count, size = outcome
                attached = try_enqueue_workspace_file(
                    ctx,
                    workspace_manager,
                    output_path,
                    config,
                )
                return json.dumps(
                    {
                        "path": workspace_manager.relative_user_file_path(
                            ctx.workspace_key,
                            output_path,
                        ),
                        "size_bytes": size,
                        "entry_count": entry_count,
                        "attached": attached,
                    }
                )
        except Exception as e:
            return tool_error(scrub_user_paths(str(e), workspace_manager, ctx.workspace_key))

    async def _extract_archive(args: dict, ctx: MessageContext) -> str:
        path_arg = str(args.get("path", "")).strip()
        dest_arg = args.get("dest")
        try:
            strip_top_level = as_bool(
                args.get("strip_top_level"), name="strip_top_level", default=True
            )
        except ValueError as e:
            return tool_error(str(e))
        if not path_arg:
            return tool_error("path is required")
        try:
            async with workspace_activity(locks, ctx):
                archive = workspace_manager.resolve_user_file_path(
                    ctx.workspace_key,
                    path_arg,
                    must_exist=True,
                )
                if archive.is_symlink() or not archive.is_file():
                    return tool_error("path is not a file")
                if archive_kind(archive.name) is None:
                    return tool_error("unsupported archive type; use .tar.gz, .tgz, or .zip")
                if isinstance(dest_arg, str) and dest_arg.strip():
                    dest = workspace_manager.resolve_user_file_path(
                        ctx.workspace_key,
                        dest_arg.strip(),
                    )
                else:
                    dest = workspace_manager.resolve_user_file_path(
                        ctx.workspace_key,
                        default_dest_name(archive.name),
                    )
                # Extraction places files like a write does: the reserved
                # env dirs (excluded from the doc quota) must stay unreachable.
                ensure_not_env_dir(workspace_manager, ctx.workspace_key, dest)
                if dest.exists():
                    rel = workspace_manager.relative_user_file_path(ctx.workspace_key, dest)
                    return tool_error(f"{rel} already exists (choose another dest or delete it)")
                files_root = workspace_manager.user_files_dir(ctx.workspace_key).resolve()
                existing_entries = count_entries_up_to(files_root, config.max_workspace_entries)
                if existing_entries >= config.max_workspace_entries:
                    return tool_error(
                        f"workspace holds too many files (limit "
                        f"{config.max_workspace_entries} entries); delete files or "
                        "directories you no longer need"
                    )
                used = workspace_manager.user_files_size(ctx.workspace_key)
                remaining = config.max_user_bytes - used
                if remaining <= 0:
                    return tool_error(
                        "extraction " + format_quota_error(used, config.max_user_bytes)
                    )
                limits = ExtractLimits(
                    max_entries=config.max_zip_entries,
                    max_file_bytes=config.max_file_bytes,
                    max_total_bytes=min(config.max_extract_total_bytes, remaining),
                )
                try:
                    result = await asyncio.to_thread(
                        safe_extract,
                        archive,
                        dest,
                        strip_top_level=strip_top_level,
                        limits=limits,
                    )
                except ArchiveError as e:
                    shutil.rmtree(dest, ignore_errors=True)
                    return tool_error(
                        scrub_user_paths(str(e), workspace_manager, ctx.workspace_key)
                    )
                except Exception as e:
                    shutil.rmtree(dest, ignore_errors=True)
                    return tool_error(
                        "extraction failed: "
                        + scrub_user_paths(str(e), workspace_manager, ctx.workspace_key)
                    )
                payload: dict[str, object] = {
                    "dest": workspace_manager.relative_user_file_path(ctx.workspace_key, dest),
                    "entries": result.entries,
                    "total_bytes": result.total_bytes,
                }
                if result.stripped_top_level:
                    payload["stripped_top_level"] = result.stripped_top_level
                return json.dumps(payload)
        except Exception as e:
            return tool_error(scrub_user_paths(str(e), workspace_manager, ctx.workspace_key))

    registry.register(
        name="zip",
        description=(
            "Create a .zip archive from workspace files and/or directories (directories "
            "recurse) and queue it for attachment when limits allow."
        ),
        parameters={
            "type": "object",
            "properties": {
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Workspace-relative files or directories to include; "
                        'pass ["."] to archive the whole workspace.'
                    ),
                },
                "output": {
                    "type": "string",
                    "description": "Workspace-relative archive name; must end in .zip.",
                },
            },
            "required": ["paths", "output"],
        },
        handler=_zip,
        min_tier=TrustTier.MEMBER,
    )
    registry.register(
        name="extract_archive",
        description=(
            "Safely unpack a .tar.gz/.tgz/.zip archive already in your workspace "
            "into a directory. Use after fetch_url downloads a repo tarball."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": ("Workspace-relative path to the .tar.gz/.tgz/.zip archive."),
                },
                "dest": {
                    "type": "string",
                    "description": (
                        "Optional output directory (workspace-relative). "
                        "Defaults to the archive name without its extension."
                    ),
                },
                "strip_top_level": {
                    "type": "boolean",
                    "description": (
                        "Collapse a single common top-level directory "
                        "(e.g. GitHub's repo-<sha>/). Default true."
                    ),
                },
            },
            "required": ["path"],
        },
        handler=_extract_archive,
        min_tier=TrustTier.MEMBER,
        searchable=True,
        category="Workspace",
    )


def _build_zip(
    workspace_manager: WorkspaceManager,
    workspace_key: WorkspaceKey,
    paths_arg: list[str],
    output_path: Path,
    root: Path,
    config: WorkspaceToolConfig,
) -> str | tuple[int, int]:
    """Scan inputs and write the archive; blocking, runs in a worker thread.

    Returns a tool_error payload string on failure, else (entry_count, size_bytes).
    """
    entries: list[tuple[str, Path]] = []
    seen: dict[str, str] = {}
    scanned = 0
    for p in paths_arg:
        # allow_root so `paths: ["."]` means "zip everything" instead of the
        # contradictory "path must be relative" error.
        resolved = workspace_manager.resolve_user_file_path(workspace_key, p, allow_root=True)
        if not resolved.exists():
            return tool_error(f"path not found: {p}")
        if resolved.is_file():
            members: Iterable[Path] = [resolved]
        elif resolved.is_dir():
            members = resolved.rglob("*")
        else:
            return tool_error(f"path not found: {p}")
        for f in members:
            # Env dirs are hidden from listings/glob/grep; including them here
            # would trip the entry cap on files the model cannot even see.
            if in_env_dir(root, f):
                continue
            # In-flight .part temps are transient plumbing, never archive input.
            if f.name.endswith(".part"):
                continue
            scanned += 1
            if scanned > config.max_zip_entries:
                return tool_error(f"too many files to zip; the limit is {config.max_zip_entries}")
            if f.is_symlink():
                arcname = f.relative_to(root).as_posix()
                return tool_error(f"{arcname} is a symlink and cannot be archived")
            if not f.is_file():
                continue
            arcname = f.relative_to(root).as_posix()
            if arcname in seen:
                return tool_error(f"duplicate archive entry {arcname} from {seen[arcname]} and {p}")
            seen[arcname] = p
            entries.append((arcname, f))
    if not entries:
        return tool_error("no files to archive (inputs contained no files)")
    entries.sort()
    temp_path = root / f".zip-{uuid.uuid4().hex}.part"
    try:
        with zipfile.ZipFile(
            temp_path,
            "w",
            compression=zipfile.ZIP_DEFLATED,
        ) as zf:
            for arcname, f in entries:
                zf.write(f, arcname=arcname)
        size = temp_path.stat().st_size
        if size > config.max_file_bytes:
            return tool_error(
                f"zip would be {size} bytes, over the {config.max_file_bytes} byte file limit"
            )
        if not quota_ok(
            workspace_manager,
            workspace_key,
            new_size=size,
            destination=output_path,
            temp_path=temp_path,
            max_user_bytes=config.max_user_bytes,
            max_entries=config.max_workspace_entries,
        ):
            used = workspace_manager.user_files_size(workspace_key)
            return tool_error("zip " + format_quota_error(used, config.max_user_bytes))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path.replace(output_path)
    finally:
        temp_path.unlink(missing_ok=True)
    return len(entries), size
