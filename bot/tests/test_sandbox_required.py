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

from app.tools import build_sandbox_config
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
    assert sandbox_available(build_sandbox_config(settings)), (
        f"{REQUIRE_ENV}=1 but the configured code-execution profile cannot start here; "
        "run `python -m scripts.sandbox_probe` to see which prerequisite failed"
    )
    # Raises SandboxUnavailableError naming the failing step, which is the
    # report we want in the failure output.
    validate_sandbox_runtime(build_script_sandbox_limits(settings))


def test_the_skip_conversion_lists_match_the_sources() -> None:
    """The conftest hook fails open if a module is renamed or a gate reason is
    reworded, so pin both lists against the actual files."""

    tests_dir = Path(__file__).resolve().parent
    sources: list[str] = []
    for stem in tests_conftest._SANDBOX_MODULES:
        path = tests_dir / f"{stem}.py"
        assert path.is_file(), f"conftest._SANDBOX_MODULES names a missing file: {stem}"
        sources.append(path.read_text(encoding="utf-8"))
    joined = "\n".join(sources)
    for reason in tests_conftest._SANDBOX_SKIP_REASONS:
        assert reason in joined, f"conftest._SANDBOX_SKIP_REASONS entry not found: {reason!r}"
