"""Preflight and the sandbox probe must check the service's runtime profile."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from scripts.runtime_env import merge_runtime_env


def test_merges_the_named_runtime_env_before_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_env = tmp_path / "runtime.env"
    runtime_env.write_text('CODE_EXEC_NETWORK_MODE=none\nBOT_NAME="Probe ${PROBE_VALUE}"\n')
    monkeypatch.setenv("RUNTIME_ENV", str(runtime_env))
    monkeypatch.setenv("PROBE_VALUE", "expanded")
    monkeypatch.delenv("CODE_EXEC_NETWORK_MODE", raising=False)
    monkeypatch.delenv("BOT_NAME", raising=False)

    note = merge_runtime_env()

    assert note == f"merged RUNTIME_ENV={runtime_env}"
    assert os.environ["CODE_EXEC_NETWORK_MODE"] == "none"
    # EnvironmentFile semantics: values are literal, no interpolation.
    assert os.environ["BOT_NAME"] == "Probe ${PROBE_VALUE}"


def test_malformed_assignment_leaves_environment_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_env = tmp_path / "runtime.env"
    runtime_env.write_text("PROBE_VALUE=changed\nJUST_A_NAME\n")
    monkeypatch.setenv("RUNTIME_ENV", str(runtime_env))
    monkeypatch.setenv("PROBE_VALUE", "original")

    with pytest.raises(SystemExit, match="invalid assignment"):
        merge_runtime_env()

    assert os.environ["PROBE_VALUE"] == "original"


def test_missing_file_is_reported_not_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    absent = tmp_path / "nope.env"
    monkeypatch.setenv("RUNTIME_ENV", str(absent))

    assert merge_runtime_env() == f"no runtime.env overlay ({absent} absent)"


def test_unset_variable_defaults_to_the_config_home_like_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("RUNTIME_ENV", raising=False)
    monkeypatch.setenv("KIMI_CONFIG_HOME", str(tmp_path))
    runtime_env = tmp_path / "runtime.env"

    assert merge_runtime_env() == f"no runtime.env overlay ({runtime_env} absent)"

    monkeypatch.delenv("PROBE_DEFAULTED_VALUE", raising=False)
    runtime_env.write_text("PROBE_DEFAULTED_VALUE=yes\n")
    assert merge_runtime_env() == f"merged RUNTIME_ENV={runtime_env}"

    assert os.environ["PROBE_DEFAULTED_VALUE"] == "yes"
