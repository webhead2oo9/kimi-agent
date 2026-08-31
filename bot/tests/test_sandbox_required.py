"""Fail early and by name when the live sandbox is required but cannot start.

The live-jail tests in test_sandbox_runner.py, test_code_exec_tool.py, and
test_skill_sandbox.py skip when the Linux boundary cannot start, which is
right on a developer laptop and self-concealing on the one CI job whose
purpose is to prove the boundary. That job sets KIMI_REQUIRE_SANDBOX_TESTS=1.
The enforcement lives in conftest.py, which turns any skip in those modules
into a failure under that flag; this test is the fast, readable first failure
that names the prerequisite instead of leaving 23 converted skips to explain.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import ast

from app.tools import build_sandbox_config
from config.operator_settings import apply_operator_settings
from config.settings import Settings
from sandbox.runner import sandbox_available
from skills.registration import build_script_sandbox_limits
from skills.sandbox import validate_sandbox_runtime
from tests import conftest as tests_conftest

REQUIRE_ENV = "KIMI_REQUIRE_SANDBOX_TESTS"


def test_live_sandbox_is_available_where_required() -> None:
    if os.environ.get(REQUIRE_ENV) != "1":
        pytest.skip(f"{REQUIRE_ENV}=1 is not set; the live sandbox is optional on this host")

    settings = Settings()  # type: ignore[call-arg]
    # The same layered profile startup certifies: the operator settings.md
    # overlay is applied from the configured instance directory.
    apply_operator_settings(settings, config_dir=Path(settings.config_dir).resolve())
    assert sandbox_available(build_sandbox_config(settings)), (
        f"{REQUIRE_ENV}=1 but the configured code-execution profile cannot start here; "
        "run `python -m scripts.sandbox_probe` to see which prerequisite failed"
    )
    # Raises SandboxUnavailableError naming the failing step, which is the
    # report we want in the failure output.
    validate_sandbox_runtime(build_script_sandbox_limits(settings))


def _skip_reason_literals(tree: ast.AST) -> set[str]:
    """Every literal a skip in this module can carry: skipif/skip reasons."""

    reasons: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name == "skipif":
            for keyword in node.keywords:
                if (
                    keyword.arg == "reason"
                    and isinstance(keyword.value, ast.Constant)
                    and isinstance(keyword.value.value, str)
                ):
                    reasons.add(keyword.value.value)
        elif (
            name == "skip"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            reasons.add(node.args[0].value)
    return reasons


def test_every_skip_reason_is_classified_and_every_gate_reason_is_real() -> None:
    """Bidirectional pin for the conftest skip-to-failure hook.

    Forward: every skip literal in the sandbox modules must be classified as a
    sandbox gate (converted under the flag) or a host-shape condition (left as
    a skip), so a new unclassified skip cannot silently hollow the CI job out.
    Backward: every gate reason must actually appear as a skip literal, so a
    reworded gate turns red instead of quietly never converting again.
    """

    tests_dir = Path(__file__).resolve().parent
    found: set[str] = set()
    for stem in tests_conftest._SANDBOX_MODULES:
        path = tests_dir / f"{stem}.py"
        assert path.is_file(), f"conftest._SANDBOX_MODULES names a missing file: {stem}"
        found |= _skip_reason_literals(ast.parse(path.read_text(encoding="utf-8")))

    classified = set(tests_conftest._SANDBOX_SKIP_REASONS) | set(
        tests_conftest._HOST_SHAPE_SKIP_REASONS
    )
    unclassified = {
        reason for reason in found if not any(reason.startswith(known) for known in classified)
    }
    assert not unclassified, (
        "skip reasons in the sandbox modules are neither a sandbox gate nor a "
        f"declared host-shape condition: {sorted(unclassified)}"
    )
    for gate in tests_conftest._SANDBOX_SKIP_REASONS:
        assert any(reason.startswith(gate) for reason in found), (
            f"conftest._SANDBOX_SKIP_REASONS entry no longer appears as a skip: {gate!r}"
        )
