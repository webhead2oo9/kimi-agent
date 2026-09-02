from __future__ import annotations

import pytest

from storage.db import Database
from storage.video_sessions import VideoSessionStore


async def _conversation(db: Database, key: str = "root") -> int:
    async with db.write_transaction() as conn:
        cursor = await conn.execute(
            """
            INSERT INTO conversations (
                key, guild_id, channel_id, created_at, last_active_at
            ) VALUES (?, 'guild', 'channel', 1, 1)
            """,
            (key,),
        )
        assert cursor.lastrowid is not None
        return cursor.lastrowid


@pytest.mark.asyncio
async def test_video_session_is_scoped_and_advances_atomically(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        conversation_id = await _conversation(db)
        store = VideoSessionStore(db)
        await store.create_session(
            handle="video_local",
            conversation_id=conversation_id,
            actor_user_id="user",
            guild_id="guild",
            youtube_url="https://www.youtube.com/watch?v=abcdefghijk",
            youtube_video_id="abcdefghijk",
            model="gemini-3.7-flash",
            interaction_id="remote-1",
            now=10,
            expires_at=100,
        )

        found = await store.find_sessions(
            conversation_id=conversation_id,
            actor_user_id="user",
            guild_id="guild",
            now=20,
            handle=None,
        )
        assert len(found) == 1
        assert found[0].latest_interaction_id == "remote-1"
        assert found[0].source_kind == "youtube"
        assert not await store.find_sessions(
            conversation_id=conversation_id,
            actor_user_id="other",
            guild_id="guild",
            now=20,
            handle="video_local",
        )
        assert not await store.find_sessions(
            conversation_id=conversation_id,
            actor_user_id="user",
            guild_id="other-guild",
            now=20,
            handle="video_local",
        )

        assert await store.advance_session(
            handle="video_local",
            actor_user_id="user",
            expected_interaction_id="remote-1",
            interaction_id="remote-2",
            now=30,
            expires_at=130,
            max_interactions=3,
        )
        assert not await store.advance_session(
            handle="video_local",
            actor_user_id="user",
            expected_interaction_id="remote-1",
            interaction_id="orphan",
            now=31,
            expires_at=131,
            max_interactions=3,
        )
        updated = await store.find_sessions(
            conversation_id=conversation_id,
            actor_user_id="user",
            guild_id="guild",
            now=40,
            handle="video_local",
        )
        assert updated[0].latest_interaction_id == "remote-2"
        assert updated[0].interaction_count == 2
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_uploaded_file_reservation_is_claimed_atomically(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        conversation_id = await _conversation(db)
        store = VideoSessionStore(db)
        await store.reserve_provider_file(
            file_name="files/kv-upload",
            conversation_id=conversation_id,
            actor_user_id="user",
            guild_id="guild",
            mime_type="video/mp4",
            byte_size=5,
            now=10,
        )
        await store.create_uploaded_session(
            handle="video_upload",
            conversation_id=conversation_id,
            actor_user_id="user",
            guild_id="guild",
            source_kind="attachment",
            source_display_name="clip.mp4",
            source_locator="clip.mp4",
            source_byte_size=5,
            model="gemini-3.7-flash",
            interaction_id="remote-upload",
            file_name="files/kv-upload",
            now=20,
            expires_at=100,
        )

        [session] = await store.find_sessions(
            conversation_id=conversation_id,
            actor_user_id="user",
            guild_id="guild",
            now=30,
            handle="video_upload",
        )
        assert session.source_kind == "attachment"
        assert session.source_display_name == "clip.mp4"
        assert session.source_locator == "clip.mp4"
        assert session.source_byte_size == 5
        assert not session.youtube_url
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_uploaded_session_rolls_back_without_reservation(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        conversation_id = await _conversation(db)
        store = VideoSessionStore(db)
        with pytest.raises(ValueError, match="reservation"):
            await store.create_uploaded_session(
                handle="video_upload",
                conversation_id=conversation_id,
                actor_user_id="user",
                guild_id="guild",
                source_kind="workspace",
                source_display_name="clip.webm",
                source_locator="imports/clip.webm",
                source_byte_size=5,
                model="gemini-3.7-flash",
                interaction_id="remote-upload",
                file_name="files/kv-missing",
                now=20,
                expires_at=100,
            )
        assert not await store.find_sessions(
            conversation_id=conversation_id,
            actor_user_id="user",
            guild_id="guild",
            now=30,
            handle=None,
        )
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_deleting_uploaded_session_orders_interaction_before_file(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        conversation_id = await _conversation(db)
        store = VideoSessionStore(db)
        await store.reserve_provider_file(
            file_name="files/kv-upload",
            conversation_id=conversation_id,
            actor_user_id="user",
            guild_id="guild",
            mime_type="video/mp4",
            byte_size=5,
            now=10,
        )
        await store.create_uploaded_session(
            handle="video_upload",
            conversation_id=conversation_id,
            actor_user_id="user",
            guild_id="guild",
            source_kind="attachment",
            source_display_name="clip.mp4",
            source_locator="clip.mp4",
            source_byte_size=5,
            model="gemini-3.7-flash",
            interaction_id="remote-1",
            file_name="files/kv-upload",
            now=20,
            expires_at=100,
        )
        assert await store.advance_session(
            handle="video_upload",
            actor_user_id="user",
            expected_interaction_id="remote-1",
            interaction_id="remote-2",
            now=30,
            expires_at=110,
            max_interactions=5,
        )

        assert await store.delete_user_sessions("user") == 1
        pending = await store.pending_deletions(user_id="user", limit=10)
        assert {item.interaction_id for item in pending} == {"remote-1", "remote-2"}
        assert not await store.pending_file_deletions(user_id="user", limit=10)
        await store.complete_deletion("remote-1")
        await store.complete_deletion("remote-2")
        file_pending = await store.pending_file_deletions(user_id="user", limit=10)
        assert [item.file_name for item in file_pending] == ["files/kv-upload"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_deleting_session_queues_complete_remote_chain(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        conversation_id = await _conversation(db)
        store = VideoSessionStore(db)
        await store.create_session(
            handle="video_local",
            conversation_id=conversation_id,
            actor_user_id="user",
            guild_id="guild",
            youtube_url="https://www.youtube.com/watch?v=abcdefghijk",
            youtube_video_id="abcdefghijk",
            model="gemini-3.7-flash",
            interaction_id="remote-1",
            now=10,
            expires_at=100,
        )
        assert await store.advance_session(
            handle="video_local",
            actor_user_id="user",
            expected_interaction_id="remote-1",
            interaction_id="remote-2",
            now=20,
            expires_at=110,
            max_interactions=5,
        )

        assert await store.delete_user_sessions("user") == 1
        pending = await store.pending_deletions(user_id="user", limit=10)
        assert {item.interaction_id for item in pending} == {"remote-1", "remote-2"}

        await store.complete_deletion("remote-1")
        await store.fail_deletion("remote-2", "temporary")
        remaining = await store.pending_deletions(user_id="user", limit=10)
        assert [(item.interaction_id, item.attempts) for item in remaining] == [("remote-2", 1)]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_expiry_queues_remote_deletion(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        conversation_id = await _conversation(db)
        store = VideoSessionStore(db)
        await store.create_session(
            handle="video_expired",
            conversation_id=conversation_id,
            actor_user_id="user",
            guild_id="guild",
            youtube_url="https://www.youtube.com/watch?v=abcdefghijk",
            youtube_video_id="abcdefghijk",
            model="gemini-3.7-flash",
            interaction_id="remote-expired",
            now=10,
            expires_at=100,
        )

        assert await store.delete_expired(101, limit=10) == 1
        pending = await store.pending_deletions(user_id="user", limit=10)
        assert [item.interaction_id for item in pending] == ["remote-expired"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_stale_upload_reservation_queues_file_deletion(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        conversation_id = await _conversation(db)
        store = VideoSessionStore(db)
        await store.reserve_provider_file(
            file_name="files/kv-stale",
            conversation_id=conversation_id,
            actor_user_id="user",
            guild_id="guild",
            mime_type="video/mp4",
            byte_size=5,
            now=10,
        )

        assert await store.delete_stale_provider_files(11, limit=10) == 1
        pending = await store.pending_file_deletions(user_id="user", limit=10)
        assert [item.file_name for item in pending] == ["files/kv-stale"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_failed_deletions_back_off_without_starving_new_rows(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        store = VideoSessionStore(db)
        await store.enqueue_deletion(
            interaction_id="poison",
            actor_user_id="user",
            now=100,
        )
        await store.fail_deletion("poison", "permanent", now=100)
        await store.enqueue_deletion(
            interaction_id="fresh",
            actor_user_id="user",
            now=110,
        )

        pending = await store.pending_deletions(user_id="user", limit=10)
        assert [item.interaction_id for item in pending] == ["fresh", "poison"]
        assert pending[0].retry_at == 110
        assert pending[1].retry_at == 160

        for _ in range(20):
            await store.fail_deletion("poison", "permanent", now=200)
        pending = await store.pending_deletions(user_id="user", limit=10)
        poison = next(item for item in pending if item.interaction_id == "poison")
        assert poison.retry_at == 21_800
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_conversation_retention_cascade_queues_remote_deletion(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        conversation_id = await _conversation(db)
        store = VideoSessionStore(db)
        await store.create_session(
            handle="video_local",
            conversation_id=conversation_id,
            actor_user_id="user",
            guild_id="guild",
            youtube_url="https://www.youtube.com/watch?v=abcdefghijk",
            youtube_video_id="abcdefghijk",
            model="gemini-3.7-flash",
            interaction_id="remote-1",
            now=10,
            expires_at=100,
        )

        async with db.write_transaction() as conn:
            await conn.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
        pending = await store.pending_deletions(user_id="user", limit=10)
        assert [item.interaction_id for item in pending] == ["remote-1"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_video_session_persists_catalog_model(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        conversation_id = await _conversation(db)
        store = VideoSessionStore(db)
        await store.create_session(
            handle="video_pinned",
            conversation_id=conversation_id,
            actor_user_id="user",
            guild_id="guild",
            youtube_url="https://www.youtube.com/watch?v=abcdefghijk",
            youtube_video_id="abcdefghijk",
            catalog_model="gemini-video-flash",
            model="gemini-3.7-flash",
            interaction_id="remote-1",
            now=10,
            expires_at=100,
        )

        found = await store.find_sessions(
            conversation_id=conversation_id,
            actor_user_id="user",
            guild_id="guild",
            now=20,
            handle="video_pinned",
        )
        assert len(found) == 1
        assert found[0].catalog_model == "gemini-video-flash"
        assert found[0].model == "gemini-3.7-flash"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_v5_to_v6_migration_backfills_catalog_model(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "v5.db"
    monkeypatch.setattr("storage.db.SCHEMA_VERSION", 5)

    db = Database(db_path)
    await db.connect()
    conversation_id = await _conversation(db)
    async with db.write_transaction() as conn:
        # The current bootstrap SQL describes v6 even when migrations are
        # capped for this fixture, so remove the new column to construct an
        # actual v5 table before exercising the upgrade.
        await conn.execute("ALTER TABLE video_sessions DROP COLUMN catalog_model")
        async with conn.execute("PRAGMA table_info(video_sessions)") as cursor:
            columns = {row[1] for row in await cursor.fetchall()}
        assert "catalog_model" not in columns
        await conn.execute(
            """
            INSERT INTO video_sessions (
                handle, conversation_id, actor_user_id, guild_id,
                source_kind, source_display_name, source_locator, source_byte_size,
                youtube_url, youtube_video_id, model, latest_interaction_id,
                interaction_count, created_at, last_active_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
            """,
            (
                "video_legacy",
                conversation_id,
                "user",
                "guild",
                "youtube",
                "YouTube video",
                "https://www.youtube.com/watch?v=abcdefghijk",
                None,
                "https://www.youtube.com/watch?v=abcdefghijk",
                "abcdefghijk",
                "gemini-3.7-flash",
                "remote-v5",
                10.0,
                10.0,
                100.0,
            ),
        )
    await db.close()

    monkeypatch.setattr("storage.db.SCHEMA_VERSION", 6)
    db_upgraded = Database(db_path)
    await db_upgraded.connect()
    try:
        store = VideoSessionStore(db_upgraded)
        found = await store.find_sessions(
            conversation_id=conversation_id,
            actor_user_id="user",
            guild_id="guild",
            now=20.0,
            handle="video_legacy",
        )
        assert len(found) == 1
        assert found[0].model == "gemini-3.7-flash"
        assert found[0].catalog_model == "gemini-3.7-flash"
    finally:
        await db_upgraded.close()
