"""Test-only process harness for skill-runner orchestration tests."""

from __future__ import annotations

import tempfile
from typing import Any
from unittest.mock import patch

import skills.runner as runner


async def run_script_with_direct_test_command(**kwargs: Any) -> runner.ScriptResult:
    """Run orchestration tests without exposing an unsandboxed production switch.

    Linux sandbox behavior is covered separately in ``test_skill_sandbox.py``.
    These tests replace command construction at the module boundary so stream,
    cancellation, redaction, and output-file behavior can run on every platform.
    """

    workspace_dir = kwargs.get("workspace_dir")
    original_build_env = runner._build_env

    def direct_command(**command_kwargs: Any) -> list[str]:
        return [
            str(command_kwargs["interpreter"]),
            str(command_kwargs["resolved_script"]),
        ]

    with tempfile.TemporaryDirectory(prefix="skill-runner-test-home-") as scratch_home_path:

        def host_env(
            secrets: dict[str, str],
            _sandbox_workspace: str | None = None,
            scratch_home: str | None = None,
        ) -> dict[str, str]:
            del _sandbox_workspace, scratch_home
            return original_build_env(
                secrets,
                workspace_dir,
                scratch_home=scratch_home_path,
            )

        with (
            patch.object(runner, "detect_sandbox_runtime", return_value=object()),
            patch.object(runner, "build_sandbox_command", side_effect=direct_command),
            patch.object(runner, "_build_env", side_effect=host_env),
        ):
            return await runner.run_script(**kwargs)
