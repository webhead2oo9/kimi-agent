"""Reference Kimi module: a small "kudos" feature that exercises every host port.

The package is split by responsibility so each file reads as one lesson:

- ``spec.py``        the ``ModuleSpec`` (identity, declarations, load-time wiring)
- ``settings.py``    deployment settings, with the operator-editable subset
- ``guild_settings`` the per-guild schema staff edit through proposals
- ``migrations.py``  forward-only schema changes on the module's own tables
- ``ledger.py``      the SQL layer, written against the ``ModuleStorage`` port
- ``module.py``      the lifecycle object: tools, commands, jobs, events

Only ``SPEC`` is public. The entry point in ``pyproject.toml`` points here, and
that is the whole coupling between this package and the host.
"""

from community_agent_reference_module.spec import SPEC

__all__ = ["SPEC"]
