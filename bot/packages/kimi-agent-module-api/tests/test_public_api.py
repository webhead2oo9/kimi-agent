"""The published SDK stays importable without the Kimi application runtime."""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

import kimi_agent_module_api as api

SDK_ROOT = Path(__file__).resolve().parents[1] / "src" / "kimi_agent_module_api"


def test_public_exports_resolve() -> None:
    assert api.__all__
    assert all(getattr(api, name, None) is not None for name in api.__all__)


def test_standalone_environment_has_no_core_runtime() -> None:
    """CI runs this package from an isolated environment, outside core's import root."""
    assert importlib.util.find_spec("app") is None
    assert importlib.util.find_spec("discord") is None


def test_sdk_imports_only_declared_dependencies() -> None:
    allowed = set(sys.stdlib_module_names) | {
        "aiosqlite",
        "kimi_agent_module_api",
        "pydantic_settings",
    }
    unexpected: dict[str, set[str]] = {}
    for path in SDK_ROOT.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        if outside := imported - allowed:
            unexpected[path.name] = outside
    assert unexpected == {}
