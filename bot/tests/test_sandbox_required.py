"""Fail early and by name when the live sandbox is required but cannot start.

The live-jail tests in test_sandbox_runner.py, test_code_exec_tool.py, and
test_skill_sandbox.py skip when the Linux boundary cannot start, which is
right on a developer laptop and self-concealing on the one CI job whose
purpose is to prove the boundary. That job sets KIMI_REQUIRE_SANDBOX_TESTS=1.
Their shared gate suppresses prerequisite skips under that flag, and this test
is the fast, readable first failure that names the missing prerequisite. Other
host-shape skips remain ordinary skips in both environments.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.tools import build_sandbox_config
from config.operator_settings import apply_operator_settings
from config.settings import Settings
from sandbox.runner import sandbox_available
from skills.registration import build_script_sandbox_limits
from skills.sandbox import validate_sandbox_runtime
from tests.sandbox_gate import (
    REQUIRE_SANDBOX_ENV,
    sandbox_skip_allowed,
    sandbox_unavailable,
)


@pytest.mark.uses_live_settings_env
def test_live_sandbox_is_available_where_required() -> None:
    if os.environ.get(REQUIRE_SANDBOX_ENV) != "1":
        pytest.skip("KIMI_REQUIRE_SANDBOX_TESTS=1 is not set; the live sandbox is optional here")

    settings = Settings()  # type: ignore[call-arg]
    # The same layered profile startup certifies: the operator settings.md
    # overlay is applied from the configured instance directory.
    apply_operator_settings(settings, config_dir=Path(settings.config_dir).resolve())
    assert sandbox_available(build_sandbox_config(settings)), (
        f"{REQUIRE_SANDBOX_ENV}=1 but the configured code-execution profile cannot start here; "
        "run `python -m scripts.sandbox_probe` to see which prerequisite failed"
    )
    # Raises SandboxUnavailableError naming the failing step, which is the
    # report we want in the failure output.
    validate_sandbox_runtime(build_script_sandbox_limits(settings))


def test_unavailable_sandbox_skip_depends_on_required_ci_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(REQUIRE_SANDBOX_ENV, raising=False)
    assert sandbox_skip_allowed(True) is True
    assert sandbox_skip_allowed(False) is False

    monkeypatch.setenv(REQUIRE_SANDBOX_ENV, "1")
    assert sandbox_skip_allowed(True) is False


def test_runtime_sandbox_gate_skips_locally_and_fails_in_required_ci(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(REQUIRE_SANDBOX_ENV, raising=False)
    with pytest.raises(pytest.skip.Exception, match="libseccomp unavailable"):
        sandbox_unavailable("libseccomp unavailable")

    monkeypatch.setenv(REQUIRE_SANDBOX_ENV, "1")
    with pytest.raises(pytest.fail.Exception, match="KIMI_REQUIRE_SANDBOX_TESTS=1"):
        sandbox_unavailable("libseccomp unavailable")
