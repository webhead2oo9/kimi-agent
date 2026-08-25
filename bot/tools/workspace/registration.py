from __future__ import annotations

from workspace import WorkspaceManager
from tools.registry import ToolRegistry

from .archive_tools import register_archive_tools
from .common import UserLocks
from .config import WorkspaceToolConfig
from .documents import register_document_tools
from .fetch import register_fetch_tools
from .files import register_file_tools
from .images import register_image_tools
from .search import register_search_tools


def init_workspace_tools(
    registry: ToolRegistry,
    workspace_manager: WorkspaceManager,
    *,
    config: WorkspaceToolConfig | None = None,
    locks: UserLocks | None = None,
) -> UserLocks:
    """Register the workspace file tools and return the per-workspace UserLocks.

    The returned locks instance must also be handed to every other surface that
    writes into workspaces: the skill script runner (skills/registration.py),
    embed asset writes, the privacy deletion barrier, and the lifecycle sweeper.
    Sharing one instance is what stops a concurrent writer racing a file tool's
    resolve->write path on the same workspace. That concurrency is what would
    make it exploitable via a symlink swap.
    """
    cfg = config or WorkspaceToolConfig()
    locks = locks or UserLocks()
    register_file_tools(registry, workspace_manager, cfg, locks)
    register_document_tools(registry, workspace_manager, cfg, locks)
    register_archive_tools(registry, workspace_manager, cfg, locks)
    register_fetch_tools(registry, workspace_manager, cfg, locks)
    register_search_tools(registry, workspace_manager, cfg, locks)
    register_image_tools(registry, workspace_manager, cfg, locks)
    return locks
