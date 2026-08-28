"""SQL for the kudos table, written only against the ``ModuleStorage`` port.

Keeping every query in one class does two things: the lifecycle object in
``module.py`` stays readable, and the ledger can be unit-tested with an
in-memory SQLite connection (see ``tests/conftest.py``) without a host.

Conventions worth copying:

- Reads go straight through ``storage.connection``.
- Every write goes through ``async with storage.write_transaction()``. The
  host shares one connection between core and all modules; the context
  manager serializes writers and scopes commit/rollback to this unit of work.
  A write outside it can be committed or rolled back by a bystander.
- Table names always come from ``storage.table(...)``.
"""

from __future__ import annotations

from dataclasses import dataclass

from kimi_agent_module_api import ModuleStorage

DAY_SECONDS = 24 * 60 * 60


@dataclass(frozen=True, slots=True)
class BoardEntry:
    user_id: int
    count: int


@dataclass(frozen=True, slots=True)
class Kudos:
    id: int
    guild_id: int
    giver_id: int
    receiver_id: int
    reason: str
    given_at: float


class KudosLedger:
    def __init__(self, storage: ModuleStorage) -> None:
        self._storage = storage
        # Resolve the quoted physical name once; it never changes at runtime.
        self._kudos = storage.table("kudos")

    async def give(
        self, guild_id: int, giver_id: int, receiver_id: int, reason: str, now: float
    ) -> Kudos:
        async with self._storage.write_transaction() as connection:
            cursor = await connection.execute(
                f"INSERT INTO {self._kudos} (guild_id, giver_id, receiver_id, reason, given_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (guild_id, giver_id, receiver_id, reason, now),
            )
            row_id = int(cursor.lastrowid or 0)
        return Kudos(row_id, guild_id, giver_id, receiver_id, reason, now)

    async def get(self, kudos_id: int) -> Kudos | None:
        cursor = await self._storage.connection.execute(
            f"SELECT id, guild_id, giver_id, receiver_id, reason, given_at "
            f"FROM {self._kudos} WHERE id = ?",
            (kudos_id,),
        )
        row = await cursor.fetchone()
        return None if row is None else Kudos(*row)

    async def given_recently(self, guild_id: int, giver_id: int, now: float) -> int:
        """Kudos ``giver_id`` gave in this guild during the trailing 24 hours."""
        cursor = await self._storage.connection.execute(
            f"SELECT COUNT(*) FROM {self._kudos} "
            "WHERE guild_id = ? AND giver_id = ? AND given_at > ?",
            (guild_id, giver_id, now - DAY_SECONDS),
        )
        row = await cursor.fetchone()
        return int(row[0]) if row is not None else 0

    async def top(self, guild_id: int, *, since: float, limit: int) -> list[BoardEntry]:
        """Receivers ranked by kudos received after ``since``; ties break by user id."""
        cursor = await self._storage.connection.execute(
            f"SELECT receiver_id, COUNT(*) AS n FROM {self._kudos} "
            "WHERE guild_id = ? AND given_at > ? "
            "GROUP BY receiver_id ORDER BY n DESC, receiver_id ASC LIMIT ?",
            (guild_id, since, limit),
        )
        return [BoardEntry(int(user_id), int(count)) for user_id, count in await cursor.fetchall()]

    async def forget_member(self, guild_id: int, user_id: int) -> int:
        """Erase everything that mentions a member who left; returns rows removed."""
        async with self._storage.write_transaction() as connection:
            cursor = await connection.execute(
                f"DELETE FROM {self._kudos} "
                "WHERE guild_id = ? AND (giver_id = ? OR receiver_id = ?)",
                (guild_id, user_id, user_id),
            )
            return int(cursor.rowcount or 0)


__all__ = ["DAY_SECONDS", "BoardEntry", "Kudos", "KudosLedger"]
