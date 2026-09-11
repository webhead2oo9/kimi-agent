"""The package dependency graph, frozen.

`test_architecture_boundaries.py` asserts a handful of specific rules about
specific files. This is the whole graph: every package-to-package import edge
that executes must appear in `_ALLOWED_EDGES` below. A new edge fails here,
making every layering change an explicit review decision.

Adding an edge is allowed. Doing it deliberately, in a diff a reviewer sees, is
what this asks for.

`if TYPE_CHECKING:` imports are ignored throughout: a type-only import costs
nothing at runtime and does not constrain boot order.
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests.source_inspection import python_sources, runtime_imports, source_module

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Package -> the packages it may import. Leaves map to an empty set.
#
# Four direct bidirectional seams survive and are listed on both sides
# deliberately. The first three merge transitively with memory, skills, storage,
# and usage into one eight-node SCC; app/modules is the second nontrivial SCC.
# Each seam is known rather than an oversight:
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
#   app <-> modules            app composes the module runtime services, while
#                              modules/testing drives ModuleManager (which
#                              lives in app/) the way the bot does. Moving the
#                              manager into modules/ would break the cycle.
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
        "image_gen",
        "bram_agent_module_api",
        "memory",
        "moderation",
        "modules",
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
        "video_understanding",
        "workspace",
        "web_browser",
        "xai",
    },
    "bot": {"app", "config"},
    "branding": set(),
    "codex": {"utils"},
    "commands": {
        "branding",
        "discord_adapter",
        "bram_agent_module_api",
        "memory",
        "storage",
        "tools",
        "trust",
        "utils",
        "workspace",
    },
    "community_agent_reference_module": {"bram_agent_module_api"},
    "config": {"branding", "bram_agent_module_api", "providers", "tools", "trust", "utils"},
    "deploy": {"config", "sandbox", "tools", "web_browser"},
    "discord_adapter": {
        "agent",
        "bram_agent_module_api",
        "memory",
        "storage",
        "tools",
        "trust",
        "workspace",
    },
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
    "image_gen": {"codex", "utils"},
    "hello_module": {"bram_agent_module_api"},
    # Core may depend on the shared SDK vocabulary, but the standalone SDK may not
    # depend on core. Its broader third-party allowlist is pinned by
    # test_module_api_contracts.py::test_entire_sdk_has_no_core_runtime_imports.
    "bram_agent_module_api": set(),
    "memory": {"providers", "storage", "utils"},
    "moderation": {"observability", "providers", "trust", "utils"},
    # Module API runtime services. Grows as each service lands; the app edge is
    # the harness cycle documented above.
    "modules": {"app", "config", "bram_agent_module_api", "storage", "tools", "utils", "workspace"},
    "observability": {"utils"},
    "providers": {"branding", "codex", "utils", "xai"},
    "scripts": {
        "app",
        "branding",
        "codex",
        "config",
        "bram_agent_module_api",
        "sandbox",
        "skills",
        "xai",
    },
    # Sandbox quota enforcement uses workspace's fd-relative ownership boundary.
    "sandbox": {"workspace"},
    "search": {"utils"},
    "skills": {"branding", "config", "tools", "trust", "utils", "workspace"},
    "storage": {"providers", "usage"},
    "tools": {
        "agent",
        "config",
        "discord_adapter",
        "image_gen",
        "memory",
        "providers",
        "sandbox",
        "search",
        "skills",
        "storage",
        "trust",
        "usage",
        "utils",
        "video_understanding",
        "workspace",
        "web_browser",
        "xai",
    },
    "trust": {"bram_agent_module_api"},
    "usage": {"config"},
    "utils": {"bram_agent_module_api"},
    "video_understanding": {"utils"},
    "web_browser": {"sandbox", "utils"},
    "workspace": set(),
    "xai": {"branding", "utils"},
}

_EXPECTED_NONTRIVIAL_SCCS: frozenset[frozenset[str]] = frozenset(
    {
        frozenset(
            {
                "agent",
                "config",
                "discord_adapter",
                "memory",
                "skills",
                "storage",
                "tools",
                "usage",
            }
        ),
        frozenset({"app", "modules"}),
    }
)


def _observed_graph() -> tuple[set[str], dict[str, set[str]]]:
    sources = [(path, source_module(path, PROJECT_ROOT)) for path in python_sources(PROJECT_ROOT)]
    nodes = {module.split(".")[0] for _, module in sources}
    edges: dict[str, set[str]] = {}
    for path, module in sources:
        relative = path.relative_to(PROJECT_ROOT).as_posix()
        source = module.split(".")[0]
        package = module if path.stem == "__init__" else module.rpartition(".")[0]
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        for imported in runtime_imports(tree, package=package):
            target = imported.split(".")[0]
            if target in nodes and target != source:
                edges.setdefault(source, set()).add(target)
    return nodes, edges


def _observed_edges() -> dict[str, set[str]]:
    return _observed_graph()[1]


def _strongly_connected_components(graph: dict[str, set[str]]) -> list[frozenset[str]]:
    next_index = 0
    indexes: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    components: list[frozenset[str]] = []

    def visit(node: str) -> None:
        nonlocal next_index
        indexes[node] = next_index
        lowlinks[node] = next_index
        next_index += 1
        stack.append(node)
        on_stack.add(node)

        for target in sorted(graph.get(node, set())):
            if target not in indexes:
                visit(target)
                lowlinks[node] = min(lowlinks[node], lowlinks[target])
            elif target in on_stack:
                lowlinks[node] = min(lowlinks[node], indexes[target])

        if lowlinks[node] != indexes[node]:
            return
        component: set[str] = set()
        while stack:
            member = stack.pop()
            on_stack.remove(member)
            component.add(member)
            if member == node:
                break
        components.append(frozenset(component))

    nodes = set(graph)
    nodes.update(target for targets in graph.values() for target in targets)
    for node in sorted(nodes):
        if node not in indexes:
            visit(node)
    return components


def _display_components(components: frozenset[frozenset[str]]) -> list[list[str]]:
    return sorted((sorted(component) for component in components), key=lambda members: members[0])


def test_no_undeclared_package_dependencies() -> None:
    nodes, edges = _observed_graph()
    undeclared = [
        f"{node} (missing from _ALLOWED_EDGES)" for node in sorted(nodes - _ALLOWED_EDGES.keys())
    ]
    for source, targets in sorted(edges.items()):
        allowed = _ALLOWED_EDGES.get(source)
        if allowed is None:
            continue
        for target in sorted(targets - allowed):
            undeclared.append(f"{source} -> {target}")

    assert not undeclared, (
        "New package dependencies. Add each to _ALLOWED_EDGES if it is "
        f"intended, or route around it: {undeclared}"
    )


def test_standalone_sdk_is_scanned() -> None:
    observed_nodes, _ = _observed_graph()
    assert "bram_agent_module_api" in observed_nodes, (
        "Standalone SDK source root disappeared from package-graph discovery"
    )


def test_declared_edges_still_exist() -> None:
    """Reject stale allowlist entries that weaken the graph constraint."""

    nodes, observed = _observed_graph()
    assert not (missing := _ALLOWED_EDGES.keys() - nodes), (
        f"Declared modules disappeared from source discovery: {sorted(missing)}"
    )
    stale = [
        f"{source} -> {target}"
        for source, targets in sorted(_ALLOWED_EDGES.items())
        for target in sorted(targets - observed.get(source, set()))
    ]

    assert not stale, f"_ALLOWED_EDGES lists unobserved dependencies: {stale}"


def test_allowed_dependency_sccs_do_not_grow() -> None:
    actual = frozenset(
        component
        for component in _strongly_connected_components(_ALLOWED_EDGES)
        if len(component) > 1
    )
    unexpected = actual - _EXPECTED_NONTRIVIAL_SCCS
    missing = _EXPECTED_NONTRIVIAL_SCCS - actual
    assert actual == _EXPECTED_NONTRIVIAL_SCCS, (
        "Allowed dependency cycles changed; a node joined, left, or created a nontrivial SCC. "
        f"Unexpected components: {_display_components(unexpected)}; "
        f"missing components: {_display_components(missing)}"
    )


def test_forbidden_dependency_edges_are_absent() -> None:
    """High-risk dependency boundaries fail with named, specific diagnostics."""

    observed = _observed_edges()
    sdk_core_dependencies = observed.get("bram_agent_module_api", set())
    assert not sdk_core_dependencies, (
        "bram_agent_module_api -> core is forbidden: the standalone SDK must remain "
        f"host-independent; found {sorted(sdk_core_dependencies)}"
    )
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
        "config must not import agent: operator configuration must remain "
        "independent of the ReAct core."
    )


def test_graph_discovers_namespace_packages_and_standalone_modules(tmp_path, monkeypatch) -> None:
    import sys

    import pytest

    (tmp_path / "new_namespace").mkdir()
    (tmp_path / "new_namespace/client.py").write_text("import standalone\n")
    (tmp_path / "standalone.py").write_text("")
    monkeypatch.setattr(sys.modules[__name__], "PROJECT_ROOT", tmp_path)
    nodes, edges = _observed_graph()
    assert nodes == {"new_namespace", "standalone"}
    assert edges == {"new_namespace": {"standalone"}}
    with pytest.raises(AssertionError, match="standalone .*missing from _ALLOWED_EDGES"):
        test_no_undeclared_package_dependencies()
