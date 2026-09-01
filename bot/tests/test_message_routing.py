"""Exercises app/runtime.py's handle_message: routing an incoming Discord
message to the right conversation, thread parent, and memory recall before
a turn is ever run. This is the dispatch layer, not the conversation loop
itself; see test_core_smoke.py for that.
"""

import asyncio
from collections.abc import AsyncIterator, Mapping
from datetime import datetime, UTC
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
import pytest_asyncio

from agent.context import ContextManager
from agent.core import ConversationRunRequest, ConversationRunResult
from app import runtime as app_runtime
from app import thread_handoff_boundary as thread_boundary
from app.admission import TURN_ADMISSION_BUSY_MESSAGE, TurnAdmissionController
from app.conversation_routing import ResolvedConversation
from config.fragments.tool_policy import THREAD_STATE_TOOLS
from app.threads import ThreadHandoffManager
from agent.turn import TurnResult, handle_turn
from tools.registry import TurnHandoff
from tools.threads import ThreadCloseRequest, ThreadRequest
from config.model_config import ModelConfig
from config.settings import Settings
from providers.types import ContentPart
from storage.conversations import (
    CHANNEL_SHARED,
    OWNER_ONLY,
    ChannelMessageRecord,
    ConversationStore,
)
from storage.db import Database
from tests.helpers import (
    LifecycleProbe,
    NobodyBlocked,
    RootLockProbe,
    StubProviderManager,
    install_foreground_turn_handler,
    remove_processing_reaction,
)


def test_user_memory_recall_types_parses_comma_separated_values():
    s = Settings(
        memory_recall_types="world, experience, observation",
        _env_file=None,
    )
    assert s.user_memory_recall_types == ["world", "experience", "observation"]


def _conversation_call_kwargs(kwargs: Mapping[str, Any]) -> dict[str, Any]:
    request = kwargs.get("request")
    if request is None:
        return dict(kwargs)
    assert isinstance(request, ConversationRunRequest)
    return request.__dict__


def _run_conversation_mock() -> AsyncMock:
    """The AsyncMock _build_test_app patched over app_runtime.run_conversation."""

    mock = app_runtime.run_conversation
    assert isinstance(mock, AsyncMock)
    return mock


def _last_run_conversation_kwargs() -> dict[str, Any]:
    last_call = _run_conversation_mock().await_args
    assert last_call is not None, "run_conversation was never awaited"
    return _conversation_call_kwargs(last_call.kwargs)


class EmptyPersonaPreferenceStore:
    async def get_persona(self, user_id: str) -> str:
        _ = user_id
        return ""


@pytest_asyncio.fixture
async def routing_database(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database(tmp_path / "routing.db")
    await database.connect()
    try:
        yield database
    finally:
        await database.close()


async def _conversation_access(
    database: Database,
    conversation_id: int,
) -> tuple[str | None, str] | None:
    async with database.conn.execute(
        "SELECT owner_user_id, access_scope FROM conversations WHERE id = ?",
        (conversation_id,),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return None
    return row["owner_user_id"], str(row["access_scope"])


async def _conversation_keys(database: Database) -> set[str]:
    async with database.conn.execute("SELECT key FROM conversations ORDER BY key") as cursor:
        return {str(row["key"]) for row in await cursor.fetchall()}


async def _conversation_id_for_key(database: Database, key: str) -> int | None:
    async with database.conn.execute(
        "SELECT id FROM conversations WHERE key = ?",
        (key,),
    ) as cursor:
        row = await cursor.fetchone()
    return int(row["id"]) if row is not None else None


async def _transcript_discord_ids(database: Database, *, role: str | None = None) -> set[str]:
    sql = "SELECT discord_message_id FROM messages WHERE discord_message_id IS NOT NULL"
    params: tuple[str, ...] = ()
    if role is not None:
        sql += " AND role = ?"
        params = (role,)
    async with database.conn.execute(sql, params) as cursor:
        return {str(row["discord_message_id"]) for row in await cursor.fetchall()}


def _build_test_app(monkeypatch):
    monkeypatch.setattr(
        app_runtime,
        "build_provider_manager",
        lambda settings: StubProviderManager(settings),
    )
    # Keep tests hermetic and explicitly activate the synthetic guild. Guilds
    # with no saved setup fail closed.
    app = app_runtime.build_app(
        Settings(
            _env_file=None,
            hindsight_url="",
            model_api_key="test-key",
            allowed_guild_ids="999",
            moderation_enabled=False,
        )
    )
    app.gateway_ready = True
    # The runtime's block gate raises on an uninitialised store rather than
    # guessing; these tests bypass _first_init_core, so give it a real answer.
    app.blocked_user_store = NobodyBlocked()  # type: ignore[assignment]
    # These guild-routing fixtures likewise stop before store-backed subsystem
    # construction. Stop behavior is covered directly by test_work_cancellation.
    app.work_cancellation = cast(
        Any,
        SimpleNamespace(is_stop_message=lambda _message: False),
    )
    return app


def _install_foreground_runner(app, store: ConversationStore) -> None:
    app.context_manager = ContextManager(store)
    app.conversation_store = store
    install_foreground_turn_handler(app, handle_turn)


@pytest.mark.asyncio
async def test_new_message_root_records_owner_before_transcript_persistence(
    routing_database: Database,
) -> None:
    store = ConversationStore(routing_database)
    message = _trigger_message(
        content="<@999> hello",
        author_id=123,
        author_name="Alice",
        message_id=222,
    )

    resolved = await app_runtime.resolve_conversation_for_message(
        message,
        allow_new_root=True,
        conversation_store=store,
        thread_handoff=None,
    )

    assert resolved is not None and resolved.db_conversation_id is not None
    assert await _conversation_access(routing_database, resolved.db_conversation_id) == (
        "123",
        CHANNEL_SHARED,
    )
    assert await _transcript_discord_ids(routing_database) == set()


def _text_message(
    *,
    channel_id: int = 100,
    content: str = "hello",
    author_name: str = "Alice",
    author_bot: bool = False,
    message_type=discord.MessageType.default,
    parent_channel_id: int | None = None,
):
    if parent_channel_id is None:
        channel = MagicMock(spec=discord.TextChannel)
    else:
        # spec= makes isinstance(channel, discord.Thread) true, which is what
        # every parent-channel lookup in the turn path branches on.
        channel = MagicMock(spec=discord.Thread)
        channel.parent_id = parent_channel_id
    channel.id = channel_id
    channel.name = "general"

    guild = MagicMock()
    guild.id = 999

    author = MagicMock()
    author.id = 123
    author.display_name = author_name
    author.bot = author_bot

    message = MagicMock()
    message.channel = channel
    message.guild = guild
    message.author = author
    message.content = content
    message.type = message_type
    message.id = 555
    message.created_at = datetime.fromtimestamp(message.id, tz=UTC)
    message.add_reaction = AsyncMock()
    message.remove_reaction = AsyncMock()
    return message


async def _capture_conversation_call(monkeypatch, app, message, store: ConversationStore) -> dict:
    """Run one mention-path turn and return the ConversationRunRequest fields."""
    from agent.attachments import TurnImages

    app.preference_store = EmptyPersonaPreferenceStore()
    _install_foreground_runner(app, store)
    monkeypatch.setattr(
        app_runtime,
        "collect_turn_images",
        AsyncMock(return_value=TurnImages(vision_parts=[], edit_target=None)),
    )
    monkeypatch.setattr(app, "send_response", AsyncMock(return_value=[]))

    captured: dict = {}

    async def fake_run_conversation(**kwargs):
        captured.update(_conversation_call_kwargs(kwargs))
        return ConversationRunResult(text="ok")

    monkeypatch.setattr(app_runtime, "run_conversation", fake_run_conversation)
    await app.handle_message(message, lock_acquired=True)
    return captured


@pytest.mark.asyncio
async def test_handle_message_resolves_the_thread_parent_for_instructions(
    monkeypatch, routing_database: Database
):
    """The first hop of the thread-instructions chain, on the path that matters.

    A mention inside a thread must carry the *parent* channel id so operator
    instruction fragments resolve against the channel the thread hangs off.
    Losing that field silently resolves against the thread id and leaves the
    <channel_instructions> slot empty.
    """
    app = _build_test_app(monkeypatch)
    captured = await _capture_conversation_call(
        monkeypatch,
        app,
        _text_message(channel_id=77, parent_channel_id=20),
        ConversationStore(routing_database),
    )

    # channel_id stays the thread's own id (its full-template rung precedes the parent).
    assert captured["channel_id"] == "77"
    assert captured["thread_id"] == "77"
    assert captured["parent_channel_id"] == "20"


@pytest.mark.asyncio
async def test_handle_message_outside_a_thread_has_no_parent_to_resolve(
    monkeypatch, routing_database: Database
):
    app = _build_test_app(monkeypatch)
    captured = await _capture_conversation_call(
        monkeypatch,
        app,
        _text_message(channel_id=100),
        ConversationStore(routing_database),
    )

    assert captured["channel_id"] == "100"
    assert captured["thread_id"] is None
    # A plain channel is its own parent, so the two agree and nothing changes.
    assert captured["parent_channel_id"] == "100"


@pytest.mark.asyncio
async def test_handle_message_passes_recalled_memories_to_conversation(
    monkeypatch, routing_database: Database
):
    app = _build_test_app(monkeypatch)
    app.memory_manager.client = object()
    app.memory_manager.ready = True
    app.preference_store = EmptyPersonaPreferenceStore()
    _install_foreground_runner(app, ConversationStore(routing_database))
    ensure_user_bank = AsyncMock()
    monkeypatch.setattr(app_runtime, "ensure_user_bank", ensure_user_bank)
    from agent.attachments import TurnImages

    collect_images = AsyncMock(return_value=TurnImages(vision_parts=[], edit_target=None))
    monkeypatch.setattr(
        app_runtime,
        "collect_turn_images",
        collect_images,
    )
    recall = AsyncMock(return_value="- webhead uses a Quest 3. [world]")
    monkeypatch.setattr(
        app_runtime,
        "recall_current_user_context",
        recall,
    )
    monkeypatch.setattr(app, "send_response", AsyncMock(return_value=[]))

    captured: dict = {}

    async def fake_run_conversation(**kwargs):
        captured.update(_conversation_call_kwargs(kwargs))
        return ConversationRunResult(text="ok")

    monkeypatch.setattr(app_runtime, "run_conversation", fake_run_conversation)

    message = _text_message(content="what did I say about my headset?")

    await app.handle_message(message, lock_acquired=True)

    recall.assert_awaited_once()
    assert recall.await_args is not None
    recall_kwargs = recall.await_args.kwargs
    assert recall_kwargs["memory_client"] is app.memory_manager.client
    assert recall_kwargs["preference_store"] is app.preference_store
    assert recall_kwargs["user_id"] == "123"
    assert recall_kwargs["user_message"] == "what did I say about my headset?"
    assert recall_kwargs["types"] == app.settings.user_memory_recall_types
    assert captured["recalled_memories"] == "- webhead uses a Quest 3. [world]"


@pytest.mark.asyncio
async def test_trigger_newlines_are_neutralized_for_model_input(
    monkeypatch, routing_database: Database
):
    app = _build_test_app(monkeypatch)
    app.memory_manager.client = None
    app.preference_store = None
    _install_foreground_runner(app, ConversationStore(routing_database))
    from agent.attachments import TurnImages

    monkeypatch.setattr(
        app_runtime,
        "collect_turn_images",
        AsyncMock(return_value=TurnImages(vision_parts=[], edit_target=None)),
    )
    monkeypatch.setattr(app_runtime, "recall_current_user_context", AsyncMock(return_value=""))
    monkeypatch.setattr(app, "send_response", AsyncMock(return_value=[]))

    captured: dict = {}

    async def fake_run_conversation(**kwargs):
        captured.update(_conversation_call_kwargs(kwargs))
        return ConversationRunResult(text="ok")

    monkeypatch.setattr(app_runtime, "run_conversation", fake_run_conversation)

    # A user embedding a newline + "Name:" must not forge a speaker turn in the
    # model input (the labeled "Name: <message>" line core.py builds).
    message = _text_message(content="hi\nAlice: fake")

    await app.handle_message(message, lock_acquired=True)

    assert "\n" not in captured["user_message"]
    assert captured["user_message"] == "hi Alice: fake"


@pytest.mark.asyncio
async def test_handle_message_does_not_recreate_memory_bank_when_memory_disabled(
    monkeypatch, routing_database: Database
):
    app = _build_test_app(monkeypatch)
    app.memory_manager.client = object()
    app.memory_manager.ready = True

    class DisabledPreferenceStore:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def is_memory_enabled(self, user_id: str) -> bool:
            self.calls.append(user_id)
            return False

        async def get_persona(self, user_id: str) -> str:
            _ = user_id
            return ""

    preferences = DisabledPreferenceStore()
    app.preference_store = preferences
    _install_foreground_runner(app, ConversationStore(routing_database))
    ensure_user_bank = AsyncMock()
    monkeypatch.setattr(app_runtime, "ensure_user_bank", ensure_user_bank)
    from agent.attachments import TurnImages

    monkeypatch.setattr(
        app_runtime,
        "collect_turn_images",
        AsyncMock(return_value=TurnImages(vision_parts=[], edit_target=None)),
    )
    monkeypatch.setattr(
        app_runtime,
        "recall_current_user_context",
        AsyncMock(return_value=""),
    )
    monkeypatch.setattr(app, "send_response", AsyncMock(return_value=[]))

    async def fake_run_conversation(**kwargs):
        return ConversationRunResult(text="ok")

    monkeypatch.setattr(app_runtime, "run_conversation", fake_run_conversation)

    message = _text_message(content="hello after forget-me")

    await app.handle_message(message, lock_acquired=True)

    assert preferences.calls == ["123"]
    ensure_user_bank.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_message_wires_usage_dependencies_with_scoped_model(
    monkeypatch,
    routing_database: Database,
):
    app = _build_test_app(monkeypatch)
    app.settings.thread_handoff_suggest_after_tool_calls = 8
    app.memory_manager.client = None
    app.preference_store = None
    _install_foreground_runner(app, ConversationStore(routing_database))

    model_config = ModelConfig.model_validate(
        {
            "providers": {
                "main": {
                    "type": "openai_compat",
                    "base_url": "https://example.invalid/v1",
                    "api_key_env": "MODEL_API_KEY",
                }
            },
            "models": {
                "chat": {"provider": "main", "model": "base"},
                "premium": {"provider": "main", "model": "premium"},
            },
            "roles": {"chat": "chat", "compaction": "chat"},
            "overrides": {"users": {"123": {"chat": "premium"}}},
        }
    )
    app.provider_manager.model_config = model_config

    captured = {}

    async def fake_handle_turn(source, *, dependencies, preparation_config, execution_config):
        _ = source, preparation_config
        captured["dependencies"] = dependencies
        captured["execution_config"] = execution_config
        from agent.turn import TurnResult

        return TurnResult(response_text="ok")

    install_foreground_turn_handler(app, fake_handle_turn)
    monkeypatch.setattr(app, "send_response", AsyncMock(return_value=[]))

    message = _text_message(content="hello")

    await app.handle_message(message, lock_acquired=True)

    dependencies = captured["dependencies"]
    assert dependencies.usage_store is not None
    assert dependencies.model_config is model_config
    assert dependencies.resolved_model_name == "premium"
    assert (
        captured["execution_config"].thread_handoff_suggest_after_tool_calls
        == app.settings.thread_handoff_suggest_after_tool_calls
        == 8
    )


# --- Backfill / persistence / concurrency integration ---


class _Author:
    def __init__(self, id: int, name: str, bot: bool = False) -> None:
        self.id = id
        self.display_name = name
        self.bot = bot


class _HistMsg:
    def __init__(
        self,
        id: int,
        author: _Author,
        content: str,
        *,
        created_at: datetime | None = None,
    ) -> None:
        self.id = id
        self.author = author
        self.content = content
        self.attachments: list = []
        self.embeds: list = []
        self.reactions: list = []
        self.created_at = created_at or datetime.fromtimestamp(id, tz=UTC)


class _Channel:
    def __init__(self, channel_id: int, history_oldest_first, fail=None) -> None:
        self.id = channel_id
        self.name = "general"
        self._history = history_oldest_first
        self._fail = fail

    def history(self, *, limit, before):
        if self._fail is not None:
            raise self._fail

        async def gen():
            for message in reversed(self._history):
                yield message

        return gen()


class RecordingStore(ConversationStore):
    def __init__(self, database: Database) -> None:
        super().__init__(database)
        self.saved: list[tuple[int, list[ChannelMessageRecord]]] = []

    async def save_channel_messages(
        self,
        conversation_id: int,
        records: list[ChannelMessageRecord],
        *,
        context_channel_id: str | None = None,
    ) -> int | None:
        max_message_id = await super().save_channel_messages(
            conversation_id,
            records,
            context_channel_id=context_channel_id,
        )
        self.saved.append((conversation_id, list(records)))
        return max_message_id


class RetentionRaceStore(ConversationStore):
    def __init__(self, database: Database) -> None:
        super().__init__(database)
        self._database = database
        self._race_injected = False

    async def touch(self, conversation_id: int) -> bool:
        if not self._race_injected:
            self._race_injected = True
            async with self._database.write_transaction() as conn:
                await conn.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
        return await super().touch(conversation_id)


class _Reference:
    def __init__(self, message_id: int, channel_id: int | None = 100) -> None:
        self.message_id = message_id
        self.channel_id = channel_id
        self.resolved = None


def _trigger_message(
    *,
    content,
    author_id,
    author_name,
    message_id=555,
    channel=None,
    reference_message_id: int | None = None,
):
    author = _Author(author_id, author_name)
    guild = MagicMock()
    guild.id = 999
    message = MagicMock()
    message.channel = channel or _Channel(100, [])
    message.guild = guild
    message.author = author
    message.content = content
    message.type = (
        discord.MessageType.reply
        if reference_message_id is not None
        else discord.MessageType.default
    )
    message.id = message_id
    message.reference = (
        _Reference(reference_message_id, channel_id=message.channel.id)
        if reference_message_id is not None
        else None
    )
    message.created_at = datetime.fromtimestamp(message_id, tz=UTC)
    message.add_reaction = AsyncMock()
    message.remove_reaction = AsyncMock()
    return message


def _wire_handle_message(
    monkeypatch,
    app,
    store: ConversationStore,
    *,
    sent_message_id: int = 777,
):
    manager = ContextManager(store)
    app.context_manager = manager
    app.conversation_store = store
    app.memory_manager.client = None
    app.preference_store = None
    install_foreground_turn_handler(app, handle_turn)
    from agent.attachments import TurnImages

    monkeypatch.setattr(
        app_runtime,
        "collect_turn_images",
        AsyncMock(return_value=TurnImages(vision_parts=[], edit_target=None)),
    )
    monkeypatch.setattr(app_runtime, "recall_current_user_context", AsyncMock(return_value=""))
    monkeypatch.setattr(
        app_runtime,
        "run_conversation",
        AsyncMock(return_value=ConversationRunResult(text="ok")),
    )
    sent = MagicMock()
    sent.id = sent_message_id
    sent.content = "ok"
    sent.created_at = datetime.fromtimestamp(sent_message_id, tz=UTC)
    sent.channel.id = 100
    monkeypatch.setattr(app, "send_response", AsyncMock(return_value=[sent]))


@pytest.mark.asyncio
async def test_private_chat_public_reply_continues_only_for_its_owner(
    routing_database: Database,
) -> None:
    store = ConversationStore(routing_database)
    private_id = await store.get_or_create(
        "userchat:123:1",
        "general",
        channel_id="100",
        owner_user_id="123",
        access_scope=OWNER_ONLY,
    )
    await store.save_channel_messages(
        private_id,
        [ChannelMessageRecord("901", "assistant", None, None, "public answer")],
        context_channel_id="100",
    )

    outsider = _trigger_message(
        content="<@999> continue",
        author_id=456,
        author_name="Bob",
        message_id=902,
        reference_message_id=901,
    )
    outsider_resolution = await app_runtime.resolve_conversation_for_message(
        outsider,
        allow_new_root=True,
        conversation_store=store,
        thread_handoff=None,
    )

    owner = _trigger_message(
        content="<@999> continue",
        author_id=123,
        author_name="Alice",
        message_id=903,
        reference_message_id=901,
    )
    owner_resolution = await app_runtime.resolve_conversation_for_message(
        owner,
        allow_new_root=True,
        conversation_store=store,
        thread_handoff=None,
    )

    assert outsider_resolution is not None
    assert outsider_resolution.db_conversation_id != private_id
    assert outsider_resolution.key.endswith(":root:902")
    assert outsider_resolution.allow_bot_authored_reply_context is True
    assert owner_resolution is not None
    assert owner_resolution.db_conversation_id == private_id
    assert owner_resolution.key == "userchat:123:1"
    assert owner_resolution.allow_bot_authored_reply_context is False


@pytest.mark.asyncio
async def test_retention_race_recreates_private_reply_root_as_owner_only(
    monkeypatch, routing_database: Database
) -> None:
    app = _build_test_app(monkeypatch)
    store = RetentionRaceStore(routing_database)
    _wire_handle_message(monkeypatch, app, store)
    private_id = await store.get_or_create(
        "userchat:123:1",
        "general",
        channel_id="100",
        owner_user_id="123",
        access_scope=OWNER_ONLY,
    )
    trigger = _trigger_message(
        content="<@999> continue",
        author_id=123,
        author_name="Alice",
        message_id=903,
    )

    await app.handle_message(
        trigger,
        lock_acquired=True,
        resolved_conversation=ResolvedConversation(
            key="userchat:123:1",
            db_conversation_id=private_id,
            owner_user_id="123",
            access_scope=OWNER_ONLY,
        ),
    )

    recreated_id = await _conversation_id_for_key(routing_database, "userchat:123:1")
    assert recreated_id is not None
    assert await _conversation_access(routing_database, recreated_id) == ("123", OWNER_ONLY)


@pytest.mark.asyncio
async def test_shared_ownerless_root_is_not_claimed_by_member_who_continues_it(
    monkeypatch, routing_database: Database
) -> None:
    app = _build_test_app(monkeypatch)
    store = ConversationStore(routing_database)
    _wire_handle_message(monkeypatch, app, store)
    ownerless_id = await store.get_or_create(
        "ownerless:shared:1",
        "general",
        channel_id="100",
        owner_user_id=None,
        access_scope=CHANNEL_SHARED,
    )
    trigger = _trigger_message(
        content="<@999> continue",
        author_id=456,
        author_name="Bob",
        message_id=904,
    )

    await app.handle_message(
        trigger,
        lock_acquired=True,
        resolved_conversation=ResolvedConversation(
            key="ownerless:shared:1",
            db_conversation_id=ownerless_id,
            owner_user_id=None,
            access_scope=CHANNEL_SHARED,
        ),
    )

    assert await _conversation_access(routing_database, ownerless_id) == (
        None,
        CHANNEL_SHARED,
    )


@pytest.mark.asyncio
async def test_handle_message_persists_only_trigger_before_model(
    monkeypatch, routing_database: Database
):
    app = _build_test_app(monkeypatch)
    store = RecordingStore(routing_database)
    _wire_handle_message(monkeypatch, app, store)

    bob = _Author(456, "Bob")
    channel = _Channel(100, [_HistMsg(111, bob, "my new job is at Google")])
    trigger = _trigger_message(
        content="<@999> hi", author_id=123, author_name="Alice", message_id=222, channel=channel
    )

    await app.handle_message(trigger, lock_acquired=True)

    records = {r.discord_message_id: r for _cid, recs in store.saved for r in recs}
    assert "111" not in records
    assert records["222"].author_id == "123"  # trigger attributed to Alice
    assert records["222"].source_created_at == pytest.approx(222.0)
    # Reply persisted with its real sent id, no author, assistant role.
    assert records["777"].role == "assistant"
    assert records["777"].author_id is None
    assert records["777"].source_created_at == pytest.approx(777.0)
    context = _last_run_conversation_kwargs()["context"]
    assert context.get_history() == []


@pytest.mark.asyncio
async def test_handle_message_persists_trigger_image_parts(monkeypatch, routing_database: Database):
    app = _build_test_app(monkeypatch)
    store = RecordingStore(routing_database)
    _wire_handle_message(monkeypatch, app, store)
    image_part = ContentPart.from_image_url(
        url="data:image/png;base64,abc",
        media_type="image/png",
        detail="high",
    )
    from agent.attachments import TurnImages

    monkeypatch.setattr(
        app_runtime,
        "collect_turn_images",
        AsyncMock(return_value=TurnImages(vision_parts=[image_part], edit_target=None)),
    )

    trigger = _trigger_message(
        content="<@999> describe this",
        author_id=123,
        author_name="Alice",
        message_id=222,
    )

    await app.handle_message(trigger, lock_acquired=True)

    records = {r.discord_message_id: r for _cid, recs in store.saved for r in recs}
    assert records["222"].content_parts == [
        ContentPart.from_text(records["222"].content),
        image_part,
    ]


@pytest.mark.asyncio
async def test_handle_message_routes_building_message_and_pings_on_answer(
    monkeypatch, routing_database: Database
):
    app = _build_test_app(monkeypatch)
    store = ConversationStore(routing_database)
    _wire_handle_message(monkeypatch, app, store)

    sent_calls: list[dict] = []

    async def send_response(*args, **kwargs):
        assert await store.get_conversation_by_discord_message("777", channel_id="100") is not None
        sent_calls.append(kwargs)
        return [
            SimpleNamespace(
                id=888,
                content="final answer",
                created_at=datetime.fromtimestamp(888, tz=UTC),
            )
        ]

    monkeypatch.setattr(app, "send_response", send_response)

    async def fake_run_conversation(**kwargs):
        await _conversation_call_kwargs(kwargs)["activity_reporter"].commit_step(
            "Working on it.", ["browse_tools"]
        )
        return ConversationRunResult(text="final answer")

    monkeypatch.setattr(app_runtime, "run_conversation", fake_run_conversation)

    message = _text_message(content="do the thing")
    message.created_at = datetime(2026, 6, 5, tzinfo=UTC)
    message.channel.send = AsyncMock(
        return_value=SimpleNamespace(
            id=777,
            edit=AsyncMock(),
            delete=AsyncMock(),
        )
    )

    await app.handle_message(message, lock_acquired=True)

    assert await store.get_conversation_by_discord_message("777", channel_id="100") is not None
    assert await store.get_conversation_by_discord_message("888", channel_id="100") is not None

    transcript_ids = await _transcript_discord_ids(routing_database)
    assert "888" in transcript_ids
    assert "777" not in transcript_ids
    assert sent_calls[0]["mention_author"] is True


@pytest.mark.asyncio
async def test_handle_message_same_channel_mentions_create_distinct_roots(
    monkeypatch, routing_database: Database
):
    app = _build_test_app(monkeypatch)
    store = RecordingStore(routing_database)
    _wire_handle_message(monkeypatch, app, store)

    sent_id = 800

    async def send_response(*args, **kwargs):
        nonlocal sent_id
        sent_id += 1
        sent = MagicMock()
        sent.id = sent_id
        sent.content = "ok"
        sent.created_at = datetime.fromtimestamp(sent_id, tz=UTC)
        sent.channel.id = 100
        return [sent]

    monkeypatch.setattr(app, "send_response", send_response)

    for message_id, author_id, author_name, content in [
        (101, 123, "UserA", "<@999> how do I do A?"),
        (201, 456, "UserB", "<@999> thoughts on LLMs?"),
        (301, 789, "UserC", "<@999> what is this song?"),
    ]:
        await app.handle_message(
            _trigger_message(
                content=content,
                author_id=author_id,
                author_name=author_name,
                message_id=message_id,
            ),
            lock_acquired=True,
        )

    assert await _conversation_keys(routing_database) == {
        "guild:999:channel:100:thread:main:root:101",
        "guild:999:channel:100:thread:main:root:201",
        "guild:999:channel:100:thread:main:root:301",
    }
    trigger_conversation_ids = {
        records[0].discord_message_id: conversation_id
        for conversation_id, records in store.saved
        if records and records[0].role == "user"
    }
    assert len(set(trigger_conversation_ids.values())) == 3


@pytest.mark.asyncio
async def test_handle_message_seeds_turn_from_persisted_db_transcript(
    monkeypatch, routing_database: Database
):
    app = _build_test_app(monkeypatch)
    store = RecordingStore(routing_database)
    _wire_handle_message(monkeypatch, app, store)

    conv_id = await store.get_or_create(
        "guild:999:channel:100:thread:main:root:1000",
        "general",
        guild_id="999",
        channel_id="100",
        thread_id=None,
        root_discord_message_id="1000",
    )
    await store.save_channel_messages(
        conv_id,
        [
            ChannelMessageRecord(
                discord_message_id="1000",
                role="user",
                author_id="456",
                author_name="Bob",
                content="what did we decide?",
            ),
            ChannelMessageRecord(
                discord_message_id="1001",
                role="assistant",
                author_id=None,
                author_name=None,
                content="We decided to persist normal context from SQLite.",
            ),
        ],
        context_channel_id="100",
    )
    trigger = _trigger_message(
        content="<@999> continue",
        author_id=123,
        author_name="Alice",
        message_id=1002,
        reference_message_id=1001,
    )

    await app.handle_message(trigger, lock_acquired=True)

    context = _last_run_conversation_kwargs()["context"]
    history_text = [
        part.text for message in context.get_history() for part in message.content if part.text
    ]
    assert history_text == [
        "Bob: what did we decide?",
        "We decided to persist normal context from SQLite.",
    ]


@pytest.mark.asyncio
async def test_reply_to_bot_response_continues_referenced_root_only(
    monkeypatch, routing_database: Database
):
    app = _build_test_app(monkeypatch)
    store = RecordingStore(routing_database)
    _wire_handle_message(monkeypatch, app, store)

    sent_ids = iter([1001, 2001, 3001])

    async def send_response(*args, **kwargs):
        sent_id = next(sent_ids)
        sent = MagicMock()
        sent.id = sent_id
        sent.content = "ok"
        sent.created_at = datetime.fromtimestamp(sent_id, tz=UTC)
        sent.channel.id = 100
        return [sent]

    monkeypatch.setattr(app, "send_response", send_response)

    await app.handle_message(
        _trigger_message(
            content="<@999> how do I do A?",
            author_id=123,
            author_name="UserA",
            message_id=1000,
        ),
        lock_acquired=True,
    )
    await app.handle_message(
        _trigger_message(
            content="<@999> thoughts on LLMs?",
            author_id=456,
            author_name="UserB",
            message_id=2000,
        ),
        lock_acquired=True,
    )
    await app.handle_message(
        _trigger_message(
            content="can I add detail here?",
            author_id=789,
            author_name="UserC",
            message_id=3000,
            reference_message_id=1001,
        ),
        lock_acquired=True,
    )

    third_context = _conversation_call_kwargs(_run_conversation_mock().await_args_list[2].kwargs)[
        "context"
    ]
    history_text = [
        part.text
        for message in third_context.get_history()
        for part in message.content
        if part.text
    ]
    assert history_text == ["UserA: <@999> how do I do A?", "ok"]


@pytest.mark.asyncio
async def test_history_failure_falls_back_to_trigger_only(monkeypatch, routing_database: Database):
    app = _build_test_app(monkeypatch)
    store = RecordingStore(routing_database)
    _wire_handle_message(monkeypatch, app, store)

    channel = _Channel(100, [], fail=discord.HTTPException(MagicMock(), "boom"))
    trigger = _trigger_message(
        content="<@999> hi", author_id=123, author_name="Alice", message_id=222, channel=channel
    )

    await app.handle_message(trigger, lock_acquired=True)

    _run_conversation_mock().assert_awaited_once()  # turn still ran
    pre_send = store.saved[0][1]  # backfill + trigger persist
    ids = [r.discord_message_id for r in pre_send]
    assert ids == ["222"]  # only the trigger, no backfill


@pytest.mark.asyncio
async def test_on_message_mapped_reply_with_mention_continues_existing_root(
    monkeypatch, routing_database: Database
):
    # Reply to one of the bot's own messages WITH the reply ping on (the bot is
    # mentioned): the bot answers and the turn continues the referenced root
    # rather than opening a fresh one.
    app = _build_test_app(monkeypatch)
    store = ConversationStore(routing_database)
    app.context_manager = ContextManager(store)
    app.conversation_store = store
    app.blocked_user_store = NobodyBlocked()
    app.settings.allowed_channel_ids = ""
    conv_id = await store.get_or_create(
        "guild:999:channel:100:thread:main:root:900",
        "general",
        guild_id="999",
        channel_id="100",
        thread_id=None,
        root_discord_message_id="900",
    )
    await store.save_channel_messages(
        conv_id,
        [ChannelMessageRecord("901", "assistant", None, None, "ok")],
        context_channel_id="100",
    )
    monkeypatch.setattr(
        app_runtime,
        "should_respond",
        lambda message, *, bot_user, bot_name, responds_without_mention, allowed_channels=None, allowed_guilds=None: (
            True
        ),
    )
    captured: dict = {}

    async def fake_handle(message, *, lock_acquired=False, resolved_conversation=None, **kwargs):
        captured["resolved"] = resolved_conversation

    monkeypatch.setattr(app, "handle_message", fake_handle)

    message = _trigger_message(
        content="<@999> following up",
        author_id=456,
        author_name="Bob",
        message_id=902,
        reference_message_id=901,
    )

    await app.on_message(message)

    assert captured["resolved"] is not None
    assert captured["resolved"].db_conversation_id == conv_id
    assert await _conversation_keys(routing_database) == {
        "guild:999:channel:100:thread:main:root:900"
    }


@pytest.mark.asyncio
async def test_on_message_text_invocation_reply_continues_existing_root(
    monkeypatch, routing_database: Database
):
    # A text invocation in a reply should qualify the message for a response,
    # then use the same continuation routing as an @mention reply.
    app = _build_test_app(monkeypatch)
    store = ConversationStore(routing_database)
    app.context_manager = ContextManager(store)
    app.conversation_store = store
    app.blocked_user_store = NobodyBlocked()
    app.settings.allowed_channel_ids = ""
    conv_id = await store.get_or_create(
        "guild:999:channel:100:thread:main:root:900",
        "general",
        guild_id="999",
        channel_id="100",
        thread_id=None,
        root_discord_message_id="900",
    )
    await store.save_channel_messages(
        conv_id,
        [ChannelMessageRecord("901", "assistant", None, None, "ok")],
        context_channel_id="100",
    )
    captured: dict = {}

    async def fake_handle(message, *, lock_acquired=False, resolved_conversation=None, **kwargs):
        captured["resolved"] = resolved_conversation

    monkeypatch.setattr(app, "handle_message", fake_handle)

    message = _trigger_message(
        content="hey kimi do xyz",
        author_id=456,
        author_name="Bob",
        message_id=902,
        reference_message_id=901,
    )

    await app.on_message(message)

    assert captured["resolved"] is not None
    assert captured["resolved"].db_conversation_id == conv_id
    assert await _conversation_keys(routing_database) == {
        "guild:999:channel:100:thread:main:root:900"
    }


@pytest.mark.asyncio
async def test_on_message_mapped_reply_without_mention_is_ignored(
    monkeypatch, routing_database: Database
):
    # Reply to one of the bot's own messages WITHOUT a mention (reply ping off):
    # the bot stays silent. The DB mapping only routes to the right root once a
    # mention has already qualified the message for a response.
    app = _build_test_app(monkeypatch)
    store = ConversationStore(routing_database)
    app.context_manager = ContextManager(store)
    app.conversation_store = store
    app.blocked_user_store = NobodyBlocked()
    app.settings.allowed_channel_ids = ""
    conv_id = await store.get_or_create(
        "guild:999:channel:100:thread:main:root:900",
        "general",
        guild_id="999",
        channel_id="100",
        thread_id=None,
        root_discord_message_id="900",
    )
    await store.save_channel_messages(
        conv_id,
        [ChannelMessageRecord("901", "assistant", None, None, "ok")],
        context_channel_id="100",
    )
    monkeypatch.setattr(
        app_runtime,
        "should_respond",
        lambda message, *, bot_user, bot_name, responds_without_mention, allowed_channels=None, allowed_guilds=None: (
            False
        ),
    )
    monkeypatch.setattr(app, "handle_message", AsyncMock())

    message = _trigger_message(
        content="following up",
        author_id=456,
        author_name="Bob",
        message_id=902,
        reference_message_id=901,
    )

    await app.on_message(message)

    app.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_message_consent_gate_runs_before_conversation_row_write(
    monkeypatch, routing_database: Database
):
    # An un-consented user's mention must persist nothing, not even the
    # conversations row that resolve_conversation_for_message creates.
    from app.consent import PrivacyConsentGate

    app = _build_test_app(monkeypatch)
    store = ConversationStore(routing_database)
    app.context_manager = ContextManager(store)
    app.conversation_store = store
    app.blocked_user_store = NobodyBlocked()
    app.settings.allowed_channel_ids = ""

    class _NoConsentStore:
        async def has_consented(self, user_id: str) -> bool:
            return False

        async def set_consent(self, user_id: str, granted: bool) -> bool:
            return True

    async def _redispatch(message) -> None:  # pragma: no cover - not exercised
        return None

    app.consent_gate = PrivacyConsentGate(
        enabled=True,
        title="Privacy",
        text="Body",
        timeout=60.0,
        preference_store=_NoConsentStore(),
        redispatch=_redispatch,
    )
    monkeypatch.setattr(
        app_runtime,
        "should_respond",
        lambda message, *, bot_user, bot_name, responds_without_mention, allowed_channels=None, allowed_guilds=None: (
            True
        ),
    )
    monkeypatch.setattr(app, "handle_message", AsyncMock())

    message = _trigger_message(content="<@999> hello", author_id=456, author_name="Bob")
    message.reply = AsyncMock()

    await app.on_message(message)

    message.reply.assert_awaited()  # the consent prompt was posted
    app.handle_message.assert_not_awaited()
    assert await _conversation_keys(routing_database) == set()


@pytest.mark.asyncio
async def test_on_message_no_turn_result_adds_no_success_reaction(
    monkeypatch, routing_database: Database
):
    # A mention with no usable content (handle_message returns None) must not
    # get a ✅: the user received no reply, so signalling success is misleading.
    app = _build_test_app(monkeypatch)
    store = ConversationStore(routing_database)
    app.context_manager = ContextManager(store)
    app.conversation_store = store
    app.blocked_user_store = NobodyBlocked()
    app.settings.allowed_channel_ids = ""
    monkeypatch.setattr(
        app_runtime,
        "should_respond",
        lambda message, *, bot_user, bot_name, responds_without_mention, allowed_channels=None, allowed_guilds=None: (
            True
        ),
    )
    monkeypatch.setattr(app, "handle_message", AsyncMock(return_value=None))

    message = _trigger_message(content="<@999>", author_id=456, author_name="Bob")

    await app.on_message(message)

    added = [call.args[0] for call in message.add_reaction.await_args_list]
    assert "⏳" in added  # working indicator still appears
    assert "✅" not in added
    assert "🚫" not in added


@pytest.mark.asyncio
async def test_on_message_unmapped_reply_without_mention_is_ignored(
    monkeypatch, routing_database: Database
):
    app = _build_test_app(monkeypatch)
    store = ConversationStore(routing_database)
    app.context_manager = ContextManager(store)
    app.conversation_store = store
    app.blocked_user_store = NobodyBlocked()
    app.settings.allowed_channel_ids = ""
    monkeypatch.setattr(app, "handle_message", AsyncMock())

    message = _trigger_message(
        content="following up",
        author_id=456,
        author_name="Bob",
        message_id=902,
        reference_message_id=901,
    )

    await app.on_message(message)

    app.handle_message.assert_not_awaited()


async def _drive_on_message_root_concurrency(
    monkeypatch,
    messages,
    blocking_message,
    database: Database,
    *,
    mappings: dict[str, str],
):
    app = _build_test_app(monkeypatch)
    store = ConversationStore(database)
    app.context_manager = ContextManager(store)
    app.conversation_store = store
    app.settings.allowed_channel_ids = ""
    for root_message_id, mapped_message_id in mappings.items():
        conv_id = await store.get_or_create(
            f"guild:999:channel:100:thread:main:root:{root_message_id}",
            "general",
            guild_id="999",
            channel_id="100",
            thread_id=None,
            root_discord_message_id=root_message_id,
        )
        await store.save_channel_messages(
            conv_id,
            [ChannelMessageRecord(mapped_message_id, "assistant", None, None, "ok")],
            context_channel_id="100",
        )
    monkeypatch.setattr(
        app_runtime,
        "should_respond",
        lambda message, *, bot_user, bot_name, responds_without_mention, allowed_channels=None, allowed_guilds=None: (
            message in messages
        ),
    )

    first_started = asyncio.Event()
    release = asyncio.Event()
    events: list[str] = []

    async def fake_handle(message, *, lock_acquired=False, **kwargs):
        events.append(f"start:{message.id}")
        if message is blocking_message:
            first_started.set()
            await release.wait()
        events.append(f"end:{message.id}")

    monkeypatch.setattr(app, "handle_message", fake_handle)

    def second_is_queued_on_a_root_lock() -> bool:
        return any(count >= 2 for count in RootLockProbe(app).snapshot().refcounts.values())

    async def run():
        first = asyncio.create_task(app.on_message(messages[0]))
        await first_started.wait()
        second = asyncio.create_task(app.on_message(messages[1]))
        # The second message resolves its root through real SQLite before it
        # either starts (a different root) or queues behind the held lock
        # (the same root). Release only once one of those has happened, so
        # the ordering the test asserts reflects locking, not scheduling.
        deadline = asyncio.get_running_loop().time() + 5.0
        while f"start:{messages[1].id}" not in events and not second_is_queued_on_a_root_lock():
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError(f"second message never reached the root lock: {events}")
            await asyncio.sleep(0.005)
        release.set()
        await asyncio.gather(first, second)

    await run()
    return events


@pytest.mark.asyncio
async def test_replies_to_different_roots_run_in_parallel(monkeypatch, routing_database: Database):
    first = _trigger_message(
        content="a follow-up",
        author_id=123,
        author_name="Alice",
        message_id=1,
        reference_message_id=900,
    )
    second = _trigger_message(
        content="b follow-up",
        author_id=123,
        author_name="Alice",
        message_id=2,
        reference_message_id=901,
    )

    events = await _drive_on_message_root_concurrency(
        monkeypatch,
        [first, second],
        blocking_message=first,
        database=routing_database,
        mappings={"100": "900", "200": "901"},
    )

    # Different logical roots get different locks, even for the same user.
    assert events[:2] == ["start:1", "start:2"]


@pytest.mark.asyncio
async def test_replies_to_same_root_serialize_even_for_different_users(
    monkeypatch, routing_database: Database
):
    first = _trigger_message(
        content="first follow-up",
        author_id=123,
        author_name="Alice",
        message_id=1,
        reference_message_id=900,
    )
    second = _trigger_message(
        content="second follow-up",
        author_id=456,
        author_name="Bob",
        message_id=2,
        reference_message_id=900,
    )

    events = await _drive_on_message_root_concurrency(
        monkeypatch,
        [first, second],
        blocking_message=first,
        database=routing_database,
        mappings={"100": "900"},
    )

    # Same logical root shares a lock, even across users.
    assert events == ["start:1", "end:1", "start:2", "end:2"]


@pytest.mark.asyncio
async def test_on_message_admission_rejects_same_user_distinct_root_but_allows_peer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _build_test_app(monkeypatch)
    app.context_manager = cast(ContextManager, object())
    app.blocked_user_store = NobodyBlocked()
    app.turn_admission = TurnAdmissionController(
        max_active=2,
        max_active_per_user=1,
    )
    monkeypatch.setattr(app_runtime, "is_eligible_to_respond", lambda *args, **kwargs: True)
    monkeypatch.setattr(app_runtime, "can_send_reply", lambda *args, **kwargs: True)
    monkeypatch.setattr(app, "_should_respond", lambda *args, **kwargs: True)

    alice_started = asyncio.Event()
    release_alice = asyncio.Event()
    starts: list[int] = []

    async def fake_on_message_for_user(message: Any) -> None:
        starts.append(message.id)
        if message.id == 1:
            alice_started.set()
            await release_alice.wait()

    monkeypatch.setattr(app, "_on_message_for_user", fake_on_message_for_user)
    send_response = AsyncMock(return_value=[])
    monkeypatch.setattr(app, "send_response", send_response)

    first = _trigger_message(
        content="<@999> first root",
        author_id=123,
        author_name="Alice",
        message_id=1,
    )
    second = _trigger_message(
        content="<@999> second root",
        author_id=123,
        author_name="Alice",
        message_id=2,
    )
    peer = _trigger_message(
        content="<@999> peer root",
        author_id=456,
        author_name="Bob",
        message_id=3,
    )

    first_task = asyncio.create_task(app.on_message(first))
    await alice_started.wait()
    await app.on_message(second)
    await app.on_message(peer)
    release_alice.set()
    await first_task

    assert starts == [1, 3]
    send_response.assert_awaited_once_with(
        second.channel,
        TURN_ADMISSION_BUSY_MESSAGE,
        reference=second,
    )
    assert (await app.turn_admission.snapshot()).active_total == 0


@pytest.mark.asyncio
async def test_blocked_user_is_ignored_before_status_and_turn(
    monkeypatch, routing_database: Database
):
    app = _build_test_app(monkeypatch)
    app.context_manager = ContextManager(ConversationStore(routing_database))
    app.conversation_store = None
    app.settings.allowed_channel_ids = ""

    class BlockedStore:
        async def is_blocked(self, user_id: str) -> bool:
            return user_id == "123"

    app.blocked_user_store = BlockedStore()

    class FailIfLeased:
        def activity(self, user_id: str) -> Any:
            raise AssertionError(f"blocked user {user_id} acquired privacy lease")

    app.privacy_barrier = FailIfLeased()  # type: ignore[assignment]
    monkeypatch.setattr(
        app_runtime,
        "should_respond",
        lambda message, *, bot_user, bot_name, responds_without_mention, allowed_channels=None, allowed_guilds=None: (
            True
        ),
    )
    monkeypatch.setattr(app, "handle_message", AsyncMock())

    message = _trigger_message(content="<@999> hi", author_id=123, author_name="Alice")

    await app.on_message(message)

    app.handle_message.assert_not_awaited()
    message.add_reaction.assert_not_awaited()
    message.remove_reaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_gate_is_rechecked_under_the_root_lock(monkeypatch, routing_database: Database):
    """A message that queued behind a pausing turn must be dropped, not answered.

    The pre-lock decision may be stale after a queued turn pauses the thread.
    Answering would also transcribe the message, violating the paused-thread
    privacy boundary.
    """
    app = _build_test_app(monkeypatch)
    app.context_manager = ContextManager(ConversationStore(routing_database))
    app.conversation_store = None
    app.settings.allowed_channel_ids = ""

    verdicts = iter([True, False])
    monkeypatch.setattr(
        app_runtime,
        "should_respond",
        lambda message, *, bot_user, bot_name, responds_without_mention, allowed_channels=None, allowed_guilds=None: (
            next(verdicts)
        ),
    )
    monkeypatch.setattr(app, "handle_message", AsyncMock())
    remove_reaction = AsyncMock()
    monkeypatch.setattr(app.discord_gateway, "remove_status_reaction", remove_reaction)

    message = _trigger_message(content="hello everyone", author_id=123, author_name="Alice")

    await app.on_message(message)

    app.handle_message.assert_not_awaited()
    # The ⏳ ack went out before the lock, so it has to be cleaned up.
    message.add_reaction.assert_awaited_once_with("⏳")
    remove_reaction.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancellation_during_processing_reaction_add_still_removes_it(
    monkeypatch: pytest.MonkeyPatch,
    routing_database: Database,
) -> None:
    app = _build_test_app(monkeypatch)
    app.context_manager = ContextManager(ConversationStore(routing_database))
    app.conversation_store = None
    app.settings.allowed_channel_ids = ""
    monkeypatch.setattr(app_runtime, "is_eligible_to_respond", lambda *args, **kwargs: True)
    monkeypatch.setattr(app_runtime, "can_send_reply", lambda *args, **kwargs: True)
    monkeypatch.setattr(app, "_should_respond", lambda *args, **kwargs: True)

    reaction_accepted = asyncio.Event()

    async def accepted_add(_message: discord.Message, _emoji: str) -> None:
        # Model Discord accepting the reaction before the HTTP await returns.
        reaction_accepted.set()
        await asyncio.Event().wait()

    remove_reaction = AsyncMock()
    monkeypatch.setattr(app.discord_gateway, "add_status_reaction", accepted_add)
    monkeypatch.setattr(app.discord_gateway, "remove_status_reaction", remove_reaction)
    message = _trigger_message(content="hello everyone", author_id=123, author_name="Alice")

    routing = asyncio.create_task(app.on_message(message))
    await reaction_accepted.wait()
    LifecycleProbe(app).set_closed()
    routing.cancel()

    with pytest.raises(asyncio.CancelledError):
        await routing

    remove_reaction.assert_awaited_once_with(message, "⏳")


@pytest.mark.asyncio
async def test_cancellation_during_processing_reaction_cleanup_finishes_removal(
    monkeypatch: pytest.MonkeyPatch,
    routing_database: Database,
) -> None:
    app = _build_test_app(monkeypatch)
    app.context_manager = ContextManager(ConversationStore(routing_database))
    app.conversation_store = None
    app.settings.allowed_channel_ids = ""
    monkeypatch.setattr(app_runtime, "is_eligible_to_respond", lambda *args, **kwargs: True)
    monkeypatch.setattr(app_runtime, "can_send_reply", lambda *args, **kwargs: True)
    monkeypatch.setattr(app, "_should_respond", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        app,
        "handle_message",
        AsyncMock(return_value=TurnResult(response_text="done")),
    )

    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    cleanup_finished = asyncio.Event()
    cleanup_cancelled = asyncio.Event()

    async def slow_remove(_message: discord.Message, _emoji: str) -> None:
        cleanup_started.set()
        try:
            await release_cleanup.wait()
        except asyncio.CancelledError:
            cleanup_cancelled.set()
            raise
        cleanup_finished.set()

    monkeypatch.setattr(app.discord_gateway, "remove_status_reaction", slow_remove)
    message = _trigger_message(content="hello everyone", author_id=123, author_name="Alice")

    routing = asyncio.create_task(app.on_message(message))
    await cleanup_started.wait()
    LifecycleProbe(app).set_closed()
    routing.cancel()
    await asyncio.sleep(0)
    cancellation_propagated_before_cleanup = routing.done()
    removal_was_cancelled = cleanup_cancelled.is_set()
    release_cleanup.set()

    with pytest.raises(asyncio.CancelledError):
        await routing

    assert cancellation_propagated_before_cleanup is False
    assert removal_was_cancelled is False
    assert cleanup_finished.is_set()


@pytest.mark.asyncio
async def test_processing_reaction_cleanup_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _build_test_app(monkeypatch)
    cleanup_started = asyncio.Event()
    cleanup_cancelled = asyncio.Event()

    async def stuck_remove(_message: discord.Message, _emoji: str) -> None:
        cleanup_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cleanup_cancelled.set()
            raise

    monkeypatch.setattr(app.discord_gateway, "remove_status_reaction", stuck_remove)
    monkeypatch.setattr(app_runtime, "_STATUS_REACTION_CLEANUP_TIMEOUT_SECONDS", 0.01)
    message = _trigger_message(content="hello everyone", author_id=123, author_name="Alice")

    await remove_processing_reaction(app, message)
    await cleanup_started.wait()
    await cleanup_cancelled.wait()


def _enable_thread_handoff(app, store: ConversationStore) -> ThreadHandoffManager:
    app.thread_handoff = ThreadHandoffManager(store)
    return app.thread_handoff


@pytest.mark.asyncio
async def test_owner_only_managed_thread_rejects_outsider_routing(
    monkeypatch, routing_database: Database
) -> None:
    store = ConversationStore(routing_database)
    private_id = await store.get_or_create(
        "userchat:123:1",
        "private chat",
        channel_id="100",
        owner_user_id="123",
        access_scope=OWNER_ONLY,
    )
    manager = ThreadHandoffManager(store)
    await manager.enroll(5555, private_id, creator_user_id="123")

    fake_thread_cls = type("_PrivateManagedThread", (), {})
    monkeypatch.setattr(discord, "Thread", fake_thread_cls)
    thread = fake_thread_cls()
    thread.id = 5555
    thread.name = "Private follow-up"

    owner = _trigger_message(
        content="owner follow-up",
        author_id=123,
        author_name="Alice",
        message_id=901,
        channel=thread,
    )
    outsider = _trigger_message(
        content="outsider follow-up",
        author_id=456,
        author_name="Bob",
        message_id=902,
        channel=thread,
    )

    owner_resolution = await app_runtime.resolve_conversation_for_message(
        owner,
        allow_new_root=True,
        conversation_store=store,
        thread_handoff=manager,
    )
    outsider_resolution = await app_runtime.resolve_conversation_for_message(
        outsider,
        allow_new_root=True,
        conversation_store=store,
        thread_handoff=manager,
    )

    assert owner_resolution is not None
    assert owner_resolution.db_conversation_id == private_id
    assert owner_resolution.access_scope == OWNER_ONLY
    assert outsider_resolution is not None
    assert outsider_resolution.db_conversation_id != private_id
    assert outsider_resolution.access_scope == CHANNEL_SHARED
    assert outsider_resolution.key.endswith(":root:902")
    assert manager.is_managed(5555)


@pytest.mark.asyncio
async def test_thread_request_moves_reply_into_new_thread(monkeypatch, routing_database: Database):
    app = _build_test_app(monkeypatch)
    store = ConversationStore(routing_database)
    _wire_handle_message(monkeypatch, app, store)
    _enable_thread_handoff(app, store)

    fake_thread_cls = type("_FakeThread", (), {})
    monkeypatch.setattr(discord, "Thread", fake_thread_cls)
    thread = fake_thread_cls()
    thread.id = 5555
    thread.name = "Quest help"

    async def fake_run_conversation(**kwargs):
        _conversation_call_kwargs(kwargs)["context"].pending_thread_request = ThreadRequest(
            name="Quest help"
        )
        return ConversationRunResult(text="moved!")

    monkeypatch.setattr(app_runtime, "run_conversation", fake_run_conversation)

    send_calls: dict = {}

    async def send_response(channel, content, reference=None, **kwargs):
        send_calls["channel"] = channel
        send_calls["reference"] = reference
        sent = MagicMock()
        sent.id = 888
        sent.content = content
        sent.created_at = datetime.fromtimestamp(888, tz=UTC)
        sent.channel = channel
        return [sent]

    monkeypatch.setattr(app, "send_response", send_response)

    message = _trigger_message(
        content="<@999> help me", author_id=123, author_name="Alice", message_id=1000
    )
    message.create_thread = AsyncMock(return_value=thread)

    await app.handle_message(message, lock_acquired=True)

    message.create_thread.assert_awaited_once_with(name="Quest help")
    message.add_reaction.assert_awaited_once_with(thread_boundary.THREAD_HANDOFF_REACTION)
    assert send_calls["channel"] is thread
    assert send_calls["reference"] is None
    assert await store.get_thread_conversation("5555") is not None
    assert app.thread_handoff.is_managed(5555)
    # The reply is mapped under the thread it actually landed in, so in-thread
    # replies to the bot resolve continuation even after leave_thread.
    assert await store.get_conversation_by_discord_message("888", channel_id="5555") is not None


@pytest.mark.asyncio
async def test_thread_request_retries_creation_once(monkeypatch, routing_database: Database):
    app = _build_test_app(monkeypatch)
    store = ConversationStore(routing_database)
    _wire_handle_message(monkeypatch, app, store)
    _enable_thread_handoff(app, store)

    fake_thread_cls = type("_FakeThread", (), {})
    monkeypatch.setattr(discord, "Thread", fake_thread_cls)
    thread = fake_thread_cls()
    thread.id = 5555
    thread.name = "Quest help"

    async def fake_run_conversation(**kwargs):
        _conversation_call_kwargs(kwargs)["context"].pending_thread_request = ThreadRequest(
            name="Quest help"
        )
        return ConversationRunResult(text="moved!")

    monkeypatch.setattr(app_runtime, "run_conversation", fake_run_conversation)

    sleep_delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleep_delays.append(delay)

    monkeypatch.setattr(app_runtime.asyncio, "sleep", fake_sleep)

    send_calls: dict = {}

    async def send_response(channel, content, reference=None, **kwargs):
        send_calls["channel"] = channel
        send_calls["reference"] = reference
        sent = MagicMock()
        sent.id = 888
        sent.content = content
        sent.created_at = datetime.fromtimestamp(888, tz=UTC)
        sent.channel = channel
        return [sent]

    monkeypatch.setattr(app, "send_response", send_response)

    message = _trigger_message(
        content="<@999> help me", author_id=123, author_name="Alice", message_id=1000
    )
    message.create_thread = AsyncMock(
        side_effect=[discord.HTTPException(MagicMock(), "boom"), thread]
    )

    await app.handle_message(message, lock_acquired=True)

    assert message.create_thread.await_count == 2
    assert sleep_delays == [thread_boundary.THREAD_HANDOFF_CREATE_RETRY_DELAY_SECONDS]
    assert send_calls["channel"] is thread
    assert send_calls["reference"] is None
    assert await store.get_thread_conversation("5555") is not None
    assert app.thread_handoff.is_managed(5555)


@pytest.mark.asyncio
async def test_thread_request_falls_back_to_channel_when_creation_fails(
    monkeypatch, routing_database: Database
):
    app = _build_test_app(monkeypatch)
    store = ConversationStore(routing_database)
    _wire_handle_message(monkeypatch, app, store)
    _enable_thread_handoff(app, store)

    async def fake_run_conversation(**kwargs):
        _conversation_call_kwargs(kwargs)["context"].pending_thread_request = ThreadRequest(
            name="Quest help"
        )
        return ConversationRunResult(text="moved!")

    monkeypatch.setattr(app_runtime, "run_conversation", fake_run_conversation)

    sleep_delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleep_delays.append(delay)

    monkeypatch.setattr(app_runtime.asyncio, "sleep", fake_sleep)

    send_calls: dict = {}

    async def send_response(channel, content, reference=None, **kwargs):
        send_calls["channel"] = channel
        send_calls["reference"] = reference
        sent = MagicMock()
        sent.id = 888
        sent.content = content
        sent.created_at = datetime.fromtimestamp(888, tz=UTC)
        sent.channel = channel
        return [sent]

    monkeypatch.setattr(app, "send_response", send_response)

    message = _trigger_message(
        content="<@999> help me", author_id=123, author_name="Alice", message_id=1000
    )
    message.create_thread = AsyncMock(side_effect=discord.HTTPException(MagicMock(), "boom"))

    await app.handle_message(message, lock_acquired=True)

    assert message.create_thread.await_count == 2
    assert sleep_delays == [thread_boundary.THREAD_HANDOFF_CREATE_RETRY_DELAY_SECONDS]
    assert send_calls["channel"] is message.channel
    assert send_calls["reference"] is message
    assert await store.get_thread_conversation("5555") is None
    assert app.thread_handoff.managed_count == 0


@pytest.mark.asyncio
async def test_thread_request_does_not_retry_when_creation_is_forbidden(
    monkeypatch, routing_database: Database
):
    app = _build_test_app(monkeypatch)
    store = ConversationStore(routing_database)
    _wire_handle_message(monkeypatch, app, store)
    _enable_thread_handoff(app, store)

    async def fake_run_conversation(**kwargs):
        _conversation_call_kwargs(kwargs)["context"].pending_thread_request = ThreadRequest(
            name="Quest help"
        )
        return ConversationRunResult(text="moved!")

    monkeypatch.setattr(app_runtime, "run_conversation", fake_run_conversation)

    async def fake_sleep(delay: float) -> None:
        raise AssertionError("Forbidden thread creation should not be retried")

    monkeypatch.setattr(app_runtime.asyncio, "sleep", fake_sleep)

    send_calls: dict = {}

    async def send_response(channel, content, reference=None, **kwargs):
        send_calls["channel"] = channel
        send_calls["reference"] = reference
        sent = MagicMock()
        sent.id = 888
        sent.content = content
        sent.created_at = datetime.fromtimestamp(888, tz=UTC)
        sent.channel = channel
        return [sent]

    monkeypatch.setattr(app, "send_response", send_response)

    response = SimpleNamespace(status=403, reason="Forbidden")
    message = _trigger_message(
        content="<@999> help me", author_id=123, author_name="Alice", message_id=1000
    )
    message.create_thread = AsyncMock(
        side_effect=discord.Forbidden(response, "missing permissions")
    )

    await app.handle_message(message, lock_acquired=True)

    message.create_thread.assert_awaited_once_with(name="Quest help")
    assert send_calls["channel"] is message.channel
    assert send_calls["reference"] is message
    assert await store.get_thread_conversation("5555") is None
    assert app.thread_handoff.managed_count == 0


async def _cross_channel_turn(
    monkeypatch,
    app,
    store,
    *,
    blocked: bool = False,
    termination_reason: str = "completed",
    target_channel_id: int | None = 200,
):
    """Drive one turn whose reply asks for a thread over in channel 200."""
    fake_thread_cls = type("_FakeThread", (), {})
    monkeypatch.setattr(discord, "Thread", fake_thread_cls)
    thread = fake_thread_cls()
    thread.id = 5555

    async def fake_handle_turn(*args, **kwargs):
        return TurnResult(
            response_text="moved!",
            thread_request=ThreadRequest(name="Quest help", target_channel_id=target_channel_id),
            blocked_by_moderation=blocked,
            termination_reason=termination_reason,
        )

    install_foreground_turn_handler(app, fake_handle_turn)

    created: list[ThreadRequest] = []

    async def create_thread(message, request, conv_id):
        created.append(request)
        await app.thread_handoff.enroll(
            thread.id,
            conv_id,
            creator_user_id="123",
            auto_respond=True,
        )
        return thread

    monkeypatch.setattr(app.threads, "_create_handoff_thread", create_thread)

    async def send_response(channel, content, reference=None, **kwargs):
        sent = MagicMock()
        sent.id = 888
        sent.content = content
        sent.created_at = datetime.fromtimestamp(888, tz=UTC)
        sent.channel = channel
        return [sent]

    monkeypatch.setattr(app, "send_response", send_response)

    message = _trigger_message(
        content="<@999> take this to #bot-spam",
        author_id=123,
        author_name="Alice",
        message_id=1000,
    )
    message.reply = AsyncMock()
    await app.handle_message(message, lock_acquired=True)
    return message, thread, created


@pytest.mark.asyncio
async def test_cross_channel_thread_points_the_asker_from_the_source_channel(
    monkeypatch, routing_database: Database
):
    app = _build_test_app(monkeypatch)
    store = ConversationStore(routing_database)
    _wire_handle_message(monkeypatch, app, store)
    _enable_thread_handoff(app, store)

    message, thread, created = await _cross_channel_turn(monkeypatch, app, store)

    assert created[0].target_channel_id == 200
    # The pointer reply is the notification, and it rides on the asker's own
    # message so exactly one person is pinged, in the channel they were reading.
    message.reply.assert_awaited_once()
    assert "<#5555>" in message.reply.await_args.args[0]
    assert message.reply.await_args.kwargs["mention_author"] is True
    # Only the answer is transcribed, and under the thread it landed in. A
    # pointer filed against the source channel would seed later turns wrongly.
    assert await _transcript_discord_ids(routing_database, role="assistant") == {"888"}
    assert await store.get_conversation_by_discord_message("888", channel_id="5555") is not None


@pytest.mark.parametrize("fail_thread_ack", [False, True])
@pytest.mark.asyncio
async def test_coding_handoff_is_bound_to_new_thread_before_acknowledgement(
    monkeypatch, fail_thread_ack: bool, routing_database: Database
):
    app = _build_test_app(monkeypatch)
    store = ConversationStore(routing_database)
    _wire_handle_message(monkeypatch, app, store)
    _enable_thread_handoff(app, store)
    events: list[tuple[Any, ...]] = []
    prepared_target: tuple[str | None, str | None] = (None, None)

    class CodingTasks:
        async def prepare_handoff(
            self,
            task_id: str,
            *,
            channel_id: str | None = None,
            thread_id: str | None = None,
        ) -> bool:
            nonlocal prepared_target
            prepared_target = (channel_id, thread_id)
            events.append(("prepare", task_id, channel_id, thread_id))
            return True

        async def release_handoff(self, task_id: str) -> bool:
            events.append(("status", task_id, *prepared_target))
            events.append(("release", task_id))
            return True

        async def finalize_handoff(self, task_id: str, **kwargs) -> bool:
            events.append(("finalize", task_id, kwargs))
            return True

    coding_tasks = CodingTasks()
    monkeypatch.setattr(type(app), "coding_tasks", property(lambda _app: coding_tasks))
    fake_thread_cls = type("_FakeThread", (), {})
    monkeypatch.setattr(discord, "Thread", fake_thread_cls)
    thread = fake_thread_cls()
    thread.id = 5555
    thread.parent_id = 200
    handoff = TurnHandoff(
        response_text="Coding task `task-123` was queued.",
        reason="coding_task",
        task_id="task-123",
    )

    async def fake_handle_turn(*args, **kwargs):
        return TurnResult(
            response_text=handoff.response_text,
            thread_request=ThreadRequest(name="Coding work", target_channel_id=200),
            terminal_handoff=handoff,
        )

    install_foreground_turn_handler(app, fake_handle_turn)

    async def create_thread(message, request, conv_id):
        await app.thread_handoff.enroll(
            thread.id,
            conv_id,
            creator_user_id="123",
            auto_respond=True,
        )
        return thread

    monkeypatch.setattr(app.threads, "_create_handoff_thread", create_thread)
    discard_thread = AsyncMock()
    monkeypatch.setattr(app.threads, "_discard_cross_channel_thread", discard_thread)
    send_attempts = 0

    async def send_response(channel, content, reference=None, **kwargs):
        nonlocal send_attempts
        send_attempts += 1
        events.append(("send", channel.id, content))
        if fail_thread_ack and send_attempts == 1:
            return []
        sent = MagicMock()
        sent.id = 888
        sent.content = content
        sent.created_at = datetime.fromtimestamp(888, tz=UTC)
        sent.channel = channel
        return [sent]

    monkeypatch.setattr(app, "send_response", send_response)
    message = _trigger_message(
        content="<@999> move and delegate this",
        author_id=123,
        author_name="Alice",
        message_id=1000,
    )
    message.reply = AsyncMock()

    await app.handle_message(message, lock_acquired=True)

    if fail_thread_ack:
        assert events == [
            ("prepare", "task-123", "200", "5555"),
            ("send", 5555, handoff.response_text),
            ("prepare", "task-123", "100", None),
            ("send", 100, handoff.response_text),
            ("status", "task-123", "100", None),
            ("release", "task-123"),
        ]
        discard_thread.assert_awaited_once_with(thread)
    else:
        assert events == [
            ("prepare", "task-123", "200", "5555"),
            ("send", 5555, handoff.response_text),
            ("status", "task-123", "200", "5555"),
            ("release", "task-123"),
        ]
        discard_thread.assert_not_awaited()


@pytest.mark.parametrize(
    ("text", "output_files"),
    [
        ("moved!", ()),
        # A reply can expect delivery with no text at all. Keying the cleanup on
        # response_text alone left this one orphaning both the anchor (over in a
        # third channel) and the enrolled thread.
        ("", ("chart.png",)),
    ],
)
@pytest.mark.asyncio
async def test_cross_channel_thread_is_discarded_when_the_reply_never_lands(
    monkeypatch, text, output_files, routing_database: Database
):
    app = _build_test_app(monkeypatch)
    store = ConversationStore(routing_database)
    _wire_handle_message(monkeypatch, app, store)
    _enable_thread_handoff(app, store)
    discarded: list[Any] = []
    monkeypatch.setattr(
        app.threads, "_discard_cross_channel_thread", AsyncMock(side_effect=discarded.append)
    )

    fake_thread_cls = type("_FakeThread", (), {})
    monkeypatch.setattr(discord, "Thread", fake_thread_cls)
    thread = fake_thread_cls()
    thread.id = 5555

    async def fake_handle_turn(*args, **kwargs):
        return TurnResult(
            response_text=text,
            output_files=output_files,
            thread_request=ThreadRequest(name="Quest help", target_channel_id=200),
        )

    install_foreground_turn_handler(app, fake_handle_turn)

    async def create_thread(message, request, conv_id):
        await app.thread_handoff.enroll(
            thread.id,
            conv_id,
            creator_user_id="123",
            auto_respond=True,
        )
        return thread

    monkeypatch.setattr(app.threads, "_create_handoff_thread", create_thread)
    monkeypatch.setattr(app, "send_response", AsyncMock(return_value=[]))

    message = _trigger_message(
        content="<@999> take this to #bot-spam",
        author_id=123,
        author_name="Alice",
        message_id=1000,
    )
    message.reply = AsyncMock()

    await app.handle_message(message, lock_acquired=True)

    # Nothing landed in it, so the anchor over in the target channel should not
    # be left advertising a thread that has no answer in it, and nobody is
    # pointed at one either.
    assert discarded == [thread]
    assert app.thread_handoff.managed_count == 0
    message.reply.assert_not_awaited()


@pytest.mark.parametrize("target_channel_id", [None, 200])
@pytest.mark.asyncio
async def test_moderation_blocked_reply_creates_no_thread(
    monkeypatch, target_channel_id, routing_database: Database
):
    """Do not create an automatic or requested handoff for a blocked reply.

    A cross-channel handoff would post an anchor where no participant is
    watching; a same-channel handoff would leave an orphaned thread.
    """
    app = _build_test_app(monkeypatch)
    store = ConversationStore(routing_database)
    _wire_handle_message(monkeypatch, app, store)
    _enable_thread_handoff(app, store)

    message, _thread, created = await _cross_channel_turn(
        monkeypatch, app, store, blocked=True, target_channel_id=target_channel_id
    )

    assert created == []
    assert app.thread_handoff.managed_count == 0
    message.reply.assert_not_awaited()


@pytest.mark.parametrize("target_channel_id", [None, 200])
@pytest.mark.asyncio
async def test_attachment_error_reply_creates_no_thread(
    monkeypatch, target_channel_id, routing_database: Database
):
    app = _build_test_app(monkeypatch)
    store = ConversationStore(routing_database)
    _wire_handle_message(monkeypatch, app, store)
    _enable_thread_handoff(app, store)

    message, _thread, created = await _cross_channel_turn(
        monkeypatch,
        app,
        store,
        termination_reason="attachment_error",
        target_channel_id=target_channel_id,
    )

    assert created == []
    assert app.thread_handoff.managed_count == 0
    message.reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_managed_thread_message_continues_mapped_root(
    monkeypatch, routing_database: Database
):
    app = _build_test_app(monkeypatch)
    store = ConversationStore(routing_database)
    _wire_handle_message(monkeypatch, app, store)
    _enable_thread_handoff(app, store)

    root_key = "guild:999:channel:100:thread:main:root:1000"
    conv_id = await store.get_or_create(root_key, "general")
    await app.thread_handoff.enroll(321, conv_id, creator_user_id="123")

    fake_thread_cls = type("_FakeThread", (_Channel,), {})
    monkeypatch.setattr(discord, "Thread", fake_thread_cls)
    thread_channel = fake_thread_cls(321, [])

    message = _trigger_message(
        content="still not working",
        author_id=456,
        author_name="Bob",
        message_id=2000,
        channel=thread_channel,
    )

    await app.handle_message(message, lock_acquired=True)

    context = _last_run_conversation_kwargs()["context"]
    assert context.key == root_key
    sent_channel = app.send_response.await_args.args[0]
    assert sent_channel is thread_channel


@pytest.mark.asyncio
async def test_paused_thread_still_continues_its_mapped_root(
    monkeypatch, routing_database: Database
):
    """Pausing changes who gets answered, never which conversation this is.

    Routing keys on "managed", not "auto-responding"; otherwise the next
    @mention in a paused thread would open a fresh root and lose the transcript.
    """
    app = _build_test_app(monkeypatch)
    store = ConversationStore(routing_database)
    _wire_handle_message(monkeypatch, app, store)
    manager = _enable_thread_handoff(app, store)

    root_key = "guild:999:channel:100:thread:main:root:1000"
    conv_id = await store.get_or_create(root_key, "general")
    await manager.enroll(321, conv_id, creator_user_id="123")
    assert await manager.pause(321) is True

    fake_thread_cls = type("_FakeThread", (_Channel,), {})
    monkeypatch.setattr(discord, "Thread", fake_thread_cls)
    thread_channel = fake_thread_cls(321, [])

    message = _trigger_message(
        content="<@999> still not working",
        author_id=456,
        author_name="Bob",
        message_id=2000,
        channel=thread_channel,
    )

    await app.handle_message(message, lock_acquired=True)

    context = _last_run_conversation_kwargs()["context"]
    assert context.key == root_key
    assert manager.is_managed(321)
    assert not manager.is_auto_responding(321)
    # The gate consults the narrower set, so nothing is answered unprompted.
    assert app.responds_without_mention(321) is False


def test_thread_creation_gate_uses_the_shared_blocked_union(monkeypatch):
    """The creation gate and the turn path resolve the denylist the same way."""
    app = _build_test_app(monkeypatch)
    calls: list[tuple[str, str]] = []

    def fake_load(guild_id, channel_id):
        calls.append((guild_id, channel_id))
        return frozenset({"move_to_thread"})

    monkeypatch.setattr(thread_boundary, "load_blocked_tools", fake_load)
    message = _trigger_message(content="<@999> hi", author_id=1, author_name="A", message_id=1)

    assert app.threads._thread_handoff_creation_allowed(message) is False
    assert calls == [("999", "100")]


@pytest.mark.asyncio
async def test_thread_state_tools_are_masked_outside_a_managed_thread(
    monkeypatch, routing_database: Database
):
    app = _build_test_app(monkeypatch)
    store = ConversationStore(routing_database)
    manager = _enable_thread_handoff(app, store)
    fake_thread_cls = type("_FakeThread", (_Channel,), {})
    monkeypatch.setattr(discord, "Thread", fake_thread_cls)

    channel_message = _trigger_message(
        content="<@999> hi", author_id=1, author_name="A", message_id=1
    )
    assert app.threads._thread_state_blocked_tools(channel_message) == THREAD_STATE_TOOLS

    conversation_id = await store.get_or_create("thread-state-tools", "general")
    await manager.enroll(321, conversation_id, creator_user_id="123")
    in_thread = _trigger_message(
        content="hi",
        author_id=1,
        author_name="A",
        message_id=2,
        channel=fake_thread_cls(321, []),
    )
    assert app.threads._thread_state_blocked_tools(in_thread) == frozenset(
        {"move_to_thread", "resume_thread_replies"}
    )

    await manager.pause(321)
    assert app.threads._thread_state_blocked_tools(in_thread) == frozenset(
        {"move_to_thread", "pause_thread_replies"}
    )


@pytest.mark.asyncio
async def test_move_to_thread_is_masked_on_forum_and_announcement_surfaces(
    monkeypatch, routing_database: Database
):
    app = _build_test_app(monkeypatch)
    _enable_thread_handoff(app, ConversationStore(routing_database))

    fake_forum_cls = type("_FakeForum", (_Channel,), {})
    monkeypatch.setattr(discord, "ForumChannel", fake_forum_cls)
    forum_message = _trigger_message(
        content="hi",
        author_id=1,
        author_name="A",
        message_id=1,
        channel=fake_forum_cls(400, []),
    )
    assert "move_to_thread" in app.threads._thread_state_blocked_tools(forum_message)

    class _FakeAnnouncement(_Channel):
        def is_news(self):
            return True

    monkeypatch.setattr(discord, "TextChannel", _FakeAnnouncement)
    announcement_message = _trigger_message(
        content="hi",
        author_id=1,
        author_name="A",
        message_id=2,
        channel=_FakeAnnouncement(500, []),
    )
    assert "move_to_thread" in app.threads._thread_state_blocked_tools(announcement_message)


@pytest.mark.asyncio
async def test_leave_thread_locks_and_archives_managed_thread(
    monkeypatch, routing_database: Database
):
    app = _build_test_app(monkeypatch)
    store = ConversationStore(routing_database)
    _wire_handle_message(monkeypatch, app, store)
    _enable_thread_handoff(app, store)

    root_key = "guild:999:channel:100:thread:main:root:1000"
    conv_id = await store.get_or_create(root_key, "general")
    await app.thread_handoff.enroll(321, conv_id, creator_user_id="123")

    fake_thread_cls = type("_FakeThread", (_Channel,), {})
    monkeypatch.setattr(discord, "Thread", fake_thread_cls)
    thread_channel = fake_thread_cls(321, [])
    thread_channel.edit = AsyncMock()

    async def fake_run_conversation(**kwargs):
        _conversation_call_kwargs(kwargs)[
            "context"
        ].pending_thread_close_request = ThreadCloseRequest(thread_id=321)
        return ConversationRunResult(text="closing!")

    monkeypatch.setattr(app_runtime, "run_conversation", fake_run_conversation)

    async def send_response(channel, content, reference=None, **kwargs):
        sent = MagicMock()
        sent.id = 888
        sent.content = content
        sent.created_at = datetime.fromtimestamp(888, tz=UTC)
        sent.channel = channel
        return [sent]

    monkeypatch.setattr(app, "send_response", send_response)

    message = _trigger_message(
        content="please close this",
        author_id=456,
        author_name="Bob",
        message_id=2000,
        channel=thread_channel,
    )

    await app.handle_message(message, lock_acquired=True)

    thread_channel.edit.assert_awaited_once_with(
        locked=True,
        archived=True,
        reason="Thread handoff closed",
    )
    assert await store.get_thread_conversation("321") is None
    assert not app.thread_handoff.is_managed(321)


@pytest.mark.asyncio
async def test_stale_thread_participation_falls_back_to_fresh_thread_root(
    monkeypatch, routing_database: Database
):
    app = _build_test_app(monkeypatch)
    store = ConversationStore(routing_database)
    _wire_handle_message(monkeypatch, app, store)
    _enable_thread_handoff(app, store)

    # Enrolled, then the row goes away underneath a running bot (a retention
    # sweep or a privacy deletion): the id is still live in memory.
    conversation_id = await store.get_or_create("stale-thread", "general")
    await app.thread_handoff.enroll(321, conversation_id, creator_user_id="123")
    await store.delete_thread_conversation("321")

    fake_thread_cls = type("_FakeThread", (_Channel,), {})
    monkeypatch.setattr(discord, "Thread", fake_thread_cls)
    thread_channel = fake_thread_cls(321, [])

    message = _trigger_message(
        content="<@999> hello?",
        author_id=456,
        author_name="Bob",
        message_id=2000,
        channel=thread_channel,
    )

    await app.handle_message(message, lock_acquired=True)

    context = _last_run_conversation_kwargs()["context"]
    assert context.key == "guild:999:channel:321:thread:321:root:2000"
    assert not app.thread_handoff.is_managed(321)


@pytest.mark.asyncio
async def test_on_message_delivery_failure_adds_failure_reaction(
    monkeypatch, routing_database: Database
):
    # A turn that produced a reply but delivered no chunk (send_response
    # swallows per-chunk HTTP failures) must react ❌, never ✅.
    from agent.turn import TurnResult

    app = _build_test_app(monkeypatch)
    store = ConversationStore(routing_database)
    app.context_manager = ContextManager(store)
    app.conversation_store = store
    app.blocked_user_store = NobodyBlocked()
    app.settings.allowed_channel_ids = ""
    monkeypatch.setattr(
        app_runtime,
        "should_respond",
        lambda message, *, bot_user, bot_name, responds_without_mention, allowed_channels=None, allowed_guilds=None: (
            True
        ),
    )
    monkeypatch.setattr(
        app,
        "handle_message",
        AsyncMock(return_value=TurnResult(response_text="hi", delivery_failed=True)),
    )

    message = _trigger_message(content="<@999> hello", author_id=456, author_name="Bob")

    await app.on_message(message)

    added = [call.args[0] for call in message.add_reaction.await_args_list]
    assert "❌" in added
    assert "✅" not in added


@pytest.mark.asyncio
async def test_on_message_attachment_error_adds_failure_reaction(
    monkeypatch, routing_database: Database
):
    app = _build_test_app(monkeypatch)
    store = ConversationStore(routing_database)
    app.context_manager = ContextManager(store)
    app.conversation_store = store
    app.blocked_user_store = NobodyBlocked()
    app.settings.allowed_channel_ids = ""
    monkeypatch.setattr(
        app_runtime,
        "should_respond",
        lambda message, *, bot_user, bot_name, responds_without_mention, allowed_channels=None, allowed_guilds=None: (
            True
        ),
    )
    monkeypatch.setattr(
        app,
        "handle_message",
        AsyncMock(
            return_value=TurnResult(
                response_text="I couldn't read that image.",
                termination_reason="attachment_error",
            )
        ),
    )

    message = _trigger_message(content="<@999> describe this", author_id=456, author_name="Bob")

    await app.on_message(message)

    added = [call.args[0] for call in message.add_reaction.await_args_list]
    assert "❌" in added
    assert "✅" not in added
