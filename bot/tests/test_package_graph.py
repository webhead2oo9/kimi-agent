"""The package dependency graph, frozen.

`test_architecture_boundaries.py` asserts a handful of specific rules about
specific files. This is the whole graph: every package-to-package import edge
that executes must appear in `_ALLOWED_EDGES` below. A new edge fails here,
which is the point: the layering used to live only in prose, and prose drifts.

Adding an edge is allowed. Doing it deliberately, in a diff a reviewer sees, is
what this asks for.

`if TYPE_CHECKING:` imports are ignored throughout: a type-only import costs
nothing at runtime and does not constrain boot order.
"""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

_SKIP_PREFIXES = (".venv/", "tests/", "workspaces/", "data/", "skills/store/")

# Package -> the packages it may import. Leaves map to an empty set.
#
# Three cycles survive and are listed on both sides deliberately. Each is a
# known seam rather than an oversight:
#
#   agent <-> tools            the ReAct core dispatches through the registry,
#                              while the registry needs the core's activity
#                              labels and the backfill record type.
#   tools <-> discord_adapter  two tools (channel_context, member) read live
#                              Discord through the gateway, which the adapter
#                              also drives.
#   tools <-> config           tools declare typed config specs; the fragment
#                              reader in config/ resolves them. tools/config_spec
#                              is a stdlib-only leaf, held there by
#                              test_import_isolation.py.
_ALLOWED_EDGES: dict[str, set[str]] = {
    "agent": {
        "config",
        "memory",
        "moderation",
        "observability",
        "providers",
        "storage",
        "tools",
        "trust",
        "usage",
        "utils",
        "workspace",
    },
    # The composition root. It is allowed to reach everything; that is its job.
    "app": {
        "agent",
        "codex",
        "commands",
        "config",
        "discord_adapter",
        "memory",
        "moderation",
        "observability",
        "providers",
        "sandbox",
        "search",
        "skills",
        "storage",
        "tools",
        "trust",
        "usage",
        "utils",
        "workspace",
        "web_browser",
    },
    "bot": {"app", "config"},
    "codex": {"utils"},
    "commands": {
        "discord_adapter",
        "memory",
        "storage",
        "tools",
        "trust",
        "utils",
        "workspace",
    },
    "config": {"providers", "tools", "trust", "utils"},
    "discord_adapter": {"agent", "memory", "storage", "tools", "trust", "workspace"},
    # The offline harness drives the production core, so it sees what app sees.
    "evals": {
        "agent",
        "app",
        "config",
        "discord_adapter",
        "memory",
        "providers",
        "storage",
        "tools",
        "trust",
        "usage",
        "utils",
    },
    "memory": {"providers", "storage", "utils"},
    "moderation": {"observability", "providers", "trust", "utils"},
    "observability": {"utils"},
    "providers": {"codex", "utils"},
    "scripts": {"codex"},
    # Sandbox quota enforcement uses workspace's fd-relative ownership boundary.
    "sandbox": {"workspace"},
    "search": set(),
    "skills": {"config", "tools", "trust", "utils", "workspace"},
    "storage": {"providers", "usage"},
    "tools": {
        "agent",
        "config",
        "discord_adapter",
        "memory",
        "providers",
        "sandbox",
        "search",
        "skills",
        "storage",
        "trust",
        "usage",
        "utils",
        "workspace",
        "web_browser",
    },
    "trust": set(),
    "usage": {"config"},
    "utils": set(),
    "web_browser": {"sandbox"},
    "workspace": set(),
}


def _local_packages() -> set[str]:
    packages = {
        path.name
        for path in PROJECT_ROOT.iterdir()
        if path.is_dir() and (path / "__init__.py").exists() and path.name != "tests"
    }
    # bot.py is a module, not a package, but it is a graph node all the same.
    return packages | {"bot"}


def _runtime_imports(tree: ast.Module) -> set[str]:
    type_checking: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            test = node.test
            if (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
                isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
            ):
                type_checking.update(id(child) for child in ast.walk(node))

    modules: set[str] = set()
    for node in ast.walk(tree):
        if id(node) in type_checking:
            continue
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None and node.level == 0:
            modules.add(node.module)
    return modules


def _observed_edges() -> dict[str, set[str]]:
    packages = _local_packages()
    edges: dict[str, set[str]] = {}
    for path in PROJECT_ROOT.rglob("*.py"):
        relative = path.relative_to(PROJECT_ROOT).as_posix()
        if relative.startswith(_SKIP_PREFIXES):
            continue
        source = relative.split("/")[0] if "/" in relative else relative.removesuffix(".py")
        if source not in packages:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        for module in _runtime_imports(tree):
            target = module.split(".")[0]
            if target in packages and target != source:
                edges.setdefault(source, set()).add(target)
    return edges


def test_no_undeclared_package_dependencies() -> None:
    undeclared: list[str] = []
    for source, targets in sorted(_observed_edges().items()):
        allowed = _ALLOWED_EDGES.get(source)
        if allowed is None:
            undeclared.append(f"{source} (package missing from _ALLOWED_EDGES)")
            continue
        for target in sorted(targets - allowed):
            undeclared.append(f"{source} -> {target}")

    assert not undeclared, (
        "New package dependencies. Add each to _ALLOWED_EDGES if it is "
        f"intended, or route around it: {undeclared}"
    )


def test_declared_edges_still_exist() -> None:
    """Keep the table honest: a listed edge that is gone should be deleted.

    Without this the table slowly becomes a record of what the graph used to
    be, and stops constraining anything.
    """

    observed = _observed_edges()
    stale = [
        f"{source} -> {target}"
        for source, targets in sorted(_ALLOWED_EDGES.items())
        for target in sorted(targets - observed.get(source, set()))
    ]

    assert not stale, f"_ALLOWED_EDGES lists dependencies that no longer exist: {stale}"


def test_previously_removed_edges_stay_removed() -> None:
    """Named regressions, so a reintroduction says what broke rather than "new edge"."""

    observed = _observed_edges()
    assert "app" not in observed.get("commands", set()), (
        "commands must not import app: app/runtime.py imports every command "
        "module at import time, so this is a runtime cycle held together only "
        "by import order."
    )
    assert "agent" not in observed.get("memory", set()), (
        "memory must not import agent: agent/turn.py imports memory.mutations, "
        "so this closes a loop. Declare a Protocol for what you need instead."
    )
    assert "agent" not in observed.get("workspace", set()), (
        "workspace is a stdlib-only sandbox library and must stay a leaf."
    )
    assert "agent" not in observed.get("config", set()), (
        "config must not import agent: the fragment readers were moved out of "
        "agent/ precisely so operator config stops depending on the ReAct core."
    )
