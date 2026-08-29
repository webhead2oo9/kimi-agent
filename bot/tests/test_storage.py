"""Exercises storage/db.py and its schema-owning stores (conversations,
image_distillations, memory_banks): migrations, rollbacks, and version
mismatches against a real SQLite database. Unrelated to workspace or
sandbox; this is the persistence layer alone.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import stat

import pytest

import storage.db
from storage.conversations import OWNER_ONLY, ChannelMessageRecord, ConversationStore
from storage.db import Database
from storage.image_distillations import ImageDistillationStore
from storage.memory_banks import UserMemoryBankStateStore
from providers.image_caption import format_image_caption
from providers.types import ContentPart, ConversationMessage


@pytest.mark.skipif(os.name == "nt", reason="Windows does not enforce POSIX mode bits")
@pytest.mark.asyncio
async def test_database_and_sidecars_are_owner_only_under_permissive_umask(tmp_path) -> None:
    data_dir = tmp_path / "private-data"
    db_path = data_dir / "bot.db"
    previous_umask = os.umask(0)
    db = Database(db_path)
    try:
        await db.connect()
    finally:
        os.umask(previous_umask)
    try:
        assert stat.S_IMODE(data_dir.stat().st_mode) == 0o700
        for path in (db_path, data_dir / "bot.db-wal", data_dir / "bot.db-shm"):
            assert path.exists()
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
    finally:
        await db.close()


@pytest.mark.skipif(os.name == "nt", reason="Windows does not enforce POSIX mode bits")
@pytest.mark.asyncio
async def test_existing_database_is_tightened_without_changing_parent_mode(tmp_path) -> None:
    data_dir = tmp_path / "shared-data"
    data_dir.mkdir(mode=0o750)
    db_path = data_dir / "bot.db"
    db_path.write_bytes(b"")
    db_path.chmod(0o666)

    db = Database(db_path)
    await db.connect()
    try:
        assert stat.S_IMODE(data_dir.stat().st_mode) == 0o750
        assert stat.S_IMODE(db_path.stat().st_mode) == 0o600
        assert stat.S_IMODE((data_dir / "bot.db-wal").stat().st_mode) == 0o600
        assert stat.S_IMODE((data_dir / "bot.db-shm").stat().st_mode) == 0o600
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_fresh_database_uses_the_current_schema_version(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        async with db.conn.execute("SELECT MAX(version) FROM schema_version") as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row[0] == storage.db.SCHEMA_VERSION
        async with db.conn.execute(
            "SELECT name, applied_at FROM schema_version WHERE version = ?",
            (storage.db.SCHEMA_VERSION,),
        ) as cur:
            version_row = await cur.fetchone()
        assert version_row is not None
        assert version_row["name"] == "provider_circuit_breakers"
        assert version_row["applied_at"]
        assert await UserMemoryBankStateStore(db).may_exist("never-seen") is False
        async with db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'scheduled_tasks'"
        ) as cur:
            assert await cur.fetchone() is None
        async with db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name IN ('config_proposals','control_proposals','control_proposal_events') "
            "ORDER BY name"
        ) as cur:
            assert [row["name"] for row in await cur.fetchall()] == ["config_proposals"]
    finally:
        await db.close()


async def _baseline_database_with_data(path) -> None:
    db = Database(path)
    await db.connect()
    await db.close()
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE migration_test_data (
                id INTEGER PRIMARY KEY,
                value TEXT NOT NULL
            );
            INSERT INTO migration_test_data (id, value) VALUES (1, 'keep me');
            """
        )
        conn.commit()
    finally:
        conn.close()


async def _v1_database_with_coding_task(path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE schema_version (
                version INTEGER PRIMARY KEY,
                name TEXT,
                applied_at TEXT
            );
            INSERT INTO schema_version VALUES (1, 'initial_schema', 'now');

            CREATE TABLE coding_tasks (
                id TEXT PRIMARY KEY,
                conversation_id INTEGER,
                root_key TEXT NOT NULL,
                workspace_key TEXT NOT NULL,
                user_id TEXT NOT NULL,
                user_name TEXT NOT NULL DEFAULT '',
                guild_id TEXT,
                channel_id TEXT NOT NULL,
                thread_id TEXT,
                handoff_pending INTEGER NOT NULL DEFAULT 0
                    CHECK (handoff_pending IN (0, 1)),
                trigger_discord_message_id TEXT NOT NULL DEFAULT '',
                objective TEXT NOT NULL,
                acceptance_criteria_json TEXT NOT NULL DEFAULT '[]',
                context_text TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL CHECK (status IN (
                    'queued','recovering','running','waiting_for_job',
                    'waiting_for_input','cancelling','completed','failed',
                    'cancelled','timed_out'
                )),
                plan_json TEXT NOT NULL DEFAULT '[]',
                milestone TEXT NOT NULL DEFAULT '',
                checkpoint_json TEXT NOT NULL DEFAULT '{}',
                result_text TEXT NOT NULL DEFAULT '',
                error_text TEXT NOT NULL DEFAULT '',
                cancel_requested INTEGER NOT NULL DEFAULT 0
                    CHECK (cancel_requested IN (0, 1)),
                status_discord_message_id TEXT,
                final_discord_message_id TEXT,
                delivery_state TEXT NOT NULL DEFAULT 'pending' CHECK (delivery_state IN (
                    'pending','status_sent','final_pending','delivered','failed'
                )),
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                started_at REAL,
                finished_at REAL,
                deadline_at REAL NOT NULL,
                heartbeat_at REAL NOT NULL
            );
            INSERT INTO coding_tasks (
                id, root_key, workspace_key, user_id, channel_id, objective,
                status, created_at, updated_at, deadline_at, heartbeat_at
            ) VALUES (
                'existing-task', 'root', 'workspace', 'user', 'channel',
                'Keep me', 'queued', 1.0, 1.0, 60.0, 1.0
            );
            """
        )
        conn.commit()
    finally:
        conn.close()


async def _legacy_v2_database_with_control_tables(path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE schema_version (
                version INTEGER PRIMARY KEY,
                name TEXT,
                applied_at TEXT
            );
            INSERT INTO schema_version VALUES (1, 'initial_schema', 'now');
            INSERT INTO schema_version VALUES (2, 'coding_task_context_inputs', 'now');
            CREATE TABLE control_proposals (proposal_id TEXT PRIMARY KEY);
            CREATE TABLE control_proposal_events (event_id INTEGER PRIMARY KEY);
            """
        )
        conn.commit()
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_existing_v2_database_adopts_proposals_and_keeps_legacy_tables(tmp_path) -> None:
    path = tmp_path / "legacy-v2.db"
    await _legacy_v2_database_with_control_tables(path)

    db = Database(path)
    await db.connect()
    try:
        async with db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name IN ('config_proposals','control_proposals','control_proposal_events') "
            "ORDER BY name"
        ) as cur:
            assert [row["name"] for row in await cur.fetchall()] == [
                "config_proposals",
                "control_proposal_events",
                "control_proposals",
            ]
    finally:
        await db.close()


async def _add_note_column(conn) -> None:
    await conn.execute("ALTER TABLE migration_test_data ADD COLUMN note TEXT")


def _register_synthetic_migration(monkeypatch, migrate, *, name: str = "add_note") -> None:
    target = storage.db.SCHEMA_VERSION + 1
    monkeypatch.setattr(storage.db, "SCHEMA_VERSION", target)
    monkeypatch.setattr(
        storage.db, "_MIGRATIONS", {**storage.db._MIGRATIONS, target: (name, migrate)}
    )


@pytest.mark.asyncio
async def test_registered_migration_runs_once_and_preserves_data(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "bot.db"
    await _baseline_database_with_data(db_path)
    _register_synthetic_migration(monkeypatch, _add_note_column)

    db = Database(db_path)
    await db.connect()
    try:
        async with db.conn.execute(
            "SELECT version, name, applied_at FROM schema_version ORDER BY version"
        ) as cur:
            versions = list(await cur.fetchall())
        async with db.conn.execute("SELECT value FROM migration_test_data WHERE id = 1") as cur:
            preserved = await cur.fetchone()
    finally:
        await db.close()

    assert [(row["version"], row["name"]) for row in versions] == [
        (1, "initial_schema"),
        (2, "coding_task_context_inputs"),
        (3, "video_understanding_sessions"),
        (4, "provider_circuit_breakers"),
        (5, "add_note"),
    ]
    assert all(row["applied_at"] for row in versions)
    assert preserved is not None
    assert preserved["value"] == "keep me"

    reopened = Database(db_path)
    await reopened.connect()
    try:
        async with reopened.conn.execute("SELECT COUNT(*) FROM schema_version") as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row[0] == 5
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_fresh_database_records_the_same_history_as_an_upgraded_one(
    tmp_path, monkeypatch
) -> None:
    """Which migrations ran must not depend on how the file was born."""
    upgraded_path = tmp_path / "upgraded.db"
    await _baseline_database_with_data(upgraded_path)
    _register_synthetic_migration(monkeypatch, _add_note_column)

    async def history(path) -> list[tuple[int, str]]:
        db = Database(path)
        await db.connect()
        try:
            async with db.conn.execute(
                "SELECT version, name FROM schema_version ORDER BY version"
            ) as cur:
                return [(row["version"], row["name"]) for row in await cur.fetchall()]
        finally:
            await db.close()

    upgraded_history = await history(upgraded_path)
    assert await history(tmp_path / "fresh.db") == upgraded_history
    assert upgraded_history == [
        (1, "initial_schema"),
        (2, "coding_task_context_inputs"),
        (3, "video_understanding_sessions"),
        (4, "provider_circuit_breakers"),
        (5, "add_note"),
    ]


@pytest.mark.asyncio
async def test_v1_coding_task_migration_preserves_data_and_matches_fresh_schema(tmp_path) -> None:
    upgraded_path = tmp_path / "upgraded.db"
    fresh_path = tmp_path / "fresh.db"
    await _v1_database_with_coding_task(upgraded_path)

    upgraded = Database(upgraded_path)
    await upgraded.connect()
    try:
        async with upgraded.conn.execute("PRAGMA table_info(coding_tasks)") as cur:
            upgraded_columns = [tuple(row) for row in await cur.fetchall()]
        async with upgraded.conn.execute(
            """SELECT objective, display_summary, context_messages_json, input_files_json
               FROM coding_tasks WHERE id = 'existing-task'"""
        ) as cur:
            task = await cur.fetchone()
        async with upgraded.conn.execute(
            "SELECT version, name, applied_at FROM schema_version ORDER BY version"
        ) as cur:
            history = list(await cur.fetchall())
    finally:
        await upgraded.close()

    assert task is not None
    assert tuple(task) == ("Keep me", "", "[]", "[]")
    assert [(row["version"], row["name"]) for row in history] == [
        (1, "initial_schema"),
        (2, "coding_task_context_inputs"),
        (3, "video_understanding_sessions"),
        (4, "provider_circuit_breakers"),
    ]
    assert all(row["applied_at"] for row in history)

    reopened = Database(upgraded_path)
    await reopened.connect()
    await reopened.close()
    conn = sqlite3.connect(upgraded_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM schema_version").fetchone() == (4,)
    finally:
        conn.close()

    fresh = Database(fresh_path)
    await fresh.connect()
    try:
        async with fresh.conn.execute("PRAGMA table_info(coding_tasks)") as cur:
            fresh_columns = [tuple(row) for row in await cur.fetchall()]
    finally:
        await fresh.close()
    assert upgraded_columns == fresh_columns


@pytest.mark.asyncio
async def test_unregistered_migration_reports_the_same_error_on_either_path(
    tmp_path, monkeypatch
) -> None:
    existing_path = tmp_path / "existing.db"
    await _baseline_database_with_data(existing_path)
    monkeypatch.setattr(storage.db, "SCHEMA_VERSION", storage.db.SCHEMA_VERSION + 1)

    # connect() leaves its connection open when it raises, so close each one or the
    # aiosqlite worker thread is stranded.
    for path in (tmp_path / "fresh.db", existing_path):
        db = Database(path)
        try:
            with pytest.raises(RuntimeError, match="No database migration registered"):
                await db.connect()
        finally:
            await db.close()


@pytest.mark.asyncio
async def test_failed_schema_migration_rolls_back(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "bot.db"
    await _baseline_database_with_data(db_path)

    async def fail_after_first_change(conn) -> None:
        await conn.execute("ALTER TABLE migration_test_data ADD COLUMN note TEXT")
        raise RuntimeError("migration failed")

    _register_synthetic_migration(monkeypatch, fail_after_first_change)

    db = Database(db_path)
    with pytest.raises(RuntimeError, match="migration failed"):
        await db.connect()
    await db.close()

    conn = sqlite3.connect(db_path)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(migration_test_data)")}
        version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        preserved = conn.execute("SELECT value FROM migration_test_data WHERE id = 1").fetchone()
    finally:
        conn.close()

    assert columns == {"id", "value"}
    assert version == (4,)
    assert preserved == ("keep me",)


@pytest.mark.asyncio
async def test_image_distillation_cache_is_conversation_scoped_and_cascades(
    tmp_path,
) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        async with db.write_transaction() as conn:
            cursor = await conn.execute(
                "INSERT INTO conversations (key, created_at, last_active_at) VALUES (?, ?, ?)",
                ("guild:channel:root", 1.0, 1.0),
            )
            assert cursor.lastrowid is not None
            conversation_id = cursor.lastrowid

        store = ImageDistillationStore(db)
        await store.set(
            conversation_id,
            "hash",
            model_name="vision-model",
            prompt_version=1,
            description="A red stop sign.",
        )
        assert await store.get(conversation_id, "hash") == (
            "A red stop sign.",
            "vision-model",
        )

        async with db.write_transaction() as conn:
            await conn.execute(
                "DELETE FROM conversations WHERE id = ?",
                (conversation_id,),
            )
        assert await store.get(conversation_id, "hash") is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_encrypted_database_round_trips_and_rejects_plaintext_and_wrong_key(
    tmp_path,
) -> None:
    # sqlcipher3-binary is a Linux-only dependency; skip where it is not installed
    # (e.g. the Windows dev interpreter). The encrypted path is exercised on the
    # Linux deployment/CI interpreter.
    pytest.importorskip("sqlcipher3")
    from sqlcipher3 import dbapi2 as sqlcipher

    db_path = tmp_path / "bot.db"
    key = "test-passphrase-with-'quote-and-spaces"

    db = Database(db_path, encryption_key=key)
    await db.connect()
    try:
        async with db.conn.execute("SELECT MAX(version) FROM schema_version") as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row[0] == storage.db.SCHEMA_VERSION
    finally:
        await db.close()

    # The on-disk file must not be a readable plaintext SQLite database.
    assert db_path.read_bytes()[:16] != b"SQLite format 3\x00"
    with pytest.raises(sqlite3.DatabaseError):
        plain = sqlite3.connect(db_path)
        try:
            plain.execute("SELECT MAX(version) FROM schema_version").fetchall()
        finally:
            plain.close()

    # Reopening with the wrong key must fail; the right key must read back.
    wrong = Database(db_path, encryption_key="not-the-key")
    with pytest.raises(sqlcipher.DatabaseError):
        await wrong.connect()
    await wrong.close()

    reopened = Database(db_path, encryption_key=key)
    await reopened.connect()
    try:
        async with reopened.conn.execute("SELECT MAX(version) FROM schema_version") as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row[0] == storage.db.SCHEMA_VERSION
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_newer_database_version_fails_loudly(tmp_path) -> None:
    db_path = tmp_path / "bot.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE schema_version (
                version INTEGER PRIMARY KEY
            );
            INSERT INTO schema_version (version) VALUES (999);
            """
        )
        conn.commit()
    finally:
        conn.close()

    db = Database(db_path)
    with pytest.raises(RuntimeError, match="newer than supported"):
        await db.connect()
    await db.close()


@pytest.mark.asyncio
async def test_get_or_create_handles_concurrent_first_writes(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    store = ConversationStore(db)

    try:
        conversation_ids = await asyncio.gather(
            store.get_or_create("guild:channel:main", "general"),
            store.get_or_create("guild:channel:main", "general"),
        )
    finally:
        await db.close()

    assert conversation_ids[0] == conversation_ids[1]


@pytest.mark.asyncio
async def test_conversation_activated_tools_round_trips_and_dedupes(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    store = ConversationStore(db)

    try:
        conversation_id = await store.get_or_create("guild:channel:main", "general")

        assert await store.load_activated_tools(conversation_id) == set()

        await store.add_activated_tools(
            conversation_id,
            {"openalex_lookup", "wolfram_alpha", ""},
        )
        await store.add_activated_tools(conversation_id, {"openalex_lookup"})

        activated = await store.load_activated_tools(conversation_id)
    finally:
        await db.close()

    assert activated == {"openalex_lookup", "wolfram_alpha"}


@pytest.mark.asyncio
async def test_concurrent_activation_writes_union_from_stale_baselines(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    store = ConversationStore(db)

    try:
        conversation_id = await store.get_or_create("guild:channel:main", "general")
        first_baseline = await store.load_activated_tools(conversation_id)
        second_baseline = await store.load_activated_tools(conversation_id)

        assert first_baseline == second_baseline == set()

        await store.add_activated_tools(conversation_id, {"openalex_lookup"} - first_baseline)
        await store.add_activated_tools(conversation_id, {"wolfram_alpha"} - second_baseline)

        activated = await store.load_activated_tools(conversation_id)
    finally:
        await db.close()

    assert activated == {"openalex_lookup", "wolfram_alpha"}


@pytest.mark.asyncio
async def test_load_recent_conversation_messages_excludes_tool_turn_internals(
    tmp_path,
) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    store = ConversationStore(db)

    try:
        conversation_id = await store.get_or_create("guild:channel:main", "general")
        await db.conn.executemany(
            "INSERT INTO messages "
            "(conversation_id, role, user_id, user_name, content, message_data, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    conversation_id,
                    "user",
                    "u1",
                    "webhead",
                    "look this up",
                    json.dumps({"role": "user", "content": "look this up"}),
                    1.0,
                ),
                (
                    conversation_id,
                    "assistant",
                    None,
                    None,
                    "",
                    json.dumps(
                        {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "lookup",
                                        "arguments": '{"query": "vr"}',
                                    },
                                }
                            ],
                        }
                    ),
                    2.0,
                ),
                (
                    conversation_id,
                    "tool",
                    None,
                    None,
                    '{"value": "vr"}',
                    json.dumps(
                        {
                            "role": "tool",
                            "tool_call_id": "call_1",
                            "content": '{"value": "vr"}',
                        }
                    ),
                    3.0,
                ),
                (
                    conversation_id,
                    "assistant",
                    None,
                    None,
                    "Here is the answer.",
                    json.dumps(
                        {
                            "role": "assistant",
                            "content": "Here is the answer.",
                            "reasoning_content": "Tool result is enough.",
                        }
                    ),
                    4.0,
                ),
            ],
        )
        await db.conn.commit()

        messages = await store.load_recent_conversation_messages(conversation_id)
    finally:
        await db.close()

    assert messages == [
        ConversationMessage(role="user", content=[ContentPart.from_text("webhead: look this up")]),
        ConversationMessage(
            role="assistant",
            content=[ContentPart.from_text("Here is the answer.")],
        ),
    ]


@pytest.mark.asyncio
async def test_conversation_store_round_trips_content_parts(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    store = ConversationStore(db)

    try:
        conversation_id = await store.get_or_create("guild:channel:main", "general")
        cursor = await db.conn.execute(
            "INSERT INTO messages "
            "(conversation_id, role, user_id, user_name, content, message_data, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                conversation_id,
                "user",
                "u1",
                "webhead",
                "look",
                json.dumps(
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "look"},
                            {
                                "type": "image",
                                "image_url": "data:image/png;base64,abc",
                                "media_type": "image/png",
                            },
                        ],
                    }
                ),
                1.0,
            ),
        )
        await db.conn.commit()

        messages = await store.load_recent_conversation_messages(conversation_id)
    finally:
        await db.close()

    assert cursor.lastrowid == 1
    assert messages[0].content[0].text == "webhead: look"
    assert messages[0].content[1].media_type == "image/png"


@pytest.mark.asyncio
async def test_save_channel_messages_persists_image_parts(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    store = ConversationStore(db)

    try:
        conversation_id = await store.get_or_create("guild:channel:main", "general")
        await store.save_channel_messages(
            conversation_id,
            [
                ChannelMessageRecord(
                    discord_message_id="111",
                    role="user",
                    author_id="u1",
                    author_name="webhead",
                    content="look",
                    content_parts=[
                        ContentPart.from_text("look"),
                        ContentPart.from_image_url(
                            url="data:image/png;base64,abc",
                            media_type="image/png",
                            detail="high",
                        ),
                    ],
                )
            ],
        )

        messages = await store.load_recent_conversation_messages(conversation_id)
    finally:
        await db.close()

    assert messages[0].content[0].text == "webhead: look"
    assert messages[0].content[1].image_url == "data:image/png;base64,abc"
    assert messages[0].content[1].media_type == "image/png"
    assert messages[0].content[1].detail == "high"


@pytest.mark.asyncio
async def test_save_channel_messages_keeps_only_newest_ten_image_parts(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    store = ConversationStore(db)

    try:
        conversation_id = await store.get_or_create("guild:channel:main", "general")
        for index in range(11):
            await store.save_channel_messages(
                conversation_id,
                [
                    ChannelMessageRecord(
                        discord_message_id=str(100 + index),
                        role="user",
                        author_id="u1",
                        author_name="webhead",
                        content=f"look {index}",
                        content_parts=[
                            ContentPart.from_text(f"look {index}"),
                            ContentPart.from_image_url(
                                url=f"data:image/png;base64,img{index}",
                                media_type="image/png",
                            ),
                        ],
                    )
                ],
            )

        messages = await store.load_recent_conversation_messages(conversation_id, limit=20)
    finally:
        await db.close()

    image_urls = [
        part.image_url for message in messages for part in message.content if part.image_url
    ]
    assert len(image_urls) == 10
    assert image_urls == [f"data:image/png;base64,img{index}" for index in range(1, 11)]
    assert messages[0].content == [ContentPart.from_text("webhead: look 0")]


@pytest.mark.asyncio
async def test_image_caption_outlives_the_image_part_it_describes(tmp_path) -> None:
    """The caption is the whole point of the eviction case.

    An image-only message stores an empty text part, which is dropped on read, so once
    the image cap evicts its image the caption is the only part left. It must survive
    that eviction and must not be relabeled as something the user typed.
    """
    caption = format_image_caption("Image 1: a stop sign.")
    db = Database(tmp_path / "bot.db")
    await db.connect()
    store = ConversationStore(db)
    try:
        conversation_id = await store.get_or_create("guild:channel:main", "general")
        for index in range(11):
            await store.save_channel_messages(
                conversation_id,
                [
                    ChannelMessageRecord(
                        discord_message_id=str(200 + index),
                        role="user",
                        author_id="u1",
                        author_name="webhead",
                        content="",
                        content_parts=[
                            ContentPart.from_text(""),
                            ContentPart.from_image_url(
                                url=f"data:image/png;base64,img{index}",
                                media_type="image/png",
                            ),
                            ContentPart.from_text(caption),
                        ],
                    )
                ],
            )

        messages = await store.load_recent_conversation_messages(conversation_id, limit=20)
    finally:
        await db.close()

    assert messages[0].content == [ContentPart.from_text(caption)]
    assert all(part.image_url is None for part in messages[0].content)
    assert messages[-1].content[-1] == ContentPart.from_text(caption)


@pytest.mark.asyncio
async def test_user_label_prefixes_the_user_text_and_never_the_caption(tmp_path) -> None:
    caption = format_image_caption("Image 1: a stop sign.")
    db = Database(tmp_path / "bot.db")
    await db.connect()
    store = ConversationStore(db)
    try:
        conversation_id = await store.get_or_create("guild:channel:main", "general")
        await store.save_channel_messages(
            conversation_id,
            [
                ChannelMessageRecord(
                    discord_message_id="300",
                    role="user",
                    author_id="u1",
                    author_name="webhead",
                    content="look at this",
                    content_parts=[
                        ContentPart.from_text("look at this"),
                        ContentPart.from_text(caption),
                    ],
                )
            ],
        )
        messages = await store.load_recent_conversation_messages(conversation_id, limit=20)
    finally:
        await db.close()

    assert messages[0].content == [
        ContentPart.from_text("webhead: look at this"),
        ContentPart.from_text(caption),
    ]


@pytest.mark.asyncio
async def test_load_recent_conversation_messages_can_stop_before_current_trigger(
    tmp_path,
) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    store = ConversationStore(db)

    try:
        conversation_id = await store.get_or_create("guild:channel:main", "general")
        await store.save_channel_messages(
            conversation_id,
            [
                ChannelMessageRecord(
                    discord_message_id="10",
                    role="user",
                    author_id="u1",
                    author_name="Alice",
                    content="first",
                ),
                ChannelMessageRecord(
                    discord_message_id="11",
                    role="assistant",
                    author_id=None,
                    author_name=None,
                    content="reply",
                ),
                ChannelMessageRecord(
                    discord_message_id="12",
                    role="user",
                    author_id="u2",
                    author_name="Bob",
                    content="current",
                ),
            ],
        )

        messages = await store.load_recent_conversation_messages(
            conversation_id,
            before_discord_message_id="12",
        )
    finally:
        await db.close()

    assert messages == [
        ConversationMessage(role="user", content=[ContentPart.from_text("Alice: first")]),
        ConversationMessage(role="assistant", content=[ContentPart.from_text("reply")]),
    ]
    assert [message.source_discord_message_id for message in messages] == ["10", "11"]


@pytest.mark.asyncio
async def test_messages_table_has_discord_message_id_and_unique_index(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        async with db.conn.execute("PRAGMA table_info(messages)") as cur:
            columns = [row[1] for row in await cur.fetchall()]
        async with db.conn.execute("PRAGMA index_list(messages)") as cur:
            indexes = [row[1] for row in await cur.fetchall()]
    finally:
        await db.close()

    assert "discord_message_id" in columns
    assert "source_created_at" in columns
    assert "idx_messages_conv_discord" in indexes
    # New-user onboarding counts by user_id every turn; it must be indexed.
    assert "idx_messages_user_id" in indexes


@pytest.mark.asyncio
async def test_schema_has_message_contexts_and_conversation_root_metadata(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        async with db.conn.execute("PRAGMA table_info(conversations)") as cur:
            conversation_columns = [row[1] for row in await cur.fetchall()]
        async with db.conn.execute("PRAGMA table_info(message_contexts)") as cur:
            context_columns = [row[1] for row in await cur.fetchall()]
    finally:
        await db.close()

    assert "guild_id" in conversation_columns
    assert "channel_id" in conversation_columns
    assert "thread_id" in conversation_columns
    assert "root_discord_message_id" in conversation_columns
    assert context_columns == [
        "discord_message_id",
        "conversation_id",
        "channel_id",
        "created_at",
    ]


@pytest.mark.asyncio
async def test_message_context_mapping_round_trips_and_is_channel_scoped(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    store = ConversationStore(db)
    try:
        conversation_id = await store.get_or_create(
            "guild:guild-1:channel:chan-1:thread:main:root:111",
            "general",
            guild_id="guild-1",
            channel_id="chan-1",
            thread_id=None,
            root_discord_message_id="111",
        )
        await store.save_channel_messages(
            conversation_id,
            [ChannelMessageRecord("111", "user", "u-alice", "Alice", "root")],
            context_channel_id="chan-1",
        )
        await store.save_channel_messages(
            conversation_id,
            [ChannelMessageRecord("111", "user", "u-alice", "Alice", "root")],
            context_channel_id="chan-1",
        )

        resolved = await store.get_conversation_by_discord_message(
            "111",
            channel_id="chan-1",
        )
        wrong_channel = await store.get_conversation_by_discord_message(
            "111",
            channel_id="chan-2",
        )
        async with db.conn.execute("SELECT COUNT(*) FROM message_contexts") as cur:
            row = await cur.fetchone()
            assert row is not None
            context_count = row[0]
    finally:
        await db.close()

    assert resolved is not None
    assert resolved.id == conversation_id
    assert resolved.key == "guild:guild-1:channel:chan-1:thread:main:root:111"
    assert wrong_channel is None
    assert context_count == 1


@pytest.mark.asyncio
async def test_owner_only_reply_continuation_requires_exact_owner(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    store = ConversationStore(db)
    try:
        conversation_id = await store.get_or_create(
            "userchat:alice:1",
            "general",
            channel_id="chan-1",
            owner_user_id="alice",
            access_scope=OWNER_ONLY,
        )
        with pytest.raises(PermissionError, match="matching owner and scope"):
            await store.get_or_create(
                "userchat:alice:1",
                "general",
                channel_id="chan-1",
                owner_user_id="bob",
                access_scope=OWNER_ONLY,
            )
        # Generic callers must not receive a private conversation id without
        # explicitly restating its matching owner and scope.
        with pytest.raises(PermissionError, match="matching owner and scope"):
            await store.get_or_create(
                "userchat:alice:1",
                "general",
                channel_id="chan-1",
            )
        assert (
            await store.get_or_create(
                "userchat:alice:1",
                "general",
                channel_id="chan-1",
                owner_user_id="alice",
                access_scope=OWNER_ONLY,
            )
            == conversation_id
        )
        await store.save_channel_messages(
            conversation_id,
            [ChannelMessageRecord("reply-1", "assistant", None, None, "public answer")],
            context_channel_id="chan-1",
        )

        owner = await store.get_continuation_conversation_for_reply(
            "reply-1",
            channel_id="chan-1",
            requester_user_id="alice",
        )
        outsider = await store.get_continuation_conversation_for_reply(
            "reply-1",
            channel_id="chan-1",
            requester_user_id="bob",
        )
        empty_requester = await store.get_continuation_conversation_for_reply(
            "reply-1",
            channel_id="chan-1",
            requester_user_id="",
        )
    finally:
        await db.close()

    assert owner is not None and owner.id == conversation_id
    assert owner.owner_user_id == "alice"
    assert owner.access_scope == OWNER_ONLY
    assert outsider is None
    assert empty_requester is None


@pytest.mark.asyncio
async def test_ownerless_owner_only_reply_continuation_fails_closed(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    store = ConversationStore(db)
    try:
        conversation_id = await store.get_or_create(
            "userchat:ownerless:1",
            "general",
            channel_id="chan-1",
            owner_user_id="requester",
            access_scope=OWNER_ONLY,
        )
        await store.save_channel_messages(
            conversation_id,
            [ChannelMessageRecord("reply-1", "assistant", None, None, "public answer")],
            context_channel_id="chan-1",
        )
        async with db.write_transaction() as conn:
            await conn.execute(
                "UPDATE conversations SET owner_user_id = NULL WHERE id = ?",
                (conversation_id,),
            )

        with pytest.raises(PermissionError, match="matching owner and scope"):
            await store.get_or_create(
                "userchat:ownerless:1",
                "general",
                channel_id="chan-1",
                owner_user_id="requester",
                access_scope=OWNER_ONLY,
            )
        async with db.conn.execute(
            "SELECT owner_user_id FROM conversations WHERE id = ?",
            (conversation_id,),
        ) as cur:
            owner_row = await cur.fetchone()
        assert owner_row is not None and owner_row["owner_user_id"] is None

        resolved_null = await store.get_continuation_conversation_for_reply(
            "reply-1",
            channel_id="chan-1",
            requester_user_id="requester",
        )
        async with db.write_transaction() as conn:
            await conn.execute(
                "UPDATE conversations SET owner_user_id = '' WHERE id = ?",
                (conversation_id,),
            )
        resolved_empty = await store.get_continuation_conversation_for_reply(
            "reply-1",
            channel_id="chan-1",
            requester_user_id="",
        )
    finally:
        await db.close()

    assert resolved_null is None
    assert resolved_empty is None


@pytest.mark.asyncio
async def test_same_channel_root_conversations_load_isolated_history(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    store = ConversationStore(db)
    try:
        root_a = await store.get_or_create(
            "guild:guild-1:channel:chan-1:thread:main:root:111",
            "general",
            guild_id="guild-1",
            channel_id="chan-1",
            thread_id=None,
            root_discord_message_id="111",
        )
        root_b = await store.get_or_create(
            "guild:guild-1:channel:chan-1:thread:main:root:222",
            "general",
            guild_id="guild-1",
            channel_id="chan-1",
            thread_id=None,
            root_discord_message_id="222",
        )
        await store.save_channel_messages(
            root_a,
            [
                ChannelMessageRecord("111", "user", "u-a", "UserA", "how do I do A?"),
                ChannelMessageRecord("112", "assistant", None, None, "Use A."),
            ],
            context_channel_id="chan-1",
        )
        await store.save_channel_messages(
            root_b,
            [
                ChannelMessageRecord("222", "user", "u-b", "UserB", "thoughts on LLMs?"),
                ChannelMessageRecord("223", "assistant", None, None, "They are useful."),
            ],
            context_channel_id="chan-1",
        )

        a_history = await store.load_recent_conversation_messages(root_a)
        b_history = await store.load_recent_conversation_messages(root_b)
    finally:
        await db.close()

    assert [part.text for message in a_history for part in message.content if part.text] == [
        "UserA: how do I do A?",
        "Use A.",
    ]
    assert [part.text for message in b_history for part in message.content if part.text] == [
        "UserB: thoughts on LLMs?",
        "They are useful.",
    ]


@pytest.mark.asyncio
async def test_save_channel_messages_dedups_by_discord_id_and_keeps_authors(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    store = ConversationStore(db)
    try:
        conversation_id = await store.get_or_create("guild:channel:main", "general")
        await store.save_channel_messages(
            conversation_id,
            [
                ChannelMessageRecord("111", "user", "u-alice", "Alice", "hi from alice"),
                ChannelMessageRecord("222", "user", "u-bob", "Bob", "hi from bob"),
            ],
        )
        # Re-persisting message 111 (overlapping backfill window) is ignored.
        await store.save_channel_messages(
            conversation_id,
            [ChannelMessageRecord("111", "user", "u-alice", "Alice", "hi from alice")],
        )
        stored = await store.load_recent_stored_messages(conversation_id)
    finally:
        await db.close()

    assert len(stored) == 2
    by_id = {m.user_id: m for m in stored}
    assert by_id["u-alice"].user_name == "Alice"
    assert by_id["u-bob"].user_name == "Bob"
    assert by_id["u-alice"].content == "hi from alice"


@pytest.mark.asyncio
async def test_save_channel_messages_returns_max_message_id(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    store = ConversationStore(db)
    try:
        conversation_id = await store.get_or_create("guild:channel:main", "general")
        first = await store.save_channel_messages(
            conversation_id,
            [ChannelMessageRecord("111", "assistant", None, None, "bot reply")],
        )
        second = await store.save_channel_messages(
            conversation_id,
            [ChannelMessageRecord("222", "user", "u1", "webhead", "thanks")],
        )
    finally:
        await db.close()

    assert first == 1
    assert second == 2


@pytest.mark.asyncio
async def test_channel_message_source_timestamp_and_lookup_round_trip(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    store = ConversationStore(db)
    try:
        conversation_id = await store.get_or_create("guild:channel:main", "general")
        await store.save_channel_messages(
            conversation_id,
            [
                ChannelMessageRecord(
                    "111",
                    "user",
                    "u-alice",
                    "Alice",
                    "hi from alice",
                    source_created_at=101.25,
                ),
                ChannelMessageRecord(
                    "222",
                    "assistant",
                    None,
                    None,
                    "bot reply",
                    source_created_at=102.5,
                ),
            ],
        )

        stored = await store.get_message_by_discord_id(conversation_id, "111")
        recent = await store.load_recent_stored_messages(conversation_id)
    finally:
        await db.close()

    assert stored is not None
    assert stored.content == "hi from alice"
    assert stored.source_created_at == pytest.approx(101.25)
    assert [m.source_created_at for m in recent] == [pytest.approx(101.25), pytest.approx(102.5)]


@pytest.mark.asyncio
async def test_load_message_window_returns_chronological_before_anchor_after(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    store = ConversationStore(db)
    try:
        conversation_id = await store.get_or_create("guild:channel:main", "general")
        await store.save_channel_messages(
            conversation_id,
            [
                ChannelMessageRecord(
                    "111", "user", "u1", "webhead", "first", source_created_at=1.0
                ),
                ChannelMessageRecord(
                    "222", "assistant", None, None, "second", source_created_at=2.0
                ),
                ChannelMessageRecord(
                    "333", "user", "u1", "webhead", "third", source_created_at=3.0
                ),
                ChannelMessageRecord("444", "user", "u2", "Dana", "fourth", source_created_at=4.0),
                ChannelMessageRecord(
                    "555", "assistant", None, None, "fifth", source_created_at=5.0
                ),
            ],
        )
        anchor = await store.get_message_by_discord_id(conversation_id, "333")
        assert anchor is not None

        window = await store.load_message_window(
            conversation_id,
            anchor.id,
            before=2,
            after=2,
        )
    finally:
        await db.close()

    assert [message.content for message in window] == [
        "first",
        "second",
        "third",
        "fourth",
        "fifth",
    ]


@pytest.mark.asyncio
async def test_reply_continuation_resolves_only_bot_messages(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    store = ConversationStore(db)
    try:
        conversation_id = await store.get_or_create(
            "guild:guild-1:channel:chan-1:thread:main:root:111",
            "general",
            guild_id="guild-1",
            channel_id="chan-1",
            thread_id=None,
            root_discord_message_id="111",
            owner_user_id="u-alice",
        )
        # Human trigger message (the @mention that started the turn).
        await store.save_channel_messages(
            conversation_id,
            [ChannelMessageRecord("111", "user", "u-alice", "Alice", "hello bot")],
            context_channel_id="chan-1",
        )
        # The bot's own reply.
        await store.save_channel_messages(
            conversation_id,
            [ChannelMessageRecord("222", "assistant", None, None, "hi alice")],
            context_channel_id="chan-1",
        )
        # The bot's narration/activity message, mapped without a transcript row.
        await store.map_message_context("333", conversation_id, "chan-1")

        # Replying to the human's message must NOT continue the conversation.
        human_reply = await store.get_continuation_conversation_for_reply(
            "111", channel_id="chan-1", requester_user_id="u-bob"
        )
        # Replying to the bot's reply continues it.
        bot_reply = await store.get_continuation_conversation_for_reply(
            "222", channel_id="chan-1", requester_user_id="u-bob"
        )
        # Replying to the bot's narration message continues it.
        narration_reply = await store.get_continuation_conversation_for_reply(
            "333", channel_id="chan-1", requester_user_id="u-bob"
        )
        # A reply in a different channel never resolves.
        wrong_channel = await store.get_continuation_conversation_for_reply(
            "222", channel_id="chan-2", requester_user_id="u-bob"
        )
    finally:
        await db.close()

    assert human_reply is None
    assert bot_reply is not None and bot_reply.id == conversation_id
    assert narration_reply is not None and narration_reply.id == conversation_id
    assert wrong_channel is None


@pytest.mark.asyncio
async def test_map_message_context_routes_without_transcript_row(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    store = ConversationStore(db)
    try:
        conversation_id = await store.get_or_create("guild:channel:main", "general")

        await store.map_message_context("777", conversation_id, "100")

        record = await store.get_conversation_by_discord_message("777", channel_id="100")
        assert record is not None
        assert record.id == conversation_id

        messages = await store.load_recent_conversation_messages(conversation_id)
        assert messages == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_write_transaction_rolls_back_partial_unit_on_error(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        store = ConversationStore(db)
        conv_id = await store.get_or_create("guild:1:channel:2:thread:main:root:3")

        with pytest.raises(RuntimeError, match="mid-unit"):
            async with db.write_transaction() as conn:
                await conn.execute(
                    "INSERT INTO messages (conversation_id, role, content, "
                    "message_data, discord_message_id, source_created_at, created_at) "
                    "VALUES (?, 'assistant', 'partial', '{}', '42', 0, 0)",
                    (conv_id,),
                )
                raise RuntimeError("mid-unit failure")

        # An unrelated commit must not be able to finalize the aborted unit.
        await db.conn.commit()
        async with db.conn.execute(
            "SELECT COUNT(*) FROM messages WHERE conversation_id = ?", (conv_id,)
        ) as cur:
            row = await cur.fetchone()
        assert row is not None and row[0] == 0
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_write_transaction_serializes_concurrent_units(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        active = 0
        overlapped = False

        async def unit() -> None:
            nonlocal active, overlapped
            async with db.write_transaction():
                active += 1
                if active > 1:
                    overlapped = True
                await asyncio.sleep(0)
                active -= 1

        await asyncio.gather(unit(), unit(), unit())
        assert overlapped is False
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_count_user_messages_across_conversations_and_excludes_trigger(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    store = ConversationStore(db)
    try:
        c1 = await store.get_or_create("g:c:a", "a")
        c2 = await store.get_or_create("g:c:b", "b")
        await db.conn.executemany(
            "INSERT INTO messages (conversation_id, role, user_id, user_name, content, "
            "message_data, discord_message_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (c1, "user", "u1", "webhead", "hi", "{}", "m1", 1.0),
                (c2, "user", "u1", "webhead", "again", "{}", "m2", 2.0),
                (c1, "assistant", None, None, "", "{}", "b1", 3.0),  # bot msg (user_id NULL)
                (c1, "user", "u2", "Other", "yo", "{}", "m3", 4.0),
            ],
        )
        await db.conn.commit()

        # Counts the user across all conversations; bot/other-user rows excluded.
        assert await store.count_user_messages("u1") == 2
        # Excluding a message id drops it from the count.
        assert await store.count_user_messages("u1", exclude_discord_message_id="m2") == 1
        assert await store.count_user_messages("u2") == 1
        assert await store.count_user_messages("nobody") == 0
        # limit caps the result (and the scan) at min(actual, limit).
        assert await store.count_user_messages("u1", limit=1) == 1
        assert await store.count_user_messages("u1", limit=5) == 2
    finally:
        await db.close()


async def _set_last_active(db: Database, conversation_id: int, when: float) -> None:
    async with db.write_transaction() as conn:
        await conn.execute(
            "UPDATE conversations SET last_active_at = ? WHERE id = ?",
            (when, conversation_id),
        )


@pytest.mark.asyncio
async def test_delete_conversations_older_than_purges_unit_and_keeps_recent(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    store = ConversationStore(db)
    try:
        old = await store.get_or_create("g:c:old", "old", channel_id="c")
        recent = await store.get_or_create("g:c:new", "new", channel_id="c")

        # Each conversation gets a transcript row + every CASCADE child: a
        # message_contexts mapping, a thread enrollment, an activated tool, and an
        # auto-retain watermark. The old one must lose all of them; the recent one
        # must keep all of them.
        for cid, dmid in ((old, "m-old"), (recent, "m-new")):
            await store.save_channel_messages(
                cid,
                [ChannelMessageRecord(dmid, "user", "u1", "Alice", "hello")],
                context_channel_id="c",
            )
            await store.map_thread_conversation(f"thread-{cid}", cid, creator_user_id="alice")
            await store.add_activated_tools(cid, {"internet_search"})
            async with db.write_transaction() as conn:
                await conn.execute(
                    "INSERT INTO auto_retain_watermarks "
                    "(conversation_id, user_id, last_retained_message_id, updated_at) "
                    "VALUES (?, ?, ?, ?)",
                    (cid, "u1", 1, 0.0),
                )

        # Usage-ledger rows are not tied to a conversation; the transcript sweep
        # must leave both kinds of cost accounting untouched.
        async with db.write_transaction() as conn:
            await conn.execute(
                "INSERT INTO usage_ledger (user_id, model, role, created_at) VALUES (?, ?, ?, ?)",
                ("u1", "m", "chat", "2026-01-01"),
            )
            await conn.execute(
                "INSERT INTO paid_usage_ledger "
                "(user_id, tool_name, provider, cost_usd, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                ("u1", "internet_search", "exa", 0.01, "2026-01-01"),
            )

        await _set_last_active(db, old, 100.0)
        await _set_last_active(db, recent, 10_000.0)

        removed = await store.delete_conversations_older_than(1_000.0)
        assert removed == 1

        async def scalar(sql: str, *params: object) -> int:
            async with db.conn.execute(sql, params) as cur:
                row = await cur.fetchone()
            return int(row[0]) if row and row[0] is not None else 0

        # Old conversation and its whole subtree are gone.
        assert await scalar("SELECT COUNT(*) FROM conversations WHERE id = ?", old) == 0
        assert await scalar("SELECT COUNT(*) FROM messages WHERE conversation_id = ?", old) == 0
        assert (
            await scalar("SELECT COUNT(*) FROM message_contexts WHERE conversation_id = ?", old)
            == 0
        )
        assert (
            await scalar("SELECT COUNT(*) FROM thread_conversations WHERE conversation_id = ?", old)
            == 0
        )
        assert (
            await scalar(
                "SELECT COUNT(*) FROM conversation_activated_tools WHERE conversation_id = ?", old
            )
            == 0
        )
        assert (
            await scalar(
                "SELECT COUNT(*) FROM auto_retain_watermarks WHERE conversation_id = ?", old
            )
            == 0
        )

        # Recent conversation and its subtree are untouched.
        assert await scalar("SELECT COUNT(*) FROM conversations WHERE id = ?", recent) == 1
        assert await scalar("SELECT COUNT(*) FROM messages WHERE conversation_id = ?", recent) == 1
        assert (
            await scalar("SELECT COUNT(*) FROM message_contexts WHERE conversation_id = ?", recent)
            == 1
        )
        assert (
            await scalar(
                "SELECT COUNT(*) FROM thread_conversations WHERE conversation_id = ?", recent
            )
            == 1
        )
        assert (
            await scalar(
                "SELECT COUNT(*) FROM auto_retain_watermarks WHERE conversation_id = ?", recent
            )
            == 1
        )

        # Cost ledgers are left alone.
        assert await scalar("SELECT COUNT(*) FROM usage_ledger") == 1
        assert await scalar("SELECT COUNT(*) FROM paid_usage_ledger") == 1

        # Nothing left to purge → no-op returns 0.
        assert await store.delete_conversations_older_than(1_000.0) == 0
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_delete_user_data_drops_rooted_and_scrubs_shared(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    store = ConversationStore(db)
    try:
        # Conversation Alice rooted (her mention started it), with a reply from Bob
        # and the bot. Deleting Alice's data removes the whole conversation.
        alice_root = await store.get_or_create(
            "g:c:alice", "alice", channel_id="c", root_discord_message_id="m-alice"
        )
        await store.save_channel_messages(
            alice_root,
            [
                ChannelMessageRecord("m-alice", "user", "alice", "Alice", "hey kimi"),
                ChannelMessageRecord("m-bob-reply", "user", "bob", "Bob", "me too"),
                ChannelMessageRecord("m-bot-1", "assistant", None, None, "hi all"),
            ],
            context_channel_id="c",
        )

        # Conversation Bob rooted, where Alice also chimed in. Deleting Alice's data
        # must leave the conversation and Bob's/the bot's rows intact, scrubbing only
        # Alice's own message.
        bob_root = await store.get_or_create(
            "g:c:bob", "bob", channel_id="c", root_discord_message_id="m-bob"
        )
        await store.save_channel_messages(
            bob_root,
            [
                ChannelMessageRecord("m-bob", "user", "bob", "Bob", "kimi help"),
                ChannelMessageRecord("m-alice-chime", "user", "alice", "Alice", "+1"),
                ChannelMessageRecord("m-bot-2", "assistant", None, None, "sure"),
            ],
            context_channel_id="c",
        )

        # Alice also requested a managed thread inside Carol's shared root but
        # has no transcript row there. The creator marker is still Alice's data
        # and must participate in the privacy barrier/deletion.
        carol_root = await store.get_or_create(
            "g:c:carol", "carol", channel_id="c", root_discord_message_id="m-carol"
        )
        await store.save_channel_messages(
            carol_root,
            [ChannelMessageRecord("m-carol", "user", "carol", "Carol", "kimi help")],
            context_channel_id="c",
        )
        await store.map_thread_conversation("thread-carol", carol_root, creator_user_id="alice")
        distillations = ImageDistillationStore(db)
        for conversation_id, label in (
            (alice_root, "alice-root"),
            (bob_root, "shared-with-alice"),
            (carol_root, "unrelated-to-alice-messages"),
        ):
            await distillations.set(
                conversation_id,
                "images",
                model_name="vision",
                prompt_version=1,
                description=label,
            )

        assert await store.list_user_conversation_keys("alice") == [
            "g:c:alice",
            "g:c:bob",
            "g:c:carol",
        ]
        result = await store.delete_user_data("alice")
        assert result.conversations_deleted == 1
        assert result.messages_scrubbed == 1  # only Alice's row in Bob's conversation

        async def scalar(sql: str, *params: object) -> int:
            async with db.conn.execute(sql, params) as cur:
                row = await cur.fetchone()
            return int(row[0]) if row and row[0] is not None else 0

        # Alice's rooted conversation and every message in it (Bob's, the bot's) gone.
        assert await scalar("SELECT COUNT(*) FROM conversations WHERE id = ?", alice_root) == 0
        assert (
            await scalar("SELECT COUNT(*) FROM messages WHERE conversation_id = ?", alice_root) == 0
        )

        # Bob's conversation survives; only Alice's message was scrubbed.
        assert await scalar("SELECT COUNT(*) FROM conversations WHERE id = ?", bob_root) == 1
        assert (
            await scalar(
                "SELECT COUNT(*) FROM messages WHERE conversation_id = ? AND user_id = ?",
                bob_root,
                "alice",
            )
            == 0
        )
        assert (
            await scalar(
                "SELECT COUNT(*) FROM messages WHERE conversation_id = ? AND user_id = ?",
                bob_root,
                "bob",
            )
            == 1
        )
        # Aggregate image descriptions may contain Alice's image content. Both her
        # deleted root and Bob's surviving shared root are invalidated; Carol's
        # cache survives because Alice had no transcript message there.
        assert await distillations.get(alice_root, "images") is None
        assert await distillations.get(bob_root, "images") is None
        assert await distillations.get(carol_root, "images") == (
            "unrelated-to-alice-messages",
            "vision",
        )
        # No Alice rows remain anywhere.
        assert await scalar("SELECT COUNT(*) FROM messages WHERE user_id = ?", "alice") == 0
        assert (
            await scalar(
                "SELECT COUNT(*) FROM message_contexts WHERE discord_message_id = ?",
                "m-alice-chime",
            )
            == 0
        )
        assert (
            await store.get_continuation_conversation_for_reply(
                "m-alice-chime",
                channel_id="c",
                requester_user_id="alice",
            )
            is None
        )
        assert await scalar("SELECT COUNT(*) FROM conversations WHERE id = ?", carol_root) == 1
        assert await store.get_thread_conversation("thread-carol") is not None
        assert await store.get_thread_creator_user_id("thread-carol") is None

        # Idempotent: a second run finds nothing.
        again = await store.delete_user_data("alice")
        assert again.conversations_deleted == 0
        assert again.messages_scrubbed == 0
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_delete_user_data_drops_owned_assistant_only_conversation(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    store = ConversationStore(db)
    try:
        conversation_id = await store.get_or_create(
            "root:timeout",
            "general",
            channel_id="c",
            root_discord_message_id="m-timeout",
            owner_user_id="alice",
        )
        await store.save_channel_messages(
            conversation_id,
            [
                ChannelMessageRecord(
                    "m-timeout:reply",
                    "assistant",
                    None,
                    None,
                    "The request timed out.",
                )
            ],
            context_channel_id="c",
        )

        assert await store.list_user_conversation_keys("alice") == ["root:timeout"]
        result = await store.delete_user_data("alice")
        assert result.conversations_deleted == 1
        assert result.messages_scrubbed == 0
        async with db.conn.execute(
            "SELECT COUNT(*) FROM conversations WHERE id = ?",
            (conversation_id,),
        ) as cur:
            row = await cur.fetchone()
        assert row is not None and row[0] == 0
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_touch_protects_resolved_conversation_from_retention(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    store = ConversationStore(db)
    try:
        conversation_id = await store.get_or_create("g:c:active", "active")
        await _set_last_active(db, conversation_id, 100.0)

        assert await store.touch(conversation_id) is True
        assert await store.delete_conversations_older_than(1_000.0) == 0
        async with db.conn.execute(
            "SELECT 1 FROM conversations WHERE id = ?",
            (conversation_id,),
        ) as cur:
            assert await cur.fetchone() is not None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_delete_conversations_older_than_batch_limit(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    store = ConversationStore(db)
    try:
        ids = [await store.get_or_create(f"g:c:{i}", str(i)) for i in range(5)]
        for cid in ids:
            await _set_last_active(db, cid, 100.0)

        # One batch deletes at most `limit`; the oldest go first.
        assert await store.delete_conversations_older_than(1_000.0, limit=2) == 2
        assert await store.delete_conversations_older_than(1_000.0, limit=2) == 2
        assert await store.delete_conversations_older_than(1_000.0, limit=2) == 1
        assert await store.delete_conversations_older_than(1_000.0, limit=2) == 0
    finally:
        await db.close()
