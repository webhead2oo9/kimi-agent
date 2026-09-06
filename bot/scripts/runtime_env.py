"""Load the service's optional runtime environment for operator checks."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values


def merge_runtime_env() -> str:
    """Apply literal runtime.env values before importing application settings.

    The service reads this separate EnvironmentFile; preflight and the sandbox
    probe need the same overlay to check the profile the service will run.
    """

    raw = os.environ.get("RUNTIME_ENV")
    if raw:
        runtime_env = Path(raw)
    else:
        config_home = os.environ.get(
            "KIMI_CONFIG_HOME", str(Path.home() / ".config" / "kimi-agent")
        )
        runtime_env = Path(config_home) / "runtime.env"
    if not runtime_env.is_file():
        return f"no runtime.env overlay ({runtime_env} absent)"

    values = dotenv_values(runtime_env, interpolate=False)
    malformed = sorted(key for key, value in values.items() if value is None)
    if malformed:
        raise SystemExit(f"invalid assignment(s) in {runtime_env}: {', '.join(malformed)}")
    os.environ.update({key: value for key, value in values.items() if value is not None})
    return f"merged RUNTIME_ENV={runtime_env}"
