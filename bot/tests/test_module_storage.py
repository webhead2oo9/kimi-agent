"""Module storage: prefixed naming, aliases, scoped migrations, shared lock."""

from __future__ import annotations

from pathlib import Path

import pytest

from community_agent_module_api import ModuleLoadContext, ModuleRuntimeContext, ModuleSpec
from community_agent_module_api.contracts import MigrationContext, ModuleContractError
from modules.storage import ModuleStorageImpl, validate_table_aliases
from modules.testing import build_test_runtime


def test_table_names_are_prefixed_quoted_and_alias_aware() -> None:
    storage = ModuleStorageImpl(
        database=object(),
        module_name="image-fingerprints",
        table_aliases={"hashes": "image_fingerprints"},
    )
    assert storage.table("sync_state") == '"image_fingerprints_sync_state"'
    assert storage.table("hashes") == '"image_fingerprints"'
    with pytest.raises(ModuleContractError):
        storage.table("Bad Name")
    with pytest.raises(ModuleContractError):
        storage.table("x; DROP TABLE y")


def test_alias_validation_rejects_prefixed_targets_and_bad_identifiers() -> None:
    validate_table_aliases("community_moderation", {"cases": "moderation_cases"})
    with pytest.raises(ModuleContractError):
        validate_table_aliases("community_moderation", {"cases": "community_moderation_cases"})
    with pytest.raises(ModuleContractError):
        validate_table_aliases("m", {"ok": "not valid"})
    with pytest.raises(ModuleContractError):
        validate_table_aliases("m", {"Bad": "fine"})


class ScopedModule:
    """Declares scoped migrations that name tables through the context."""

    migrations = ()

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


def _spec(name: str, instance: object, **overrides: object) -> ModuleSpec:
    def create(_ctx: ModuleLoadContext) -> object:
        return instance

    return ModuleSpec(name=name, version="1.0.0", create=create, **overrides)  # type: ignore[arg-type]


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
async def test_aliases_resolve_to_legacy_tables(tmp_path: Path) -> None:
    class Legacy:
        migrations = ()

        def __init__(self) -> None:
            self.scoped_migrations = (("init", self._init),)
            self.seen = ""

        async def _init(self, ctx: MigrationContext) -> None:
            await ctx.connection.execute(
                f"CREATE TABLE {ctx.table('cases')} (id INTEGER PRIMARY KEY)"
            )

        async def start(self, ctx: ModuleRuntimeContext) -> None:
            assert ctx.storage is not None
            self.seen = ctx.storage.table("cases")

        async def close(self) -> None:
            pass

    module = Legacy()
    runtime = await build_test_runtime(
        tmp_path,
        ["community_moderation"],
        installed={
            "community_moderation": _spec(
                "community_moderation", module, table_aliases={"cases": "moderation_cases"}
            )
        },
    )
    try:
        assert module.seen == '"moderation_cases"'
        cursor = await runtime.database.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'moderation_cases'"
        )
        assert await cursor.fetchone() is not None
    finally:
        await runtime.close()
