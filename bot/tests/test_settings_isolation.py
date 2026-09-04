"""Guards against test settings silently consulting an operator profile."""

from __future__ import annotations

import ast
import os
from pathlib import Path
import subprocess
import sys

from tests.helpers import PROJECT_ROOT


_ALLOWED_LIVE_ENV_SETTINGS_CALLS = {
    (
        "test_sandbox_required.py",
        "test_live_sandbox_is_available_where_required",
    ): "the required-sandbox check validates the live operator profile",
    (
        "test_skill_sandbox.py",
        "_live_linux_sandbox_unavailable",
    ): "the collection-time sandbox probe validates the live operator limits",
    (
        "test_settings.py",
        "test_retired_v1_dotenv_settings_fail_startup",
    ): "the retired-setting guard intentionally validates an isolated temporary dotenv",
}


def test_importing_settings_does_not_construct_settings() -> None:
    script = """
import config.settings as settings_module
from pydantic import ValidationError

try:
    settings_module.Settings(_env_file=None)
except ValidationError as exc:
    assert any(error["loc"] == ("script_default_timeout",) for error in exc.errors())
else:
    raise AssertionError("poisoned settings unexpectedly validated")

print(hasattr(settings_module, "settings"))
"""
    env = os.environ.copy()
    env["SCRIPT_DEFAULT_TIMEOUT"] = "0"

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False"


class _SettingsCallVisitor(ast.NodeVisitor):
    def __init__(self, path: Path) -> None:
        self.path = path
        self.function_name = "<module>"
        self.allowed_calls: set[tuple[str, str]] = set()
        self.unisolated_calls: list[tuple[str, str, int]] = []

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        previous_function = self.function_name
        self.function_name = node.name
        self.generic_visit(node)
        self.function_name = previous_function

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name) and node.func.id == "Settings":
            disables_dotenv = any(
                keyword.arg == "_env_file"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value is None
                for keyword in node.keywords
            )
            if not disables_dotenv:
                call_site = (self.path.name, self.function_name)
                if call_site in _ALLOWED_LIVE_ENV_SETTINGS_CALLS:
                    self.allowed_calls.add(call_site)
                else:
                    self.unisolated_calls.append((*call_site, node.lineno))
        self.generic_visit(node)


def test_operator_scripts_construct_settings_after_import() -> None:
    for script_name in ("preflight", "codex-login"):
        source = (PROJECT_ROOT / "scripts" / script_name).read_text(encoding="utf-8")
        assert "from config.settings import settings" not in source
        assert "from config.settings import Settings" in source
        assert "Settings()" in source


def test_settings_constructions_disable_dotenv_loading() -> None:
    allowed_calls: set[tuple[str, str]] = set()
    unisolated_calls: list[tuple[str, str, int]] = []

    for path in sorted((PROJECT_ROOT / "tests").glob("*.py")):
        visitor = _SettingsCallVisitor(path)
        visitor.visit(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        allowed_calls.update(visitor.allowed_calls)
        unisolated_calls.extend(visitor.unisolated_calls)

    stale_allowlist = set(_ALLOWED_LIVE_ENV_SETTINGS_CALLS) - allowed_calls
    assert not stale_allowlist, f"stale live-settings allowlist entries: {sorted(stale_allowlist)}"
    assert not unisolated_calls, (
        "Settings(...) must use make_settings() or pass _env_file=None; "
        f"unisolated calls: {unisolated_calls}"
    )
