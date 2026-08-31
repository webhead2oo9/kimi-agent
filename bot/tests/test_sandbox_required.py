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

import pytest

from sandbox.runner import SandboxConfig, sandbox_available
from skills.sandbox import validate_sandbox_runtime

REQUIRE_ENV = "KIMI_REQUIRE_SANDBOX_TESTS"


def test_live_sandbox_is_available_where_required() -> None:
    if os.environ.get(REQUIRE_ENV) != "1":
        pytest.skip(f"{REQUIRE_ENV}=1 is not set; the live sandbox is optional on this host")

    assert sandbox_available(SandboxConfig()), (
        f"{REQUIRE_ENV}=1 but the code-execution sandbox cannot start here; "
        "run `python -m scripts.sandbox_probe` to see which prerequisite failed"
    )
    # Raises SandboxUnavailableError naming the failing step, which is the
    # report we want in the failure output.
    validate_sandbox_runtime()
