"""Persistent guild scopes used to clean up module-owned app commands."""

from __future__ import annotations

from storage.db import Database


class GuildCommandScopeStore:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def track(self, guild_id: int) -> None:
        async with self._database.write_transaction() as conn:
            await conn.execute(
                "INSERT OR IGNORE INTO module_command_guilds (guild_id) VALUES (?)",
                (str(guild_id),),
            )

    async def guild_ids(self) -> tuple[int, ...]:
        cursor = await self._database.conn.execute("SELECT guild_id FROM module_command_guilds")
        rows = await cursor.fetchall()
        return tuple(int(row[0]) for row in rows)

    async def forget(self, guild_id: int) -> None:
        async with self._database.write_transaction() as conn:
            await conn.execute(
                "DELETE FROM module_command_guilds WHERE guild_id = ?",
                (str(guild_id),),
            )


__all__ = ["GuildCommandScopeStore"]
