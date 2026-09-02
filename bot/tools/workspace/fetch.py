from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
import uuid
from pathlib import Path

from workspace import WorkspaceManager
from tools.downloads import (
    filename_from_url,
    fetch_url_to_file,
    safe_filename,
    validate_fetch_url,
)
from tools.registry import MessageContext, ToolRegistry
from trust.tiers import TrustTier

from .common import (
    ATTACHMENT_HINT,
    UserLocks,
    available_destination,
    ensure_quota,
    scrub_user_paths,
    tool_error,
    workspace_barrier,
    workspace_user_lock,
)
from .config import WorkspaceToolConfig


def register_fetch_tools(
    registry: ToolRegistry,
    workspace_manager: WorkspaceManager,
    config: WorkspaceToolConfig,
    locks: UserLocks,
) -> None:
    async def _fetch_url(args: dict, ctx: MessageContext) -> str:
        url = str(args.get("url", "")).strip()
        filename_arg = args.get("filename")
        if not url:
            return tool_error("url is required")
        temp_path: Path | None = None
        try:
            validate_fetch_url(url)
            filename_text = filename_arg.strip() if isinstance(filename_arg, str) else ""
            requested_filename = safe_filename(filename_text) if filename_text else None
            url_filename = filename_from_url(url)
            # The download holds only THIS user's lock (their fetches stay
            # serialized) and never the maintenance barrier, so a slow origin cannot
            # periodically freeze every user's workspace tools. The temp lives
            # outside the workspace tree, where the sweeper and quota walks
            # never see it; only the quick finalize needs the sweep exclusion.
            temp_path = Path(tempfile.gettempdir()) / f"fetch-{uuid.uuid4().hex}.part"
            async with workspace_user_lock(locks, ctx):
                fetch_result = await fetch_url_to_file(
                    url,
                    temp_path,
                    max_bytes=config.max_file_bytes,
                    timeout_seconds=config.fetch_timeout_seconds,
                    max_redirects=config.max_redirects,
                )
                final_filename = requested_filename or fetch_result.filename or url_filename
                async with workspace_barrier(locks, ctx):
                    if requested_filename:
                        destination = workspace_manager.resolve_user_file_path(
                            ctx.workspace_key,
                            final_filename,
                        )
                        # Match the sibling tools (import_attachment, move_file,
                        # extract_archive): an explicit destination never
                        # silently clobbers an existing file.
                        if destination.exists():
                            return tool_error(
                                f"{final_filename} already exists "
                                "(choose another filename or delete it)"
                            )
                    else:
                        destination = available_destination(
                            workspace_manager,
                            ctx.workspace_key,
                            final_filename,
                        )
                    ensure_quota(
                        workspace_manager,
                        ctx.workspace_key,
                        new_size=fetch_result.size_bytes,
                        destination=destination,
                        temp_path=None,
                        max_user_bytes=config.max_user_bytes,
                        max_entries=config.max_workspace_entries,
                    )
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    # shutil.move, not Path.replace: the temp lives in the system
                    # tempdir, which may be another filesystem; threaded because
                    # a cross-device move copies the bytes.
                    await asyncio.to_thread(shutil.move, str(temp_path), str(destination))
                    return json.dumps(
                        {
                            "path": workspace_manager.relative_user_file_path(
                                ctx.workspace_key,
                                destination,
                            ),
                            "filename": destination.name,
                            "size_bytes": fetch_result.size_bytes,
                            "content_type": fetch_result.content_type,
                            "attached": False,
                            "attachment_hint": ATTACHMENT_HINT,
                        }
                    )
        except Exception as e:
            return tool_error(scrub_user_paths(str(e), workspace_manager, ctx.workspace_key))
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    registry.register(
        name="fetch_url",
        description=(
            "Download an https URL into your workspace. The saved file is not attached; "
            "call queue_file with the returned path to include it with the final reply."
        ),
        parameters={
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Public https URL to fetch.",
                },
                "filename": {
                    "type": "string",
                    "description": (
                        "Optional workspace filename to save as; fails if it "
                        "already exists. Omit to auto-name from the URL."
                    ),
                },
            },
            "required": ["url"],
        },
        handler=_fetch_url,
        min_tier=TrustTier.MEMBER,
        untrusted=True,
    )
