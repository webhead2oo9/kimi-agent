"""Module storage: prefixed naming, scoped migrations, and shared lock."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from kimi_agent_module_api import (
    MODULE_API_VERSION,
    AppModule,
    ModuleLoadContext,
    ModuleRuntimeContext,
    ModuleSpec,
)
from kimi_agent_module_api.contracts import (
    MigrationContext,
    ModuleContractError,
    ScopedModuleMigration,
)
from modules.storage import ModuleStorageImpl
from modules.testing import build_test_runtime
from storage.db import Database


def test_table_names_are_prefixed_and_quoted() -> None:
    storage = ModuleStorageImpl(database=object(), module_name="audit-log")
    assert storage.table("sync_state") == '"audit_log_sync_state"'
    assert storage.table("hashes") == '"audit_log_hashes"'
    with pytest.raises(ModuleContractError):
        storage.table("Bad Name")
    with pytest.raises(ModuleContractError):
        storage.table("x; DROP TABLE y")


class ScopedModule:
    """Declares scoped migrations that name tables through the context."""

    scoped_migrations: Sequence[ScopedModuleMigration] = ()

    def __init__(self) -> None:
        self.rows: list[tuple[int, str]] = []
        self.scoped_migrations = (("init", self._init), ("add_index", self._index))

    async def _init(self, ctx: MigrationContext) -> None:
        await ctx.connection.execute(
            f"CREATE TABLE {ctx.table('cases')} (id INTEGER PRIMARY KEY, reason TEXT NOT NULL)"
        )

    async def _index(self, ctx: MigrationContext) -> None:
        await ctx.connection.execute(
            f"CREATE INDEX {ctx.table('cases_reason')} ON {ctx.table('cases')} (reason)"
        )

    async def start(self, ctx: ModuleRuntimeContext) -> None:
        assert ctx.storage is not None
        async with ctx.storage.write_transaction() as conn:
            await conn.execute(
                f"INSERT INTO {ctx.storage.table('cases')} (reason) VALUES (?)", ("spam",)
            )
        cursor = await ctx.storage.connection.execute(
            f"SELECT id, reason FROM {ctx.storage.table('cases')}"
        )
        self.rows = [tuple(row) for row in await cursor.fetchall()]

    async def close(self) -> None:
        pass


def _spec(name: str, instance: AppModule, **overrides: object) -> ModuleSpec:
    def create(_ctx: ModuleLoadContext) -> AppModule:
        return instance

    api_version = overrides.pop("api_version", MODULE_API_VERSION)
    return ModuleSpec(
        name=name,
        version="1.0.0",
        create=create,
        api_version=api_version,  # type: ignore[arg-type]
        **overrides,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_scoped_migrations_run_through_the_ledger_with_prefixed_names(tmp_path: Path) -> None:
    module = ScopedModule()
    runtime = await build_test_runtime(tmp_path, ["mod"], installed={"mod": _spec("mod", module)})
    try:
        assert module.rows == [(1, "spam")]
        cursor = await runtime.database.conn.execute(
            "SELECT type, name FROM sqlite_master ORDER BY name"
        )
        rows = [tuple(row) for row in await cursor.fetchall()]
        assert [row for row in rows if row[1].startswith("mod_c")] == [
            ("table", "mod_cases"),
            ("index", "mod_cases_reason"),
        ]
        cursor = await runtime.database.conn.execute(
            "SELECT version, name FROM module_schema_versions WHERE module_name = 'mod' ORDER BY version"
        )
        assert [tuple(row) for row in await cursor.fetchall()] == [(1, "init"), (2, "add_index")]
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_module_host_rejects_non_callable_migration_before_database_work(
    tmp_path: Path,
) -> None:
    module = ScopedModule()
    module.scoped_migrations = (("broken", object()),)  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="migration 'broken' is not callable"):
        await build_test_runtime(tmp_path, ["mod"], installed={"mod": _spec("mod", module)})

    database = Database(tmp_path / "bot.db")
    await database.connect()
    try:
        cursor = await database.conn.execute(
            "SELECT version FROM module_schema_versions WHERE module_name = 'mod'"
        )
        assert await cursor.fetchall() == []
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_module_migrations_reject_applied_history_drift_before_new_work(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "bot.db")
    await database.connect()
    ran: list[str] = []

    async def initial(conn: Any) -> None:
        await conn.execute("CREATE TABLE drift_initial (id INTEGER PRIMARY KEY)")

    async def must_not_run(conn: Any) -> None:
        ran.append("new")
        await conn.execute("CREATE TABLE drift_new (id INTEGER PRIMARY KEY)")

    try:
        await database.apply_module_migrations("drift", (("initial", initial),))

        with pytest.raises(RuntimeError, match="history diverged at v1"):
            await database.apply_module_migrations(
                "drift",
                (("renamed_initial", initial), ("new", must_not_run)),
            )

        assert ran == []
        cursor = await database.conn.execute(
            "SELECT version, name FROM module_schema_versions "
            "WHERE module_name = 'drift' ORDER BY version"
        )
        assert [tuple(row) for row in await cursor.fetchall()] == [(1, "initial")]
        cursor = await database.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'drift_new'"
        )
        assert await cursor.fetchone() is None
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_module_migrations_reject_invalid_declarations_without_mutating_ledger(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "bot.db")
    await database.connect()

    async def no_op(_conn: Any) -> None:
        pass

    try:
        with pytest.raises(ValueError, match="non-empty name"):
            await database.apply_module_migrations("invalid", (("", no_op),))
        with pytest.raises(ValueError, match="duplicate migration name 'same'"):
            await database.apply_module_migrations("invalid", (("same", no_op), ("same", no_op)))
        with pytest.raises(ValueError, match="is not callable"):
            await database.apply_module_migrations(
                "invalid",
                (("not_callable", object()),),  # type: ignore[arg-type]
            )

        cursor = await database.conn.execute(
            "SELECT version FROM module_schema_versions WHERE module_name = 'invalid'"
        )
        assert await cursor.fetchall() == []
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_module_migrations_reject_non_contiguous_ledger(tmp_path: Path) -> None:
    database = Database(tmp_path / "bot.db")
    await database.connect()

    async def no_op(_conn: Any) -> None:
        pass

    try:
        await database.conn.execute(
            "INSERT INTO module_schema_versions "
            "(module_name, version, name, applied_at) VALUES ('gap', 2, 'second', 'now')"
        )
        await database.conn.commit()

        with pytest.raises(RuntimeError, match="ledger is not contiguous"):
            await database.apply_module_migrations("gap", (("first", no_op), ("second", no_op)))
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_failed_module_migration_rolls_back_schema_and_ledger(tmp_path: Path) -> None:
    database = Database(tmp_path / "bot.db")
    await database.connect()

    async def fail(conn: Any) -> None:
        await conn.execute("CREATE TABLE rolled_back (id INTEGER PRIMARY KEY)")
        raise RuntimeError("migration failed")

    try:
        with pytest.raises(RuntimeError, match="migration failed"):
            await database.apply_module_migrations("rollback", (("initial", fail),))

        cursor = await database.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'rolled_back'"
        )
        assert await cursor.fetchone() is None
        cursor = await database.conn.execute(
            "SELECT version FROM module_schema_versions WHERE module_name = 'rollback'"
        )
        assert await cursor.fetchall() == []
    finally:
        await database.close()
