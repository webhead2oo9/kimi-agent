"""Operator configuration that lives in markdown fragments, read fresh each turn.

`config/settings.py` and `config/operator_settings.py` are the boot-time
configuration surface. This is the other one: `<CONFIG_DIR>/servers/<id>.md`,
`channels/<id>.md`, `tools/<name>.md`, `tools.md`, and the prompt templates,
all parsed on the turn that needs them so an operator edit takes effect on the
next message without a restart.

These modules lived under `agent/` and were read by `app/`, `trust/` and the
turn path rather than by the ReAct core, which is what made `agent` look like a
dependency of everything. Nothing here imports `agent`.

Deliberately not re-exported here: importing `config.fragments` must stay cheap,
and `config/paths.py` is held by `tests/test_import_isolation.py` to dragging in
no runtime state. Import the specific module you need.
"""

from __future__ import annotations
