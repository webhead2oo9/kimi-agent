"""Forward-only database migrations owned by the reference module."""

from kimi_agent_module_api import ScopedModuleMigration
from kimi_agent_module_api.contracts import MigrationContext


async def create_state(ctx: MigrationContext) -> None:
    table = ctx.table("state")
    await ctx.connection.execute(
        f"CREATE TABLE IF NOT EXISTS {table} (key TEXT PRIMARY KEY, value INTEGER NOT NULL)"
    )
    await ctx.connection.execute(
        f"INSERT OR IGNORE INTO {table} (key, value) VALUES ('invocations', 0)"
    )


MIGRATIONS: tuple[ScopedModuleMigration, ...] = (("001_create_state", create_state),)

__all__ = ["MIGRATIONS"]
