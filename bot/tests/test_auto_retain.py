from __future__ import annotations

import asyncio

import pytest

from memory.auto_retain import AutoRetainFlusher
from memory.privacy import forget_user_memory
from storage.auto_retain import AutoRetainStore
from storage.db import Database
from storage.privacy import PrivacyDeletionRequestStore

NOW = 1_750_000_000.0
IDLE = 30 * 60
HORIZON = 24 * 3600


class FakeMemoryClient:
    def __init__(self, *, retain_ok: bool = True) -> None:
        self.retain_ok = retain_ok
        self.retains: list[dict] = []
        self.deleted_banks: list[str] = []

    async def retain(self, **kwargs) -> bool:
        self.retains.append(kwargs)
        return self.retain_ok

    async def delete_bank(self, bank_id: str) -> bool:
        self.deleted_banks.append(bank_id)
        return True


class FakePreferences:
    def __init__(self) -> None:
        self.disabled: set[str] = set()

    async def is_memory_enabled(self, user_id: str) -> bool:
        return user_id not in self.disabled

    async def set_memory_enabled(self, user_id: str, enabled: bool) -> bool:
        if enabled:
            self.disabled.discard(user_id)
        else:
            self.disabled.add(user_id)
        return True


async def fake_ensure_user_bank(client, discord_id: str, display_name: str) -> str:
    return f"user:{discord_id}"


def make_flusher(
    db: Database,
    memory: FakeMemoryClient,
    prefs: FakePreferences,
    **overrides,
) -> AutoRetainFlusher:
    kwargs: dict = {
        "store": AutoRetainStore(db),
        "preference_store": prefs,
        "memory_client": memory,
        "ensure_user_bank": fake_ensure_user_bank,
        "get_bot_name": lambda: "Kimi",
        "idle_seconds": IDLE,
        "backfill_horizon_seconds": HORIZON,
        "min_user_chars": 20,
        "max_content_chars": 24000,
        "max_flushes_per_sweep": 20,
    }
    kwargs.update(overrides)
    return AutoRetainFlusher(**kwargs)


_discord_id_counter = 100_000


def _next_discord_id() -> str:
    global _discord_id_counter
    _discord_id_counter += 1
    return str(_discord_id_counter)


async def seed_conversation(
    db: Database,
    *,
    key: str,
    last_active_at: float,
    messages: list[tuple[str, str | None, str | None, str]],
    discord_ids: list[str] | None = None,
    guild_id: str | None = "g1",
) -> int:
    """messages: (role, user_id, user_name, content); each gets a snowflake id."""
    conn = db.conn
    cur = await conn.execute(
        "INSERT INTO conversations (key, channel_name, guild_id, channel_id, "
        "created_at, last_active_at) VALUES (?, 'general', ?, 'c1', ?, ?)",
        (key, guild_id, last_active_at - 600, last_active_at),
    )
    conversation_id = cur.lastrowid
    for index, (role, user_id, user_name, content) in enumerate(messages):
        discord_id = discord_ids[index] if discord_ids else _next_discord_id()
        await conn.execute(
            "INSERT INTO messages (conversation_id, role, user_id, user_name, "
            "content, message_data, discord_message_id, source_created_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, '{}', ?, ?, ?)",
            (
                conversation_id,
                role,
                user_id,
                user_name,
                content,
                discord_id,
                last_active_at - 600 + index,
                last_active_at - 600 + index,
            ),
        )
    await conn.commit()
    assert conversation_id is not None
    return conversation_id


@pytest.mark.asyncio
async def test_flush_splits_multi_user_conversation_per_user(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        conv = await seed_conversation(
            db,
            key="root:1",
            last_active_at=NOW - IDLE - 60,
            messages=[
                ("user", "alice", "Alice", "my quest 3 keeps disconnecting from my pc"),
                ("assistant", None, None, "Try a different USB port for the link cable."),
                ("user", "bob", "Bob", "I just bought a bigscreen beyond 2 headset!"),
                ("assistant", None, None, "Nice, enjoy the upgrade Bob!"),
            ],
        )
        memory = FakeMemoryClient()
        flusher = make_flusher(db, memory, FakePreferences())

        stats = await flusher.flush_once(NOW)

        assert stats.retained == 2
        assert stats.failed == 0
        by_bank = {r["bank_id"]: r for r in memory.retains}
        assert set(by_bank) == {"user:alice", "user:bob"}

        alice = by_bank["user:alice"]
        assert "quest 3" in alice["content"]
        assert "bigscreen" not in alice["content"]  # bob's message excluded
        assert "USB port" in alice["content"]  # the reply to alice included
        assert "enjoy the upgrade" not in alice["content"]  # reply to bob excluded
        assert alice["document_id"] == f"auto-retain:alice:{conv}:1"
        assert alice["update_mode"] == "replace"
        assert alice["retain_async"] is False
        assert alice["metadata"]["source_kind"] == "discord_auto_retain"
        assert alice["metadata"]["source_version"] == "1"
        assert alice["metadata"]["subject_user_id"] == "alice"
        assert alice["metadata"]["anchor_message_id"] == "1"
        assert alice["metadata"]["anchor_source_created_at"] == str(NOW - IDLE - 660)

        bob = by_bank["user:bob"]
        assert "bigscreen" in bob["content"]
        assert "quest 3" not in bob["content"]
        assert "enjoy the upgrade" in bob["content"]  # the reply to bob included
        assert "USB port" not in bob["content"]  # reply to alice excluded

        # Second sweep: nothing new, watermarks hold.
        stats = await flusher.flush_once(NOW)
        assert stats.retained == 0
        assert len(memory.retains) == 2
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_flush_reads_live_bot_name(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        await seed_conversation(
            db,
            key="root:live-name",
            last_active_at=NOW - IDLE - 60,
            messages=[
                ("user", "alice", "Alice", "my headset preference is a Quest 3"),
                ("assistant", None, None, "I will keep that preference in mind."),
            ],
        )
        active_name = ["Kimi"]
        memory = FakeMemoryClient()
        flusher = make_flusher(
            db,
            memory,
            FakePreferences(),
            get_bot_name=lambda: active_name[0],
        )

        active_name[0] = "Nova"
        await flusher.flush_once(NOW)

        [retained] = memory.retains
        assert "Nova" in retained["content"]
        assert "Nova (assistant)" in retained["context"]
        assert "Kimi" not in retained["content"]
        assert "Kimi" not in retained["context"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_auto_retain_tags_scope_memory_to_its_guild(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        await seed_conversation(
            db,
            key="root:guild",
            last_active_at=NOW - IDLE - 60,
            messages=[
                ("user", "alice", "Alice", "my quest 3 keeps disconnecting from my pc tower"),
                ("assistant", None, None, "Try a different USB port for the link cable."),
            ],
            guild_id="g1",
        )
        memory = FakeMemoryClient()
        flusher = make_flusher(db, memory, FakePreferences())

        await flusher.flush_once(NOW)

        assert memory.retains, "expected a retain"
        assert memory.retains[0]["tags"] == ["source:auto_retain", "scope:user", "guild:g1"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_auto_retain_guildless_conversation_is_left_unscoped(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        await seed_conversation(
            db,
            key="root:dm",
            last_active_at=NOW - IDLE - 60,
            messages=[
                ("user", "alice", "Alice", "my quest 3 keeps disconnecting from my pc tower"),
                ("assistant", None, None, "Try a different USB port for the link cable."),
            ],
            guild_id=None,
        )
        memory = FakeMemoryClient()
        flusher = make_flusher(db, memory, FakePreferences())

        await flusher.flush_once(NOW)

        assert memory.retains, "expected a retain"
        tags = memory.retains[0]["tags"]
        # No guild tag -> never matched by guild-scoped recall (fail closed).
        assert "scope:user" in tags
        assert not any(tag.startswith("guild:") for tag in tags)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_active_conversation_is_not_flushed(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        await seed_conversation(
            db,
            key="root:1",
            last_active_at=NOW - 60,  # active a minute ago
            messages=[("user", "alice", "Alice", "long enough message about my setup")],
        )
        memory = FakeMemoryClient()
        flusher = make_flusher(db, memory, FakePreferences())
        stats = await flusher.flush_once(NOW)
        assert stats.retained == 0
        assert not memory.retains
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_pending_privacy_deletion_is_excluded_from_candidates(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        await seed_conversation(
            db,
            key="root:pending",
            last_active_at=NOW - IDLE - 60,
            messages=[
                ("user", "alice", "Alice", "my quest 3 keeps disconnecting"),
                ("user", "bob", "Bob", "my index controller keeps drifting"),
            ],
        )
        await PrivacyDeletionRequestStore(db).request(
            user_id="alice",
            scope="memory",
            memory_backend_required=True,
            now=NOW,
        )

        pending = await AutoRetainStore(db).pending(NOW - IDLE)

        assert {item.user_id for item in pending} == {"bob"}
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_opted_out_user_is_skipped_with_watermark_advanced(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        await seed_conversation(
            db,
            key="root:1",
            last_active_at=NOW - IDLE - 60,
            messages=[("user", "alice", "Alice", "my quest 3 keeps disconnecting")],
        )
        memory = FakeMemoryClient()
        prefs = FakePreferences()
        prefs.disabled.add("alice")
        flusher = make_flusher(db, memory, prefs)

        stats = await flusher.flush_once(NOW)
        assert stats.retained == 0
        assert stats.skipped == 1
        assert not memory.retains

        # Re-enable: the opted-out window must not be ingested later.
        prefs.disabled.discard("alice")
        stats = await flusher.flush_once(NOW)
        assert stats.retained == 0
        assert not memory.retains
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_stale_first_sight_conversation_is_fast_forwarded(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        await seed_conversation(
            db,
            key="root:1",
            last_active_at=NOW - HORIZON - 3600,  # idle since before the horizon
            messages=[("user", "alice", "Alice", "ancient history about my old rift s")],
        )
        memory = FakeMemoryClient()
        flusher = make_flusher(db, memory, FakePreferences())

        stats = await flusher.flush_once(NOW)
        assert stats.retained == 0
        assert stats.skipped == 1
        assert not memory.retains

        stats = await flusher.flush_once(NOW)
        assert stats.skipped == 0  # watermark advanced; not revisited
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_trivial_user_content_is_skipped(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        await seed_conversation(
            db,
            key="root:1",
            last_active_at=NOW - IDLE - 60,
            messages=[
                ("user", "alice", "Alice", "hey thanks"),
                ("assistant", None, None, "Anytime!"),
            ],
        )
        memory = FakeMemoryClient()
        flusher = make_flusher(db, memory, FakePreferences())
        stats = await flusher.flush_once(NOW)
        assert stats.retained == 0
        assert stats.skipped == 1
        assert not memory.retains
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_failed_retain_leaves_watermark_for_retry(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        conversation_id = await seed_conversation(
            db,
            key="root:1",
            last_active_at=NOW - IDLE - 60,
            messages=[("user", "alice", "Alice", "my quest 3 keeps disconnecting")],
        )
        memory = FakeMemoryClient(retain_ok=False)
        flusher = make_flusher(db, memory, FakePreferences())

        stats = await flusher.flush_once(NOW)
        assert stats.failed == 1
        first_doc = memory.retains[0]["document_id"]
        assert await AutoRetainStore(db).get_watermark(conversation_id, "alice") is None

        memory.retain_ok = True
        stats = await flusher.flush_once(NOW)
        assert stats.retained == 1
        # Retry covers the same slice under the same document id (replace).
        assert memory.retains[-1]["document_id"] == first_doc
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_reactivated_conversation_flushes_disjoint_second_slice(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        conv = await seed_conversation(
            db,
            key="root:1",
            last_active_at=NOW - IDLE - 60,
            messages=[("user", "alice", "Alice", "my quest 3 keeps disconnecting")],
        )
        memory = FakeMemoryClient()
        flusher = make_flusher(db, memory, FakePreferences())
        await flusher.flush_once(NOW)
        assert len(memory.retains) == 1
        assert memory.retains[0]["document_id"] == f"auto-retain:alice:{conv}:1"

        # Conversation wakes up, then goes idle again.
        await db.conn.execute(
            "INSERT INTO messages (conversation_id, role, user_id, user_name, "
            "content, message_data, discord_message_id, source_created_at, created_at) "
            "VALUES (?, 'user', 'alice', 'Alice', "
            "'turns out it was my usb hub the whole time', '{}', ?, ?, ?)",
            (conv, _next_discord_id(), NOW, NOW),
        )
        await db.conn.commit()

        later = NOW + IDLE + 120
        stats = await flusher.flush_once(later)
        assert stats.retained == 1
        second = memory.retains[-1]
        assert second["document_id"] == f"auto-retain:alice:{conv}:2"
        assert "usb hub" in second["content"]
        assert "disconnecting" not in second["content"]  # first slice not resent
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_oversized_slice_splits_into_part_documents(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        long_a = "a" * 300
        long_b = "b" * 300
        conv = await seed_conversation(
            db,
            key="root:1",
            last_active_at=NOW - IDLE - 60,
            messages=[
                ("user", "alice", "Alice", long_a),
                ("user", "alice", "Alice", long_b),
            ],
        )
        memory = FakeMemoryClient()
        flusher = make_flusher(db, memory, FakePreferences(), max_content_chars=400)

        stats = await flusher.flush_once(NOW)
        assert stats.retained == 1
        assert len(memory.retains) == 2
        assert memory.retains[0]["document_id"] == f"auto-retain:alice:{conv}:1"
        assert memory.retains[1]["document_id"] == f"auto-retain:alice:{conv}:1:p1"
        assert long_a in memory.retains[0]["content"]
        assert long_b in memory.retains[1]["content"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_partial_multi_part_retry_keeps_ids_when_end_moves(tmp_path) -> None:
    class PartialFailureMemory(FakeMemoryClient):
        def __init__(self) -> None:
            super().__init__()
            self.results = iter([True, False])

        async def retain(self, **kwargs) -> bool:
            self.retains.append(kwargs)
            return next(self.results, True)

    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        conv = await seed_conversation(
            db,
            key="root:partial",
            last_active_at=NOW - IDLE - 60,
            messages=[
                ("user", "alice", "Alice", "a" * 300),
                ("user", "alice", "Alice", "b" * 300),
            ],
        )
        memory = PartialFailureMemory()
        flusher = make_flusher(db, memory, FakePreferences(), max_content_chars=400)

        assert (await flusher.flush_once(NOW)).failed == 1
        assert [call["document_id"] for call in memory.retains] == [
            f"auto-retain:alice:{conv}:1",
            f"auto-retain:alice:{conv}:1:p1",
        ]

        await db.conn.execute(
            "INSERT INTO messages (conversation_id, role, user_id, user_name, "
            "content, message_data, discord_message_id, source_created_at, created_at) "
            "VALUES (?, 'user', 'alice', 'Alice', ?, '{}', ?, ?, ?)",
            (conv, "c" * 300, _next_discord_id(), NOW, NOW),
        )
        await db.conn.commit()

        assert (await flusher.flush_once(NOW)).retained == 1
        retry_ids = [call["document_id"] for call in memory.retains[2:]]
        assert retry_ids == [
            f"auto-retain:alice:{conv}:1",
            f"auto-retain:alice:{conv}:1:p1",
            f"auto-retain:alice:{conv}:1:p2",
        ]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_bank_configuration_failure_prevents_auto_retain(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        await seed_conversation(
            db,
            key="root:no-bank",
            last_active_at=NOW - IDLE - 60,
            messages=[("user", "alice", "Alice", "my durable hardware preference")],
        )
        memory = FakeMemoryClient()

        async def fail_bank(client, discord_id: str, display_name: str) -> None:
            return None

        flusher = make_flusher(db, memory, FakePreferences())
        flusher._ensure_user_bank = fail_bank

        assert (await flusher.flush_once(NOW)).failed == 1
        assert memory.retains == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_synthetic_non_snowflake_rows_are_never_flushed(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        # Synthetic user-role rows must never become user memories.
        await seed_conversation(
            db,
            key="synthetic-root",
            last_active_at=NOW - IDLE - 60,
            messages=[
                ("user", "alice", "Alice", "synthetic model-authored prompt"),
                ("assistant", None, None, "Synthetic response content."),
            ],
            discord_ids=["synthetic:7:1750000000", "987654321"],
        )
        memory = FakeMemoryClient()
        flusher = make_flusher(db, memory, FakePreferences())
        stats = await flusher.flush_once(NOW)
        assert stats.retained == 0
        assert not memory.retains
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_forget_me_fast_forwards_watermarks(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        await seed_conversation(
            db,
            key="root:1",
            last_active_at=NOW - IDLE - 60,
            messages=[
                ("user", "alice", "Alice", "my quest 3 keeps disconnecting"),
                ("assistant", None, None, "Try another port."),
            ],
        )
        memory = FakeMemoryClient()
        prefs = FakePreferences()
        store = AutoRetainStore(db)

        result = await forget_user_memory(
            memory_client=memory,
            preference_store=prefs,
            user_id="alice",
            auto_retain_watermarks=store,
        )
        assert result.bank_deleted
        assert memory.deleted_banks == ["user:alice"]

        # Even after re-enabling memory, pre-forget history is never flushed.
        prefs.disabled.discard("alice")
        flusher = make_flusher(db, memory, prefs)
        stats = await flusher.flush_once(NOW)
        assert stats.retained == 0
        assert not memory.retains
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_stale_pending_slice_reloads_fast_forwarded_watermark_inside_guard(
    tmp_path,
) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        conversation_id = await seed_conversation(
            db,
            key="root:stale-pending",
            last_active_at=NOW - IDLE - 60,
            messages=[
                ("user", "alice", "Alice", "my quest 3 keeps disconnecting"),
                ("assistant", None, None, "Try another port."),
            ],
        )
        store = AutoRetainStore(db)
        [stale_item] = await store.pending(NOW - IDLE)

        # Models Delete memory completing while this pending snapshot waits for
        # the per-user mutation guard, followed by the user opting in again.
        assert await store.fast_forward_user("alice") == 1
        memory = FakeMemoryClient()
        flusher = make_flusher(db, memory, FakePreferences())

        outcome = await flusher._flush_slice(stale_item, NOW - HORIZON)

        assert outcome == "skipped"
        assert memory.retains == []
        assert await store.get_watermark(conversation_id, "alice") is not None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_stale_candidate_rechecks_durable_deletion_inside_mutation_guard(
    tmp_path,
) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        conversation_id = await seed_conversation(
            db,
            key="root:durable-delete-race",
            last_active_at=NOW - IDLE - 60,
            messages=[
                ("user", "alice", "Alice", "my quest 3 keeps disconnecting"),
                ("assistant", None, None, "Try another port."),
            ],
        )
        store = AutoRetainStore(db)
        [stale_item] = await store.pending(NOW - IDLE)
        await PrivacyDeletionRequestStore(db).request(
            user_id="alice",
            scope="all",
            memory_backend_required=True,
            now=NOW,
        )
        memory = FakeMemoryClient()
        flusher = make_flusher(db, memory, FakePreferences())

        outcome = await flusher._flush_slice(stale_item, NOW - HORIZON)

        assert outcome == "skipped"
        assert memory.retains == []
        assert await store.get_watermark(conversation_id, "alice") is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_forget_waits_for_inflight_auto_retain_then_deletes_bank(tmp_path) -> None:
    """A confirmed deletion must be the last bank mutation in the race.

    The retain is deliberately suspended after the flusher has passed its
    preference check. Without the shared per-user mutation boundary, forget can
    delete the bank while retain is suspended and the resumed retain recreates it.
    """

    class BlockingMemoryClient(FakeMemoryClient):
        def __init__(self) -> None:
            super().__init__()
            self.retain_started = asyncio.Event()
            self.release_retain = asyncio.Event()
            self.operations: list[str] = []

        async def retain(self, **kwargs) -> bool:
            self.retain_started.set()
            await self.release_retain.wait()
            self.operations.append("retain")
            return await super().retain(**kwargs)

        async def delete_bank(self, bank_id: str) -> bool:
            self.operations.append("delete")
            return await super().delete_bank(bank_id)

    class SignalingPreferences(FakePreferences):
        def __init__(self) -> None:
            super().__init__()
            self.disable_started = asyncio.Event()

        async def set_memory_enabled(self, user_id: str, enabled: bool) -> bool:
            if not enabled:
                self.disable_started.set()
            return await super().set_memory_enabled(user_id, enabled)

    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        await seed_conversation(
            db,
            key="root:forget-race",
            last_active_at=NOW - IDLE - 60,
            messages=[
                ("user", "alice", "Alice", "my quest 3 keeps disconnecting"),
                ("assistant", None, None, "Try another port."),
            ],
        )
        memory = BlockingMemoryClient()
        preferences = SignalingPreferences()
        store = AutoRetainStore(db)
        flusher = make_flusher(db, memory, preferences)

        flush_task = asyncio.create_task(flusher.flush_once(NOW))
        await memory.retain_started.wait()

        forget_task = asyncio.create_task(
            forget_user_memory(
                memory_client=memory,
                preference_store=preferences,
                user_id="alice",
                auto_retain_watermarks=store,
            )
        )
        # Let forget run until it blocks on the flusher's per-user guard. The
        # preference transition must not overtake the suspended retain.
        for _ in range(3):
            await asyncio.sleep(0)
        disable_overtook_retain = preferences.disable_started.is_set()
        forget_finished_early = forget_task.done()

        memory.release_retain.set()
        stats, result = await asyncio.gather(flush_task, forget_task)

        assert not disable_overtook_retain
        assert not forget_finished_early
        assert stats.retained == 1
        assert result.bank_deleted is True
        assert preferences.disabled == {"alice"}
        assert memory.operations == ["retain", "delete"]
        assert memory.deleted_banks == ["user:alice"]
    finally:
        await db.close()
