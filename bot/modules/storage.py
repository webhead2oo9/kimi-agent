"""Module-scoped view of the shared application database.

The connection is the same one core uses: this is naming discipline and an
audited convention for trusted code, not SQL isolation. ``table(name)``
returns ``<module>_<name>`` (hyphens normalized to underscores).
"""

from __future__ import annotations

from dataclasses import dataclass
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
        return _quote(f"{self.prefix}_{name}")

    def write_transaction(self) -> Any:
        return self.database.write_transaction()

    def migration_context(self, connection: Any) -> MigrationContext:
        return MigrationContext(connection=connection, table=self.table)


__all__ = ["ModuleStorageImpl"]
