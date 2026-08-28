"""Lifecycle object and tool implementation for the reference module."""

from collections.abc import Sequence
from typing import Any

from kimi_agent_module_api import ModuleRuntimeContext, ModuleToolContext, ScopedModuleMigration

from community_agent_reference_module.migrations import MIGRATIONS


class ReferenceGreeter:
    scoped_migrations: Sequence[ScopedModuleMigration] = MIGRATIONS

    def __init__(self, greeting: str) -> None:
        self._greeting = greeting
        self._runtime: ModuleRuntimeContext | None = None

    async def start(self, ctx: ModuleRuntimeContext) -> None:
        self._runtime = ctx

    async def close(self) -> None:
        self._runtime = None

    async def greet(self, arguments: dict[str, Any], _ctx: ModuleToolContext) -> str:
        if self._runtime is None:
            raise RuntimeError("reference_greeter has not started")
        name = str(arguments.get("name") or "there").strip() or "there"
        table = self._runtime.storage.table("state")
        async with self._runtime.storage.write_transaction() as connection:
            await connection.execute(
                f"UPDATE {table} SET value = value + 1 WHERE key = 'invocations'"
            )
            cursor = await connection.execute(
                f"SELECT value FROM {table} WHERE key = 'invocations'"
            )
            row = await cursor.fetchone()
        count = int(row[0]) if row is not None else 0
        return f"{self._greeting}, {name}! I have greeted someone {count} time(s)."


__all__ = ["ReferenceGreeter"]
