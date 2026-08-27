"""Per-(user, guild) sandboxed file workspaces.

The sandbox itself: owner keys, path resolution that refuses traversal and
symlink escapes, quota and TTL sweeping. Stdlib-only and provider-neutral.

Deliberately not under `agent/`: exactly one `agent/` module uses it while
`tools/`, `skills/`, `commands/`, `app/`, and the lifecycle sweepers all do, so
living there would put `agent` on both sides of most import edges. The
Discord-facing tools built on top of it are a separate package,
`tools/workspace/`.
"""

from __future__ import annotations

from workspace.manager import (
    ENV_DIR_NAMES,
    GENERATED_SEGMENT_RE,
    MAX_PATH_DEPTH,
    PRIVATE_DIRECTORY_MODE,
    PRIVATE_FILE_MODE,
    WINDOWS_RESERVED_NAMES,
    WorkspaceKey,
    WorkspaceManager,
    WorkspacePathSymlinkError,
    path_contains_symlink,
    safe_generated_segment,
    workspace_owner_key,
)

__all__ = [
    "ENV_DIR_NAMES",
    "GENERATED_SEGMENT_RE",
    "MAX_PATH_DEPTH",
    "PRIVATE_DIRECTORY_MODE",
    "PRIVATE_FILE_MODE",
    "WINDOWS_RESERVED_NAMES",
    "WorkspaceKey",
    "WorkspaceManager",
    "WorkspacePathSymlinkError",
    "path_contains_symlink",
    "safe_generated_segment",
    "workspace_owner_key",
]
