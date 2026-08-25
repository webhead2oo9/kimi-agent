from __future__ import annotations

import asyncio

import pytest

from storage import blocked_users
from storage.blocked_users import BlockedUserStore
from storage.db import Database


@pytest.mark.asyncio
async def test_blocked_user_round_trips_and_delete_unblocks(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    store = BlockedUserStore(db)

    try:
        assert await store.is_blocked("123") is False

        changed = await store.block_user(
            user_id="123",
            blocked_by="999",
            reason="spammy tool use",
        )
        record = await store.get_block("123")

        assert changed is True
        assert await store.is_blocked("123") is True
        assert record is not None
        assert record.user_id == "123"
        assert record.blocked_by == "999"
        assert record.reason == "spammy tool use"

        unblocked = await store.unblock_user("123")

        assert unblocked is True
        assert await store.is_blocked("123") is False
        assert await store.get_block("123") is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_block_user_updates_existing_row_without_creating_second_state(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    store = BlockedUserStore(db)

    try:
        assert await store.block_user("123", blocked_by="999", reason="first") is True
        assert await store.block_user("123", blocked_by="888", reason="updated") is False

        async with db.conn.execute(
            "SELECT COUNT(*) FROM blocked_users WHERE user_id = ?",
            ("123",),
        ) as cur:
            row = await cur.fetchone()
        record = await store.get_block("123")

        assert row is not None
        assert row[0] == 1
        assert record is not None
        assert record.blocked_by == "888"
        assert record.reason == "updated"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_block_user_created_flag_survives_identical_timestamps(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    store = BlockedUserStore(db)
    monkeypatch.setattr(blocked_users.time, "time", lambda: 123.0)

    try:
        assert await store.block_user("123", blocked_by="999", reason="first") is True
        assert await store.block_user("123", blocked_by="888", reason="updated") is False

        record = await store.get_block("123")
        assert record is not None
        assert record.created_at == record.updated_at == 123.0
        assert record.blocked_by == "888"
        assert record.reason == "updated"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_concurrent_first_blocks_report_created_exactly_once(tmp_path) -> None:
    # The created flag comes from SQLite's insert/conflict outcome, so a
    # concurrent double-submit cannot make both calls report a fresh block.
    db = Database(tmp_path / "bot.db")
    await db.connect()
    store = BlockedUserStore(db)

    try:
        results = await asyncio.gather(
            store.block_user("123", blocked_by="999", reason="first"),
            store.block_user("123", blocked_by="888", reason="second"),
        )

        assert sorted(results) == [False, True]
        record = await store.get_block("123")
        assert record is not None
        assert record.blocked_by in {"999", "888"}
    finally:
        await db.close()
