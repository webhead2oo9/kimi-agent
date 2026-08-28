"""Module-scoped view of the shared application database.

The connection is the same one core uses: this is naming discipline and an
audited convention for trusted code, not SQL isolation. ``table(name)``
returns ``<module>_<name>`` (hyphens normalized to underscores), or the legacy
physical name when the module declares a ``table_aliases`` entry so an
existing installation keeps its data until the physical rename ships.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from kimi_agent_module_api.contracts import (
    TABLE_NAME_RE,
    MigrationContext,
    ModuleContractError,
    table_prefix,
)

_IDENTIFIER_RE = TABLE_NAME_RE


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


@dataclass(frozen=True, slots=True)
class ModuleStorageImpl:
    """The ``ModuleStorage`` port handed to one module."""

    database: Any
    module_name: str
    table_aliases: Mapping[str, str] = field(default_factory=dict)

    @property
    def prefix(self) -> str:
        return table_prefix(self.module_name)

    @property
    def connection(self) -> Any:
        return self.database.conn

    def table(self, name: str) -> str:
        """Quoted physical table name for a logical module table."""
        if not _IDENTIFIER_RE.match(name):
            raise ModuleContractError(
                f"module {self.module_name!r} table name {name!r} is not a valid identifier"
            )
        physical = self.table_aliases.get(name, f"{self.prefix}_{name}")
        return _quote(physical)

    def physical_names(self, names: Mapping[str, Any] | tuple[str, ...]) -> tuple[str, ...]:
        return tuple(self.table(name) for name in names)

    def write_transaction(self) -> Any:
        return self.database.write_transaction()

    def migration_context(self, connection: Any) -> MigrationContext:
        return MigrationContext(connection=connection, table=self.table)


def validate_table_aliases(module_name: str, aliases: Mapping[str, str]) -> None:
    prefix = table_prefix(module_name)
    for logical, physical in aliases.items():
        if not _IDENTIFIER_RE.match(logical):
            raise ModuleContractError(
                f"module {module_name!r} alias {logical!r} is not a valid identifier"
            )
        if not _IDENTIFIER_RE.match(physical):
            raise ModuleContractError(
                f"module {module_name!r} alias target {physical!r} is not a valid identifier"
            )
        if physical.startswith(f"{prefix}_"):
            raise ModuleContractError(
                f"module {module_name!r} alias {logical!r} targets an already-prefixed table; "
                "drop the alias"
            )


__all__ = ["ModuleStorageImpl", "validate_table_aliases"]
