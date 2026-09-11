"""Static source discovery and import inspection shared by architecture checks."""

from __future__ import annotations

import ast
from collections import Counter
from collections.abc import Iterator
from importlib.util import resolve_name
from pathlib import Path

# These directories contain generated files or deployment-owned data, never core
# source. Prune before descending so a workspace cannot affect architecture tests.
_SKIP_DIRECTORIES = {"tests", "__pycache__", "node_modules", "build", "dist", "venv", "env", "ENV"}
_PRIVATE_ROOTS = {
    "data",
    "workspaces",
    "secrets",
    "attachments",
    "transcripts",
    "sandboxes",
    "skills/store",
    "skills/instances",
    "evals/private",
    "evals/cassettes",
    "evals/captions",
    "evals/runs",
    "evals/results",
    "evals/latest",
}
_SOURCE_ROOTS = (
    "packages/bram-agent-module-api/src",
    "modules/example/src",
    "modules/minimal",
)


def python_sources(root: Path) -> Iterator[Path]:
    """Find runtime and eval Python, including namespace packages and new files."""
    for directory, directories, files in root.walk():
        directories[:] = sorted(
            name
            for name in directories
            if not name.startswith(".")
            and name not in _SKIP_DIRECTORIES
            and (directory / name).relative_to(root).as_posix() not in _PRIVATE_ROOTS
            and not (directory == root and name.startswith(("config.", "skills.")))
        )
        for name in sorted(files):
            if name.endswith(".py"):
                path = directory / name
                if path.is_symlink():
                    raise ValueError(f"Python source must not be a symlink: {path}")
                yield path


def source_module(path: Path, root: Path) -> str:
    """Map source layouts to import names, without importing application code."""
    relative = path.relative_to(root)
    for prefix in _SOURCE_ROOTS:
        if relative.is_relative_to(prefix):
            relative = relative.relative_to(prefix)
            break
    parts = list(relative.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _typing_aliases(tree: ast.Module) -> tuple[set[str], set[str]]:
    bindings: Counter[str] = Counter()
    modules: set[str] = set()
    constants: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.asname or alias.name.split(".")[0]
                bindings[name] += 1
                if alias.name == "typing":
                    modules.add(name)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                name = alias.asname or alias.name
                bindings[name] += 1
                if node.level == 0 and node.module == "typing" and alias.name == "TYPE_CHECKING":
                    constants.add(name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bindings[node.id] += 1
        elif (
            isinstance(node, ast.Attribute)
            and isinstance(node.ctx, (ast.Store, ast.Del))
            and node.attr == "TYPE_CHECKING"
            and isinstance(node.value, ast.Name)
        ):
            bindings[node.value.id] += 1
        elif isinstance(node, ast.arg):
            bindings[node.arg] += 1
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) or (
            isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)) and node.name
        ):
            if node.name is not None:
                bindings[node.name] += 1
        elif isinstance(node, ast.MatchMapping) and node.rest:
            bindings[node.rest] += 1
    # Conservatively scan both branches if an alias is rebound or shadowed in
    # any scope. Only an unambiguous typing binding can hide an import.
    return (
        {name for name in modules if bindings[name] == 1},
        {name for name in constants if bindings[name] == 1},
    )


def runtime_imports(tree: ast.Module, *, package: str = "") -> set[str]:
    """Resolve imports and omit branches guarded by unambiguous typing aliases.

    This is a static boundary check, not a Python interpreter: unknown conditions
    and shadowed aliases retain both branches. Relative imports require the
    containing package, so a missing source context cannot silently hide an edge.
    """
    modules: set[str] = set()
    typing_modules, typing_constants = _typing_aliases(tree)

    def guard_value(node: ast.expr) -> bool | None:
        if isinstance(node, ast.Name) and node.id in typing_constants:
            return False
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "TYPE_CHECKING"
            and isinstance(node.value, ast.Name)
            and node.value.id in typing_modules
        ):
            return False
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            value = guard_value(node.operand)
            return None if value is None else not value
        return None

    class ImportVisitor(ast.NodeVisitor):
        def visit_If(self, node: ast.If) -> None:
            value = guard_value(node.test)
            if value is None:
                self.generic_visit(node)
            else:
                for child in node.body if value else node.orelse:
                    self.visit(child)

        def visit_Import(self, node: ast.Import) -> None:
            modules.update(alias.name for alias in node.names)

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            if node.level:
                if not package:
                    raise ValueError("Relative import requires its containing package")
                base = resolve_name("." * node.level + (node.module or ""), package)
                if node.module is None:
                    modules.update(f"{base}.{alias.name}" for alias in node.names)
                else:
                    modules.add(base)
            elif node.module is not None:
                modules.add(node.module)

    ImportVisitor().visit(tree)
    return modules
