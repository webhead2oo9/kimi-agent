from __future__ import annotations

import asyncio
from pathlib import Path

import discord
import pytest
from discord.ext import commands
from pydantic import ValidationError

from commands.chat_cmd import register_user_app_chat_commands
from agent.turn import TurnResult, TurnTerminationReason
from app import runtime as app_runtime
from config.fragments.prompt import resolve_template_path
from config.settings import Settings
from storage.conversations import OWNER_ONLY, ConversationStore
from storage.db import Database
from trust.tiers import TrustTier
from trust.user_app import UserAppAccess
from tools.registry import MessageContext
from workspace import user_app_workspace_key
from tests.helpers import StubProviderManager


def test_user_app_access_uses_highest_overlapping_tier() -> None:
    access = UserAppAccess(
        member_ids=frozenset({"1", "2", "3"}),
        regular_ids=frozenset({"2", "3"}),
        staff_ids=frozenset({"3"}),
    )
    assert access.resolve("1") is TrustTier.MEMBER
    assert access.resolve("2") is TrustTier.REGULAR
    assert access.resolve("3") is TrustTier.STAFF
    assert access.resolve("4") is None


def test_user_app_settings_are_off_by_default_and_require_access() -> None:
    settings = Settings(
        user_app_chat_enabled=False,
        owner_user_id="",
        user_app_member_ids="",
        user_app_regular_ids="",
        user_app_staff_ids="",
    )
    assert settings.user_app_chat_enabled is False
    with pytest.raises(ValidationError, match="USER_APP_CHAT_ENABLED requires"):
        Settings(
            user_app_chat_enabled=True,
            owner_user_id="",
            user_app_member_ids="",
            user_app_regular_ids="",
            user_app_staff_ids="",
        )


def test_owner_is_automatically_user_app_staff() -> None:
    settings = Settings(
        user_app_chat_enabled=True,
        owner_user_id="42",
        user_app_member_ids="",
        user_app_regular_ids="",
        user_app_staff_ids="",
    )
    assert settings.user_app_staff_id_set == {"42"}


def test_user_app_workspace_is_global_per_user() -> None:
    assert user_app_workspace_key("123") == "123__userapp"
    assert user_app_workspace_key("123") != user_app_workspace_key("456")


def test_personal_tool_context_splits_location_from_data_scope() -> None:
    workspace = user_app_workspace_key("123")
    context = MessageContext(
        user_id="123",
        user_name="Alice",
        guild_id=None,
        channel_id="physical-channel",
        thread_id="physical-thread",
        trust_tier=TrustTier.REGULAR,
        workspace_key_override=workspace,
        personal_chat=True,
        platform_guild_id="physical-guild",
    )
    # The logical scope is guild-less; the physical location is metadata only.
    assert context.guild_id is None
    assert context.platform_guild_id == "physical-guild"
    assert context.channel_id == "physical-channel"
    assert context.thread_id == "physical-thread"
    assert context.workspace_key == workspace
    assert context.conversation_channel_id == "userapp"


def test_local_command_prompt_wins_over_tracked_default(tmp_path: Path) -> None:
    command_dir = tmp_path / "prompts" / "commands"
    command_dir.mkdir(parents=True)
    (tmp_path / "prompt.md").write_text("base", encoding="utf-8")
    tracked = command_dir / "chat.md"
    local = command_dir / "chat.local.md"
    tracked.write_text("tracked", encoding="utf-8")
    local.write_text("local", encoding="utf-8")
    assert (
        resolve_template_path(
            tmp_path,
            channel_id="",
            guild_id="",
            command_template="chat",
        )
        == local
    )


def test_chat_commands_are_user_install_only() -> None:
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())

    async def run_chat(*_args: object) -> None:
        return None

    async def reset_chat(_interaction: discord.Interaction) -> str:
        return "ok"

    register_user_app_chat_commands(bot, run_chat=run_chat, reset_chat=reset_chat)
    for name in ("chat", "chat-reset"):
        command = bot.tree.get_command(name)
        assert command is not None
        installs = command.allowed_installs
        contexts = command.allowed_contexts
        assert installs is not None and installs.guild is False and installs.user is True
        assert contexts is not None
        assert contexts.guild is True
        assert contexts.dm_channel is True
        assert contexts.private_channel is True


@pytest.mark.parametrize(
    ("requested_public", "termination_reason", "blocked", "expected"),
    [
        (True, "completed", False, True),
        (False, "completed", False, False),
        (True, "moderation_blocked", True, False),
        (True, "provider_error", False, False),
        (True, "timed_out", False, False),
        (True, "max_iterations", False, False),
        (True, "completed", True, False),
    ],
)
def test_only_successful_user_app_results_can_be_public(
    requested_public: bool,
    termination_reason: TurnTerminationReason,
    blocked: bool,
    expected: bool,
) -> None:
    result = TurnResult(
        response_text="result",
        termination_reason=termination_reason,
        blocked_by_moderation=blocked,
    )
    assert (
        app_runtime._should_publish_user_app_result(
            result,
            requested_public=requested_public,
        )
        is expected
    )


@pytest.mark.asyncio
async def test_chat_reset_deletes_only_exact_owner_root(tmp_path: Path) -> None:
    db = Database(tmp_path / "chat-reset.db")
    await db.connect()
    try:
        store = ConversationStore(db)
        mine = await store.get_or_create(
            "userchat:1",
            "Personal chat",
            owner_user_id="1",
            access_scope=OWNER_ONLY,
        )
        theirs = await store.get_or_create(
            "userchat:2",
            "Personal chat",
            owner_user_id="2",
            access_scope=OWNER_ONLY,
        )
        assert mine != theirs
        assert await store.delete_owner_conversation("userchat:1", "2") is False
        assert await store.delete_owner_conversation("userchat:1", "1") is True
        async with db.conn.execute("SELECT key FROM conversations ORDER BY key") as cursor:
            keys = [str(row["key"]) for row in await cursor.fetchall()]
        assert keys == ["userchat:2"]
        assert await store.delete_owner_conversation("userchat:1", "1") is False
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_runtime_registers_user_commands_only_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        app_runtime,
        "build_provider_manager",
        lambda settings: StubProviderManager(settings),
    )

    off = app_runtime.build_app(
        Settings.model_validate(
            {
                "discord_bot_token": "token",
                "model_api_key": "key",
                "config_dir": str(tmp_path / "off-config"),
                "database_path": str(tmp_path / "off.db"),
                "user_app_chat_enabled": False,
            }
        )
    )
    await off._first_init_core()
    assert off.bot.tree.get_command("chat") is None
    assert off.bot.tree.get_command("chat-reset") is None
    assert off.bot.tree.allowed_installs.guild is True
    assert off.bot.tree.allowed_installs.user is False
    await off.database.close()

    on = app_runtime.build_app(
        Settings.model_validate(
            {
                "discord_bot_token": "token",
                "model_api_key": "key",
                "config_dir": str(tmp_path / "on-config"),
                "database_path": str(tmp_path / "on.db"),
                "user_app_chat_enabled": True,
                "owner_user_id": "42",
            }
        )
    )
    await on._first_init_core()
    for name in ("chat", "chat-reset", "privacy", "memory", "stop"):
        command = on.bot.tree.get_command(name)
        assert command is not None
        assert command.allowed_installs is not None
        assert command.allowed_installs.user is True
    await on.database.close()


@pytest.mark.asyncio
async def test_reset_drains_full_chat_lifecycle_and_invalidates_older_requests(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        app_runtime,
        "build_provider_manager",
        lambda settings: StubProviderManager(settings),
    )
    app = app_runtime.build_app(
        Settings.model_validate(
            {
                "discord_bot_token": "token",
                "model_api_key": "key",
                "config_dir": str(tmp_path / "config"),
                "database_path": str(tmp_path / "runtime.db"),
                "user_app_chat_enabled": True,
                "owner_user_id": "42",
            }
        )
    )
    await app._first_init_core()

    class FakeInteraction:
        def __init__(self, interaction_id: int) -> None:
            self.id = interaction_id
            self.user = type("User", (), {"id": 42, "display_name": "Alice"})()
            self.edits: list[str] = []

        async def edit_original_response(self, **kwargs: object) -> None:
            self.edits.append(str(kwargs.get("content", "")))

    entered_delivery = asyncio.Event()
    calls = 0

    async def wait_in_delivery(_interaction: object, **_kwargs: object) -> None:
        nonlocal calls
        calls += 1
        entered_delivery.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(app, "_run_user_app_chat_turn", wait_in_delivery)
    interaction = FakeInteraction(1)
    generation = app._user_app_chat_generation("42")
    task = asyncio.create_task(
        app._execute_user_app_chat(
            interaction,  # type: ignore[arg-type]
            message="hello",
            attachment=None,
            public=True,
            request_generation=generation,
        )
    )
    await entered_delivery.wait()

    summary = await app._handle_user_app_chat_reset(interaction)  # type: ignore[arg-type]
    await task

    assert summary == "Your personal chat thread is already clear."
    assert interaction.edits == ["Stopped."]
    assert calls == 1

    stale = FakeInteraction(2)
    await app._execute_user_app_chat(
        stale,  # type: ignore[arg-type]
        message="old retained request",
        attachment=None,
        public=False,
        request_generation=generation,
    )
    assert calls == 1
    assert "expired because your personal thread was reset or deleted" in stale.edits[-1]
    await app.database.close()


@pytest.mark.asyncio
async def test_stop_current_reaches_personal_chat_for_dual_installed_app(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A guild+user installed app reports both integration owners, so /stop must
    still reach the personal root instead of only the invoking channel."""
    monkeypatch.setattr(
        app_runtime,
        "build_provider_manager",
        lambda settings: StubProviderManager(settings),
    )
    app = app_runtime.build_app(
        Settings.model_validate(
            {
                "discord_bot_token": "token",
                "model_api_key": "key",
                "config_dir": str(tmp_path / "config"),
                "database_path": str(tmp_path / "runtime.db"),
                "user_app_chat_enabled": True,
                "owner_user_id": "42",
            }
        )
    )
    await app._first_init_core()

    recorded: list[tuple[str, str | None, bool]] = []

    async def record_cancel(
        *,
        user_id: str,
        root_key: str | None,
        channel_id: str,
        all_operations: bool,
        wait_seconds: float,
    ) -> tuple[int, bool]:
        recorded.append((channel_id, root_key, all_operations))
        return 0, True

    monkeypatch.setattr(app.active_operations, "cancel", record_cancel)

    class FakeInteraction:
        def __init__(self, *, user_install: bool, guild_install: bool) -> None:
            self.user = type("User", (), {"id": 42, "display_name": "Alice"})()
            self.channel_id = 555
            self.guild_id = 777
            self._user_install = user_install
            self._guild_install = guild_install

        def is_user_integration(self) -> bool:
            return self._user_install

        def is_guild_integration(self) -> bool:
            return self._guild_install

    dual = FakeInteraction(user_install=True, guild_install=True)
    await app._handle_stop_interaction(dual, False, None)  # type: ignore[arg-type]
    assert ("userapp", "userchat:42", False) in recorded
    assert ("555", None, False) in recorded

    recorded.clear()
    user_only = FakeInteraction(user_install=True, guild_install=False)
    await app._handle_stop_interaction(user_only, False, None)  # type: ignore[arg-type]
    assert recorded == [("userapp", "userchat:42", False)]

    recorded.clear()
    guild_only = FakeInteraction(user_install=False, guild_install=True)
    await app._handle_stop_interaction(guild_only, False, None)  # type: ignore[arg-type]
    assert recorded == [("555", None, False)]

    await app.database.close()


@pytest.mark.asyncio
async def test_personal_chat_cancellation_propagates_during_shutdown(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Shutdown cancels every active operation; the personal chat handler must not
    absorb that cancellation or await another edit on the closing client."""
    monkeypatch.setattr(
        app_runtime,
        "build_provider_manager",
        lambda settings: StubProviderManager(settings),
    )
    app = app_runtime.build_app(
        Settings.model_validate(
            {
                "discord_bot_token": "token",
                "model_api_key": "key",
                "config_dir": str(tmp_path / "config"),
                "database_path": str(tmp_path / "runtime.db"),
                "user_app_chat_enabled": True,
                "owner_user_id": "42",
            }
        )
    )
    await app._first_init_core()

    class FakeInteraction:
        def __init__(self) -> None:
            self.id = 1
            self.user = type("User", (), {"id": 42, "display_name": "Alice"})()
            self.edits: list[str] = []

        async def edit_original_response(self, **kwargs: object) -> None:
            self.edits.append(str(kwargs.get("content", "")))

    entered = asyncio.Event()

    async def wait_forever(_interaction: object, **_kwargs: object) -> None:
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(app, "_run_user_app_chat_turn", wait_forever)
    interaction = FakeInteraction()
    task = asyncio.create_task(
        app._execute_user_app_chat(
            interaction,  # type: ignore[arg-type]
            message="hello",
            attachment=None,
            public=False,
            request_generation=app._user_app_chat_generation("42"),
        )
    )
    await entered.wait()

    app._closed = True
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()
    assert interaction.edits == []

    await app.database.close()


def _personal_context(**overrides: object) -> MessageContext:
    base: dict[str, object] = {
        "user_id": "123",
        "user_name": "Alice",
        # Guild-less by design; the physical location travels separately.
        "guild_id": None,
        "channel_id": "physical-channel",
        "thread_id": None,
        "trust_tier": TrustTier.STAFF,
        "workspace_key_override": user_app_workspace_key("123"),
        "personal_chat": True,
        "platform_guild_id": "physical-guild",
    }
    base.update(overrides)
    return MessageContext(**base)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_personal_chat_cannot_dispatch_a_guild_scoped_tool() -> None:
    """Trust from USER_APP_* is granted outside any guild, so it must not unlock
    a tool scoped to the guild the slash command happened to be invoked from."""
    from tools.registry import ToolRegistry

    registry = ToolRegistry()

    async def handler(_args: dict, _ctx: MessageContext) -> str:
        return "ran"

    registry.register(
        name="guild_scoped_tool",
        description="scoped",
        parameters={},
        handler=handler,
        min_tier=TrustTier.MEMBER,
        guild_ids=frozenset({"physical-guild"}),
    )

    personal = _personal_context()
    # Masked as unknown so a scoped tool's existence never leaks.
    assert "Unknown tool: guild_scoped_tool" in await registry.dispatch(
        "guild_scoped_tool", {}, personal
    )

    # The same user in that guild for real still reaches it.
    in_guild = _personal_context(
        guild_id="physical-guild",
        personal_chat=False,
        workspace_key_override=None,
    )
    assert await registry.dispatch("guild_scoped_tool", {}, in_guild) == "ran"


def test_personal_chat_blocks_guild_artifact_tools() -> None:
    """Community memory and shared skills belong to a guild or the deployment.
    A guild-less personal turn has no coherent target for them."""
    from app.turn_entry import _PERSONAL_CHAT_BLOCKED_TOOLS

    for name in ("teach", "recall_community", "reflect_community"):
        assert name in _PERSONAL_CHAT_BLOCKED_TOOLS
    for name in ("skill_create", "skill_edit", "skill_delete"):
        assert name in _PERSONAL_CHAT_BLOCKED_TOOLS


@pytest.mark.asyncio
async def test_skill_create_refuses_personal_chat_instead_of_going_global() -> None:
    """Fail-closed second layer behind the denylist: a guild-less context must
    never fall into the "no guild means every guild" branch."""
    from tools import skills as skills_tools

    result = await skills_tools._skill_create(
        {"name": "x", "description": "d", "content": "c"},
        _personal_context(),
    )
    assert "only be managed from a server conversation" in result
