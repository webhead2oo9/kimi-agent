from __future__ import annotations

import pytest

from storage.db import Database
from storage.privacy import PrivacyDeletionRequestStore


@pytest.mark.asyncio
async def test_privacy_deletion_request_survives_database_reopen(tmp_path) -> None:
    path = tmp_path / "bot.db"
    db = Database(path)
    await db.connect()
    store = PrivacyDeletionRequestStore(db)
    request = await store.request(
        user_id="42",
        scope="memory",
        memory_backend_required=True,
        now=10.0,
    )
    await db.close()

    reopened = Database(path)
    await reopened.connect()
    try:
        pending = await PrivacyDeletionRequestStore(reopened).list_pending()
    finally:
        await reopened.close()

    assert pending == [request]


@pytest.mark.asyncio
async def test_repeated_request_keeps_widest_scope_and_backend_requirement(
    tmp_path,
) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        store = PrivacyDeletionRequestStore(db)
        first = await store.request(
            user_id="42",
            scope="memory",
            memory_backend_required=True,
            now=10.0,
        )
        second = await store.request(
            user_id="42",
            scope="all",
            memory_backend_required=False,
            now=20.0,
        )
        third = await store.request(
            user_id="42",
            scope="memory",
            memory_backend_required=False,
            now=30.0,
        )

        assert first.generation == 1
        assert second.generation == 2
        assert third.generation == 3
        assert third.scope == "all"
        assert third.memory_backend_required is True
        assert third.requested_at == 10.0
        assert third.updated_at == 30.0
        assert len({first.request_token, second.request_token, third.request_token}) == 3
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_stale_completion_cannot_remove_newer_request(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        store = PrivacyDeletionRequestStore(db)
        older = await store.request(
            user_id="42",
            scope="memory",
            memory_backend_required=False,
        )
        newer = await store.request(
            user_id="42",
            scope="all",
            memory_backend_required=False,
        )

        assert await store.complete(older) is False
        assert await store.list_pending() == [newer]
        assert await store.complete(newer) is True
        assert await store.list_pending() == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_request_token_prevents_aba_after_generation_resets(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        store = PrivacyDeletionRequestStore(db)
        stale_generation_one = await store.request(
            user_id="42",
            scope="memory",
            memory_backend_required=False,
        )
        generation_two = await store.request(
            user_id="42",
            scope="all",
            memory_backend_required=False,
        )
        assert await store.complete(generation_two) is True

        fresh_generation_one = await store.request(
            user_id="42",
            scope="memory",
            memory_backend_required=False,
        )
        assert fresh_generation_one.generation == stale_generation_one.generation == 1
        assert fresh_generation_one.request_token != stale_generation_one.request_token

        assert await store.complete(stale_generation_one) is False
        assert await store.list_pending() == [fresh_generation_one]
    finally:
        await db.close()
