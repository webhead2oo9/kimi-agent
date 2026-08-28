"""Forward-only schema migrations for the module's tables.

The host keeps one version row per module in ``module_schema_versions`` and
applies, in order, every migration whose name it has not recorded yet. Rules
that follow from that design:

- Never edit or reorder a migration that has shipped; append a new one. The
  name is the identity, so ``001_...``, ``002_...`` keeps the order obvious.
- Use ``ctx.table("<logical>")`` for every table name. It returns the quoted
  physical name (``"reference_kudos_kudos"`` for this module), which keeps the
  module's tables out of every other module's way on the shared database.
- Migrations run inside the host's startup sequence before ``start()``, so
  ``start()`` can assume the schema is current.
"""

from __future__ import annotations

from kimi_agent_module_api import ScopedModuleMigration
from kimi_agent_module_api.contracts import MigrationContext


async def create_kudos(ctx: MigrationContext) -> None:
    """One row per kudos given. ``given_at`` is unix seconds."""
    kudos = ctx.table("kudos")
    await ctx.connection.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {kudos} (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id    INTEGER NOT NULL,
            giver_id    INTEGER NOT NULL,
            receiver_id INTEGER NOT NULL,
            reason      TEXT    NOT NULL,
            given_at    REAL    NOT NULL
        )
        """
    )


async def index_kudos(ctx: MigrationContext) -> None:
    """A second migration, to show that later schema work is simply appended.

    Every read is scoped to one guild and most order by time, so a composite
    index keeps the leaderboard cheap as the table grows.
    """
    kudos = ctx.table("kudos")
    # Index names share the database-wide namespace, so prefix them too.
    await ctx.connection.execute(
        f"CREATE INDEX IF NOT EXISTS reference_kudos_by_guild_time ON {kudos} (guild_id, given_at)"
    )


MIGRATIONS: tuple[ScopedModuleMigration, ...] = (
    ("001_create_kudos", create_kudos),
    ("002_index_kudos", index_kudos),
)

__all__ = ["MIGRATIONS"]
