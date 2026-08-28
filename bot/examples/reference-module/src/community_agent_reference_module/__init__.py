"""A minimal, complete community module used as executable documentation."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from pydantic_settings import BaseSettings, SettingsConfigDict

from community_agent_module_api import (
    AppModule,
    ModuleLoadContext,
    ModuleRuntimeContext,
    ModuleSetting,
    ModuleSettingsDefinition,
    ModuleSpec,
    ModuleToolContext,
    ScopedModuleMigration,
)
from community_agent_module_api.contracts import MigrationContext


class ReferenceSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="REFERENCE_GREETER_", extra="ignore")

    greeting: str = "Hello"


async def _create_state(ctx: MigrationContext) -> None:
    table = ctx.table("state")
    await ctx.connection.execute(
        f"CREATE TABLE IF NOT EXISTS {table} (key TEXT PRIMARY KEY, value INTEGER NOT NULL)"
    )
    await ctx.connection.execute(
        f"INSERT OR IGNORE INTO {table} (key, value) VALUES ('invocations', 0)"
    )


class ReferenceGreeter:
    scoped_migrations: Sequence[ScopedModuleMigration] = (("001_create_state", _create_state),)

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


def create(ctx: ModuleLoadContext) -> AppModule:
    settings = ctx.settings_for(ReferenceSettings)
    module = ReferenceGreeter(settings.greeting)
    ctx.registry.register(
        "reference_greet",
        "Greet someone and report this module's persistent invocation count.",
        {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Who to greet."}},
            "required": [],
            "additionalProperties": False,
        },
        module.greet,
    )
    ctx.register_tool_labels({"reference_greet": "Greeting someone"})
    return module


SETTINGS = ModuleSettingsDefinition(
    name="reference_greeter",
    label="Reference greeter",
    model=ReferenceSettings,
    exposed=(
        ModuleSetting(
            field="greeting",
            label="Greeting",
            help="The opening word used by reference_greet.",
        ),
    ),
)

SPEC = ModuleSpec(
    name="reference_greeter",
    version="1.0.0",
    create=create,
    settings=SETTINGS,
)

__all__ = ["SPEC", "ReferenceGreeter", "ReferenceSettings", "create"]
