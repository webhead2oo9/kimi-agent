from __future__ import annotations

import pytest

from storage.db import SCHEMA_VERSION, Database


@pytest.mark.asyncio
async def test_usage_ledgers_and_indexes_exist_in_current_schema(tmp_path) -> None:
    db = Database(path=tmp_path / "usage.db")
    await db.connect()
    try:
        async with db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='usage_ledger'"
        ) as cur:
            assert await cur.fetchone() is not None

        async with db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_usage_%'"
        ) as cur:
            names = {row["name"] for row in await cur.fetchall()}
        async with db.conn.execute("PRAGMA table_info(usage_ledger)") as cur:
            columns = {row["name"] for row in await cur.fetchall()}
        async with db.conn.execute("PRAGMA table_info(paid_usage_ledger)") as cur:
            paid_columns = {row["name"] for row in await cur.fetchall()}
        async with db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_paid_usage_%'"
        ) as cur:
            paid_indexes = {row["name"] for row in await cur.fetchall()}
        async with db.conn.execute("PRAGMA table_info(usage_markers)") as cur:
            marker_columns = {row["name"] for row in await cur.fetchall()}
        async with db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_usage_markers_%'"
        ) as cur:
            marker_indexes = {row["name"] for row in await cur.fetchall()}
    finally:
        await db.close()

    assert {"turn_id", "pricing_model"} <= columns
    assert "idx_usage_turn" in names
    assert {"idx_usage_user_time", "idx_usage_guild_time", "idx_usage_time"} <= names
    assert {
        "user_id",
        "tool_name",
        "provider",
        "cost_usd",
        "turn_id",
        "created_at",
    } <= paid_columns
    assert {
        "idx_paid_usage_user_time",
        "idx_paid_usage_guild_time",
        "idx_paid_usage_time",
        "idx_paid_usage_turn",
    } <= paid_indexes
    assert {"user_id", "surface", "operation", "unit_count", "created_at"} <= marker_columns
    assert {"idx_usage_markers_user_surface_time", "idx_usage_markers_time"} <= marker_indexes
    assert SCHEMA_VERSION == 7
