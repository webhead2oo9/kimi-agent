"""Modules that must not drag in the settings singleton or bot runtime.

``config/paths.py`` supplies the config directory to readers that run before the
settings singleton exists, so importing settings there would be a cycle and would
make the process-wide default unsettable. ``tools/config_spec.py`` is declared at
import time by tool modules that load long before the settings overlay is
applied, so coupling it would put boot ordering between a tool and its own
configuration declaration.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

# Importing the module under test must not pull any of these in transitively.
_FORBIDDEN = (
    "config.settings",
    "app.runtime",
    "discord",
    "aiosqlite",
)


@pytest.mark.parametrize(
    "module",
    [
        "config.paths",
        "tools.config_spec",
    ],
)
def test_module_does_not_import_runtime_state(module: str) -> None:
    # A subprocess, because the pytest process has already imported nearly
    # everything: an in-process sys.modules check would always pass.
    script = (
        "import sys, importlib;"
        f"importlib.import_module({module!r});"
        f"leaked=[m for m in {_FORBIDDEN!r} if m in sys.modules];"
        "print(','.join(leaked))"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    leaked = result.stdout.strip()
    assert not leaked, f"{module} transitively imported: {leaked}"
