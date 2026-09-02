from __future__ import annotations

import asyncio
import base64
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import discord
import pytest

from app import message_runtime
from discord.ext import commands
from pydantic import ValidationError

from agent.activity import ActivityUpdate
from agent.core import ConversationRunRequest, ConversationRunResult
from agent.turn import TurnResult
from app import user_app_chat as user_app_chat_module
from app import user_app_turn_adapter
from commands.chat_cmd import register_user_app_chat_commands
from app import runtime as app_runtime
from config.fragments.prompt import resolve_template_path
from config.settings import Settings
from discord_adapter.interaction_io import (
    PartialPublicDeliveryError,
    send_interaction_result,
    send_interaction_status,
)
from storage.conversations import OWNER_ONLY, ConversationStore
from storage.db import Database
from providers.types import ContentPartType, ProviderCapability
from trust.tiers import TrustTier
from trust.user_app import UserAppAccess
from tools.registry import MessageContext
from workspace import user_app_workspace_key
from tests.helpers import (
    LifecycleProbe,
    PersonalChatDriver,
    StubProviderManager,
    install_foreground_turn_handler,
    make_settings,
)


_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9aw"
    "AAAABJRU5ErkJggg=="
)


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
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        user_app_chat_enabled=False,
        user_app_dm_enabled=False,
        owner_user_id="",
        user_app_member_ids="",
        user_app_regular_ids="",
        user_app_staff_ids="",
    )
    assert settings.user_app_chat_enabled is False
    with pytest.raises(ValidationError, match="USER_APP_CHAT_ENABLED requires"):
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            user_app_chat_enabled=True,
            owner_user_id="",
            user_app_member_ids="",
            user_app_regular_ids="",
            user_app_staff_ids="",
        )


def test_owner_is_automatically_user_app_staff() -> None:
    settings = make_settings(
        user_app_chat_enabled=True,
        owner_user_id="42",
        user_app_member_ids="",
        user_app_regular_ids="",
        user_app_staff_ids="",
    )
    assert settings.user_app_staff_id_set == {"42"}


def test_owner_user_id_rejects_multiple_ids() -> None:
    with pytest.raises(ValidationError, match="OWNER_USER_ID must be one numeric"):
        Settings(_env_file=None, owner_user_id="1,2")  # type: ignore[call-arg]
    settings = Settings(_env_file=None, owner_user_id=" 42 ")  # type: ignore[call-arg]
    assert settings.owner_user_id == "42"


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

    register_user_app_chat_commands(
        bot,
        run_chat=run_chat,
        reset_chat=reset_chat,
        bot_name="Kimi",
    )
    chat_command = cast(Any, bot.tree.get_command("chat"))
    assert chat_command is not None
    assert chat_command.description == "Chat with Kimi"
    parameters = {parameter.name: parameter for parameter in chat_command.parameters}
    assert set(parameters) == {"message", "attachment", "visibility"}
    assert parameters["visibility"].required is False
    assert parameters["visibility"].default == "private"
    assert [(choice.name, choice.value) for choice in parameters["visibility"].choices] == [
        ("Only me", "private"),
        ("Everyone", "public"),
    ]
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


def test_chat_command_description_respects_discord_limit() -> None:
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())

    async def run_chat(*_args: object) -> None:
        return None

    async def reset_chat(_interaction: discord.Interaction) -> str:
        return "ok"

    register_user_app_chat_commands(
        bot,
        run_chat=run_chat,
        reset_chat=reset_chat,
        bot_name="K" * 100,
    )

    command = cast(Any, bot.tree.get_command("chat"))
    assert command is not None
    assert command.description == f"Chat with {'K' * 90}"
    assert len(command.description) == 100


def test_user_app_public_post_uses_invoking_members_permissions() -> None:
    interaction = SimpleNamespace(
        guild_id=777,
        channel=object(),
        permissions=SimpleNamespace(
            send_messages=True,
            use_external_apps=True,
        ),
        app_permissions=SimpleNamespace(send_messages=False),
        is_user_integration=lambda: True,
        is_guild_integration=lambda: False,
    )

    assert (
        user_app_chat_module.interaction_can_post_publicly(cast(discord.Interaction, interaction))
        is True
    )


@pytest.mark.parametrize(
    ("send_messages", "use_external_apps"),
    [(False, True), (True, False)],
)
def test_user_app_public_post_requires_member_channel_and_external_app_permissions(
    send_messages: bool,
    use_external_apps: bool,
) -> None:
    interaction = SimpleNamespace(
        guild_id=777,
        channel=object(),
        permissions=SimpleNamespace(
            send_messages=send_messages,
            use_external_apps=use_external_apps,
        ),
        app_permissions=SimpleNamespace(send_messages=True),
        is_user_integration=lambda: True,
        is_guild_integration=lambda: False,
    )

    assert (
        user_app_chat_module.interaction_can_post_publicly(cast(discord.Interaction, interaction))
        is False
    )


@pytest.mark.parametrize(
    ("send_messages_in_threads", "expected"),
    [(True, True), (False, False)],
)
def test_user_app_public_post_uses_thread_permission(
    monkeypatch: pytest.MonkeyPatch,
    send_messages_in_threads: bool,
    expected: bool,
) -> None:
    class FakeThread:
        pass

    monkeypatch.setattr(discord, "Thread", FakeThread)
    interaction = SimpleNamespace(
        guild_id=777,
        channel=FakeThread(),
        permissions=SimpleNamespace(
            send_messages=False,
            send_messages_in_threads=send_messages_in_threads,
            use_external_apps=True,
        ),
        app_permissions=SimpleNamespace(send_messages_in_threads=False),
        is_user_integration=lambda: True,
        is_guild_integration=lambda: False,
    )

    assert (
        user_app_chat_module.interaction_can_post_publicly(cast(discord.Interaction, interaction))
        is expected
    )


def test_dual_installed_app_public_post_uses_application_permissions() -> None:
    interaction = SimpleNamespace(
        guild_id=777,
        channel=object(),
        permissions=SimpleNamespace(
            send_messages=False,
            use_external_apps=False,
        ),
        app_permissions=SimpleNamespace(send_messages=True),
        is_user_integration=lambda: True,
        is_guild_integration=lambda: True,
    )

    assert (
        user_app_chat_module.interaction_can_post_publicly(cast(discord.Interaction, interaction))
        is True
    )


def test_partial_integration_markers_do_not_grant_user_install_permissions() -> None:
    interaction = SimpleNamespace(
        guild_id=777,
        channel=object(),
        permissions=SimpleNamespace(
            send_messages=True,
            use_external_apps=True,
        ),
        app_permissions=SimpleNamespace(send_messages=False),
        is_user_integration=lambda: True,
    )

    assert (
        user_app_chat_module.interaction_can_post_publicly(cast(discord.Interaction, interaction))
        is False
    )


def test_user_app_public_post_is_allowed_outside_guilds() -> None:
    interaction = SimpleNamespace(guild_id=None)

    assert (
        user_app_chat_module.interaction_can_post_publicly(cast(discord.Interaction, interaction))
        is True
    )


@pytest.mark.asyncio
async def test_chat_visibility_choice_maps_to_internal_public_flag() -> None:
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
    calls: list[bool] = []

    async def run_chat(
        _interaction: discord.Interaction,
        _message: str,
        _attachment: discord.Attachment | None,
        public: bool,
    ) -> None:
        calls.append(public)

    async def reset_chat(_interaction: discord.Interaction) -> str:
        return "ok"

    register_user_app_chat_commands(
        bot,
        run_chat=run_chat,
        reset_chat=reset_chat,
        bot_name="Kimi",
    )
    command = cast(Any, bot.tree.get_command("chat"))
    assert command is not None
    interaction = cast(discord.Interaction, object())

    await command.callback(interaction, "private")
    await command.callback(
        interaction,
        "public",
        visibility="public",
    )

    assert calls == [False, True]


class _UserAppImageAttachment:
    def __init__(self, *, unreadable: bool = False) -> None:
        self.filename = "photo.png"
        self.content_type = "application/octet-stream"
        self._payload = _PNG_1X1
        self.size = len(self._payload)
        self.unreadable = unreadable
        self.read_count = 0

    async def read(self) -> bytes:
        self.read_count += 1
        if self.unreadable:
            raise OSError("attachment expired")
        return self._payload


class _UserAppInteractionResponse:
    def __init__(self) -> None:
        self.deferred: list[dict[str, object]] = []
        self.sent: list[dict[str, object]] = []

    async def defer(self, **kwargs: object) -> None:
        self.deferred.append(kwargs)

    async def send_message(self, content: object = None, **kwargs: object) -> None:
        kwargs["content"] = content
        self.sent.append(kwargs)


class _UserAppImageInteraction:
    def __init__(self, interaction_id: int = 7, *, file_size_limit: int | None = None) -> None:
        self.id = interaction_id
        self.user = SimpleNamespace(id=42, display_name="Alice")
        guild = (
            SimpleNamespace(filesize_limit=file_size_limit) if file_size_limit is not None else None
        )
        self.channel = SimpleNamespace(guild=guild)
        self.channel_id = 99
        self.guild = None
        self.guild_id = None
        self.created_at = datetime.now(UTC)
        self.response = _UserAppInteractionResponse()
        self.edits: list[dict[str, object]] = []

    async def edit_original_response(self, **kwargs: object) -> None:
        self.edits.append(kwargs)


async def _user_app_chat_app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    **setting_overrides: object,
) -> Any:
    monkeypatch.setattr(
        app_runtime,
        "build_provider_manager",
        lambda settings: StubProviderManager(settings),
    )
    settings_values: dict[str, object] = {
        "discord_bot_token": "token",
        "model_api_key": "key",
        "config_dir": str(tmp_path / "config"),
        "database_path": str(tmp_path / "runtime.db"),
        "workspace_dir": str(tmp_path / "workspaces"),
        "attachment_store_dir": str(tmp_path / "attachments"),
        "user_app_chat_enabled": True,
        "owner_user_id": "42",
    }
    settings_values.update(setting_overrides)
    app = app_runtime.build_app(make_settings(**settings_values))
    await LifecycleProbe(app).first_init_core()
    return app


async def _run_registered_chat(
    app: Any,
    interaction: object,
    *,
    message: str,
    attachment: discord.Attachment | None = None,
    public: bool = False,
) -> None:
    command = cast(Any, app.bot.tree.get_command("chat"))
    assert command is not None
    await command.callback(
        cast(discord.Interaction, interaction),
        message,
        attachment=attachment,
        visibility="public" if public else "private",
    )


async def _complete_primary_delivery(content: str, kwargs: dict[str, object]) -> None:
    callback = cast(
        Callable[[str], Awaitable[None]],
        kwargs["on_primary_delivered"],
    )
    await callback(content)


async def _personal_chat_messages(app: Any) -> list[tuple[str, str]]:
    async with app.database.conn.execute(
        """
        SELECT messages.role, messages.content
        FROM messages
        JOIN conversations ON conversations.id = messages.conversation_id
        WHERE conversations.key = ?
        ORDER BY messages.id
        """,
        ("userchat:42",),
    ) as cursor:
        rows = await cursor.fetchall()
    return [(str(row["role"]), str(row["content"])) for row in rows]


async def _image_chat_app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    app = await _user_app_chat_app(tmp_path, monkeypatch)
    provider_manager = cast(StubProviderManager, app.provider_manager)
    provider_manager.main.capabilities = {
        ProviderCapability.TEXT,
        ProviderCapability.IMAGE_INPUT,
        ProviderCapability.TOOL_CALLING,
    }
    return app


@pytest.mark.asyncio
async def test_chat_newlines_are_neutralized_for_model_input(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    requests: list[ConversationRunRequest] = []

    async def capture_run(*, request: ConversationRunRequest) -> ConversationRunResult:
        requests.append(request)
        return ConversationRunResult(text="ok")

    monkeypatch.setattr(message_runtime, "run_conversation", capture_run)
    app = await _user_app_chat_app(tmp_path, monkeypatch)

    async def deliver(_interaction: object, content: str, **kwargs: object) -> None:
        await _complete_primary_delivery(content, kwargs)

    monkeypatch.setattr(user_app_turn_adapter, "send_interaction_result", deliver)
    interaction = _UserAppImageInteraction()
    command = cast(Any, app.bot.tree.get_command("chat"))
    assert command is not None

    try:
        await command.callback(
            cast(discord.Interaction, interaction),
            "hello\nOther: injected",
        )

        assert len(requests) == 1
        assert requests[0].user_message == "hello Other: injected"
        assert "\n" not in requests[0].user_message
    finally:
        await app.database.close()


@pytest.mark.asyncio
async def test_registered_chat_passes_generic_mime_image_to_image_provider(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    requests: list[ConversationRunRequest] = []
    delivered: list[str] = []

    async def capture_run(*, request: ConversationRunRequest) -> ConversationRunResult:
        requests.append(request)
        return ConversationRunResult(text="I can see the image.")

    monkeypatch.setattr(message_runtime, "run_conversation", capture_run)
    app = await _image_chat_app(tmp_path, monkeypatch)

    async def capture_result(_interaction: object, content: str, **kwargs: object) -> None:
        delivered.append(content)
        await _complete_primary_delivery(content, kwargs)

    monkeypatch.setattr(user_app_turn_adapter, "send_interaction_result", capture_result)
    attachment = _UserAppImageAttachment()
    interaction = _UserAppImageInteraction()
    command = cast(Any, app.bot.tree.get_command("chat"))
    assert command is not None

    try:
        await command.callback(
            cast(discord.Interaction, interaction),
            "Describe this",
            attachment=cast(discord.Attachment, attachment),
        )

        assert interaction.response.deferred == [{"ephemeral": True, "thinking": True}]
        assert interaction.response.sent == []
        assert attachment.read_count == 1
        assert delivered == ["I can see the image."]
        assert len(requests) == 1
        provider_manager = cast(StubProviderManager, app.provider_manager)
        assert requests[0].provider is provider_manager.main
        input_parts = requests[0].input_parts
        assert input_parts is not None
        assert len(input_parts) == 1
        image_part = input_parts[0]
        assert image_part.type is ContentPartType.IMAGE
        assert image_part.media_type == "image/png"
        assert image_part.image_url is not None
        header, encoded = image_part.image_url.split(",", maxsplit=1)
        assert header == "data:image/png;base64"
        assert base64.b64decode(encoded, validate=True) == _PNG_1X1
        assert not list((tmp_path / "attachments").rglob("photo.png"))
    finally:
        await app.database.close()


@pytest.mark.asyncio
async def test_registered_chat_reports_unreadable_image_without_provider_or_transcript(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    requests: list[ConversationRunRequest] = []
    delivered: list[str] = []

    async def capture_run(*, request: ConversationRunRequest) -> ConversationRunResult:
        requests.append(request)
        return ConversationRunResult(text="unexpected")

    monkeypatch.setattr(message_runtime, "run_conversation", capture_run)
    app = await _image_chat_app(tmp_path, monkeypatch)

    async def capture_result(_interaction: object, content: str, **kwargs: object) -> None:
        delivered.append(content)
        await _complete_primary_delivery(content, kwargs)

    monkeypatch.setattr(user_app_turn_adapter, "send_interaction_result", capture_result)
    attachment = _UserAppImageAttachment(unreadable=True)
    interaction = _UserAppImageInteraction()
    command = cast(Any, app.bot.tree.get_command("chat"))
    assert command is not None

    try:
        await command.callback(
            cast(discord.Interaction, interaction),
            "Describe this",
            attachment=cast(discord.Attachment, attachment),
        )

        assert attachment.read_count == 1
        assert requests == []
        assert delivered == [
            (
                "I couldn't read the attached image. Re-upload it as a valid PNG, JPEG, GIF, "
                "or WebP within the attachment size limit."
            )
        ]
        async with app.database.conn.execute(
            """
            SELECT messages.role
            FROM messages
            JOIN conversations ON conversations.id = messages.conversation_id
            WHERE conversations.key = ?
            """,
            ("userchat:42",),
        ) as cursor:
            rows = await cursor.fetchall()
        assert rows == []
        assert not list((tmp_path / "attachments").rglob("photo.png"))
    finally:
        await app.database.close()


@pytest.mark.asyncio
async def test_public_result_edits_original_then_uses_public_followups() -> None:
    class Followup:
        def __init__(self) -> None:
            self.messages: list[dict[str, object]] = []

        async def send(self, content: object = None, **kwargs: object) -> None:
            kwargs["content"] = content
            self.messages.append(kwargs)

    class FakeInteraction:
        def __init__(self) -> None:
            self.channel = object()
            self.followup = Followup()
            self.edits: list[dict[str, object]] = []
            self.deleted = False

        async def edit_original_response(self, **kwargs: object) -> None:
            self.edits.append(kwargs)

        async def delete_original_response(self) -> None:
            self.deleted = True

    interaction = FakeInteraction()
    await send_interaction_result(
        interaction,  # type: ignore[arg-type]
        "A" * 2500,
        ephemeral=False,
        original_ephemeral=False,
    )

    assert interaction.deleted is False
    assert len(interaction.edits) == 1
    assert str(interaction.edits[0]["content"]).startswith("A")
    assert interaction.edits[0]["content"] != "Posted the response publicly."
    assert len(interaction.followup.messages) == 1
    assert interaction.followup.messages[0]["ephemeral"] is False


@pytest.mark.asyncio
async def test_partial_public_delivery_keeps_primary_response() -> None:
    class Followup:
        def __init__(self) -> None:
            self.calls = 0

        async def send(self, _content: object = None, **_kwargs: object) -> None:
            self.calls += 1
            if self.calls == 2:
                response = SimpleNamespace(status=500, reason="Server Error")
                raise discord.HTTPException(response, "followup failed")  # type: ignore[arg-type]

    class FakeInteraction:
        def __init__(self) -> None:
            self.channel = object()
            self.followup = Followup()
            self.edits: list[dict[str, object]] = []
            self.deleted = False

        async def edit_original_response(self, **kwargs: object) -> None:
            self.edits.append(kwargs)

        async def delete_original_response(self) -> None:
            self.deleted = True

    interaction = FakeInteraction()
    with pytest.raises(PartialPublicDeliveryError):
        await send_interaction_result(
            interaction,  # type: ignore[arg-type]
            "A" * 4500,
            ephemeral=False,
            original_ephemeral=False,
        )

    assert interaction.deleted is False
    assert len(interaction.edits) == 1
    assert str(interaction.edits[0]["content"]).startswith("A")


@pytest.mark.asyncio
async def test_private_status_replaces_public_placeholder_with_followup() -> None:
    class Followup:
        def __init__(self) -> None:
            self.messages: list[dict[str, object]] = []

        async def send(self, content: object = None, **kwargs: object) -> None:
            kwargs["content"] = content
            self.messages.append(kwargs)

    class FakeInteraction:
        def __init__(self) -> None:
            self.followup = Followup()
            self.edits: list[dict[str, object]] = []
            self.deleted = False

        async def edit_original_response(self, **kwargs: object) -> None:
            self.edits.append(kwargs)

        async def delete_original_response(self) -> None:
            self.deleted = True

    interaction = FakeInteraction()
    await send_interaction_status(
        interaction,  # type: ignore[arg-type]
        "Private failure",
        ephemeral=True,
        original_ephemeral=False,
    )

    assert interaction.deleted is True
    assert interaction.edits == []
    assert interaction.followup.messages[0]["content"] == "Private failure"
    assert interaction.followup.messages[0]["ephemeral"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("requested_public", [False, True])
async def test_user_app_status_keeps_selected_visibility(
    monkeypatch: pytest.MonkeyPatch,
    requested_public: bool,
) -> None:
    calls: list[tuple[bool, bool]] = []

    async def send_status(
        _interaction: object,
        _content: str,
        *,
        ephemeral: bool,
        original_ephemeral: bool,
    ) -> None:
        calls.append((ephemeral, original_ephemeral))

    monkeypatch.setattr(user_app_chat_module, "send_interaction_status", send_status)

    await user_app_chat_module._send_user_app_status(
        cast(discord.Interaction, object()),
        "Turn failed.",
        requested_public=requested_public,
    )

    assert calls == [(not requested_public, not requested_public)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "expected", "sends_result"),
    [
        ("result", "The provider failed after I started.", True),
        ("exception", "I couldn't complete that chat turn. Please try again.", False),
        ("timeout", "That personal chat turn timed out. Run `/chat` again to retry.", False),
        ("cancel", "Stopped.", False),
    ],
)
async def test_public_personal_chat_finishes_activity_before_every_outcome(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    outcome: str,
    expected: str,
    sends_result: bool,
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
                "user_app_chat_timeout_seconds": 1 if outcome == "timeout" else 840,
                "owner_user_id": "42",
            }
        )
    )
    await LifecycleProbe(app).first_init_core()

    class FakeInteraction:
        def __init__(self) -> None:
            self.id = 7
            self.user = SimpleNamespace(id=42, display_name="Alice")
            self.channel = SimpleNamespace(guild=None)
            self.channel_id = 99
            self.guild = None
            self.guild_id = None
            self.created_at = datetime.now(UTC)
            self.response = _UserAppInteractionResponse()
            self.edits: list[str] = []

        async def edit_original_response(self, **kwargs: object) -> None:
            self.edits.append(str(kwargs.get("content", "")))

    reporters: list[Any] = []
    turn_started = asyncio.Event()

    async def failed_turn(
        _turn_input: object,
        *,
        dependencies: object,
        **_kwargs: object,
    ) -> TurnResult:
        reporter = dependencies.activity_reporter  # type: ignore[attr-defined]
        assert reporter is not None
        reporters.append(reporter)
        await reporter(ActivityUpdate(label="Thinking..."))
        turn_started.set()
        if outcome == "result":
            return TurnResult(
                response_text=expected,
                termination_reason="provider_error",
            )
        if outcome == "exception":
            raise RuntimeError("provider exploded")
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    delivered: list[tuple[bool, bool]] = []
    real_send_result = user_app_turn_adapter.send_interaction_result

    async def record_result(*args: object, **kwargs: object) -> None:
        delivered.append((bool(kwargs["ephemeral"]), bool(kwargs["original_ephemeral"])))
        await real_send_result(*args, **kwargs)  # type: ignore[arg-type]

    install_foreground_turn_handler(app, failed_turn)
    monkeypatch.setattr(user_app_turn_adapter, "send_interaction_result", record_result)
    interaction = FakeInteraction()

    try:
        task = asyncio.create_task(
            _run_registered_chat(
                app,
                interaction,
                message="hello",
                public=True,
            )
        )
        if outcome == "cancel":
            await turn_started.wait()
            task.cancel()
        await task
        assert reporters
        await reporters[0](ActivityUpdate(label="Late update"))

        assert interaction.edits == [
            "Thinking...",
            expected,
        ]
        assert delivered == ([(False, False)] if sends_result else [])
    finally:
        await app.database.close()


@pytest.mark.asyncio
async def test_same_root_chat_delivery_finishes_before_next_turn_starts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app = await _user_app_chat_app(tmp_path, monkeypatch)
    events: list[str] = []
    first_body_started = asyncio.Event()
    release_first_body = asyncio.Event()
    first_delivery_started = asyncio.Event()
    release_first_delivery = asyncio.Event()
    turn_count = 0

    async def run_turn(*_args: object, **_kwargs: object) -> TurnResult:
        nonlocal turn_count
        turn_count += 1
        turn_number = turn_count
        events.append(f"turn:{turn_number}:start")
        if turn_number == 1:
            first_body_started.set()
            await release_first_body.wait()
        events.append(f"turn:{turn_number}:end")
        return TurnResult(response_text=f"reply-{turn_number}")

    async def deliver(_interaction: object, content: str, **kwargs: object) -> None:
        events.append(f"deliver:{content}:start")
        if content == "reply-1":
            first_delivery_started.set()
            await release_first_delivery.wait()
        await _complete_primary_delivery(content, kwargs)
        events.append(f"deliver:{content}:end")

    install_foreground_turn_handler(app, run_turn)
    monkeypatch.setattr(user_app_turn_adapter, "send_interaction_result", deliver)
    first = asyncio.create_task(
        _run_registered_chat(
            app,
            _UserAppImageInteraction(7),
            message="first",
            public=False,
        )
    )
    await asyncio.wait_for(first_body_started.wait(), timeout=0.5)
    second = asyncio.create_task(
        _run_registered_chat(
            app,
            _UserAppImageInteraction(8),
            message="second",
            public=False,
        )
    )

    try:
        release_first_body.set()
        await asyncio.wait_for(first_delivery_started.wait(), timeout=0.5)
        await asyncio.sleep(0)
        assert events == [
            "turn:1:start",
            "turn:1:end",
            "deliver:reply-1:start",
        ]

        release_first_delivery.set()
        await asyncio.wait_for(asyncio.gather(first, second), timeout=0.5)
        assert events == [
            "turn:1:start",
            "turn:1:end",
            "deliver:reply-1:start",
            "deliver:reply-1:end",
            "turn:2:start",
            "turn:2:end",
            "deliver:reply-2:start",
            "deliver:reply-2:end",
        ]
    finally:
        release_first_body.set()
        release_first_delivery.set()
        for task in (first, second):
            if not task.done():
                task.cancel()
        await asyncio.gather(first, second, return_exceptions=True)
        await app.database.close()


@pytest.mark.asyncio
async def test_chat_admission_stays_active_through_delivery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app = await _user_app_chat_app(tmp_path, monkeypatch)
    delivery_started = asyncio.Event()
    release_delivery = asyncio.Event()

    async def run_turn(*_args: object, **_kwargs: object) -> TurnResult:
        return TurnResult(response_text="reply")

    async def deliver(_interaction: object, content: str, **kwargs: object) -> None:
        delivery_started.set()
        await release_delivery.wait()
        await _complete_primary_delivery(content, kwargs)

    install_foreground_turn_handler(app, run_turn)
    monkeypatch.setattr(user_app_turn_adapter, "send_interaction_result", deliver)
    task = asyncio.create_task(
        _run_registered_chat(
            app,
            _UserAppImageInteraction(),
            message="hello",
            public=False,
        )
    )

    try:
        await asyncio.wait_for(delivery_started.wait(), timeout=0.5)
        snapshot = await app.turn_admission.snapshot()
        assert snapshot.active_total == 1
        assert snapshot.active_by_user == {"42": 1}

        release_delivery.set()
        await asyncio.wait_for(task, timeout=0.5)
        assert (await app.turn_admission.snapshot()).active_total == 0
    finally:
        release_delivery.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await app.database.close()


@pytest.mark.asyncio
async def test_chat_delivery_failure_does_not_persist_assistant(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app = await _user_app_chat_app(tmp_path, monkeypatch)

    async def run_turn(*_args: object, **_kwargs: object) -> TurnResult:
        return TurnResult(response_text="undelivered reply")

    async def fail_delivery(*_args: object, **_kwargs: object) -> None:
        response = SimpleNamespace(status=500, reason="Server Error")
        raise discord.HTTPException(response, "delivery failed")  # type: ignore[arg-type]

    install_foreground_turn_handler(app, run_turn)
    monkeypatch.setattr(user_app_turn_adapter, "send_interaction_result", fail_delivery)
    interaction = _UserAppImageInteraction()

    try:
        await _run_registered_chat(
            app,
            interaction,
            message="hello",
            public=False,
        )

        assert not any(role == "assistant" for role, _content in await _personal_chat_messages(app))
        assert interaction.edits[-1]["content"] == (
            "I finished the turn but couldn't deliver the response here. Try again privately."
        )
    finally:
        await app.database.close()


@pytest.mark.asyncio
async def test_file_only_chat_persists_delivered_attachment_notice(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app = await _user_app_chat_app(tmp_path, monkeypatch)
    output = tmp_path / "artifact.txt"
    output.write_text("too large", encoding="utf-8")
    workspace_key = user_app_workspace_key("42")

    async def run_turn(*_args: object, **_kwargs: object) -> TurnResult:
        return TurnResult(
            response_text="",
            output_files=(str(output),),
            allowed_file_roots=(tmp_path,),
            workspace_key=workspace_key,
        )

    install_foreground_turn_handler(app, run_turn)
    interaction = _UserAppImageInteraction(file_size_limit=1)

    try:
        await _run_registered_chat(
            app,
            interaction,
            message="make a file",
            public=False,
        )

        delivered_content = str(interaction.edits[-1]["content"])
        assert delivered_content.startswith("Delivery notice:")
        assert await _personal_chat_messages(app) == [("assistant", delivered_content)]
    finally:
        await app.database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("output_files", "waits_for_writer"),
    [((), False), (("artifact.txt",), True)],
    ids=["text-only", "file"],
)
async def test_chat_file_delivery_uses_workspace_activity_lock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    output_files: tuple[str, ...],
    waits_for_writer: bool,
) -> None:
    app = await _user_app_chat_app(tmp_path, monkeypatch)
    workspace_key = user_app_workspace_key("42")
    turn_finished = asyncio.Event()
    delivery_started = asyncio.Event()

    async def run_turn(*_args: object, **_kwargs: object) -> TurnResult:
        turn_finished.set()
        return TurnResult(
            response_text="reply",
            output_files=output_files,
            workspace_key=workspace_key,
        )

    async def deliver(_interaction: object, content: str, **kwargs: object) -> None:
        delivery_started.set()
        await _complete_primary_delivery(content, kwargs)

    install_foreground_turn_handler(app, run_turn)
    monkeypatch.setattr(user_app_turn_adapter, "send_interaction_result", deliver)
    task: asyncio.Task[None] | None = None

    try:
        async with app.tools.workspace_locks.writer(workspace_key):
            task = asyncio.create_task(
                _run_registered_chat(
                    app,
                    _UserAppImageInteraction(),
                    message="hello",
                    public=False,
                )
            )
            await asyncio.wait_for(turn_finished.wait(), timeout=0.5)
            if waits_for_writer:
                await asyncio.sleep(0)
                assert delivery_started.is_set() is False
                assert task.done() is False
            else:
                await asyncio.wait_for(delivery_started.wait(), timeout=0.5)

        assert task is not None
        await asyncio.wait_for(task, timeout=0.5)
        assert delivery_started.is_set() is True
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await app.database.close()


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
                "user_app_dm_enabled": False,
            }
        )
    )
    await LifecycleProbe(off).first_init_core()
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
    await LifecycleProbe(on).first_init_core()
    for name in ("chat", "chat-reset", "privacy", "memory", "stop"):
        command = on.bot.tree.get_command(name)
        assert command is not None
        assert command.allowed_installs is not None
        assert command.allowed_installs.user is True
    await on.database.close()


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


def test_dm_surface_requires_the_chat_commands() -> None:
    """A DM-only deployment would hand out a personal transcript with no way to
    clear, cancel, or delete it: those commands ride on the /chat surface."""
    with pytest.raises(ValidationError, match="USER_APP_DM_ENABLED requires"):
        make_settings(
            user_app_chat_enabled=False,
            user_app_dm_enabled=True,
            owner_user_id="42",
            user_app_member_ids="",
            user_app_regular_ids="",
            user_app_staff_ids="",
        )
    ok = make_settings(
        user_app_chat_enabled=True,
        user_app_dm_enabled=True,
        owner_user_id="42",
        user_app_member_ids="",
        user_app_regular_ids="",
        user_app_staff_ids="",
    )
    assert ok.user_app_dm_enabled is True


def _dm_message(user_id: int, *, content: str = "hello", message_id: int = 7) -> object:
    channel = discord.DMChannel.__new__(discord.DMChannel)
    channel.id = 4242

    class _DMMessage:
        def __init__(self) -> None:
            self.id = message_id
            self.content = content
            self.channel = channel
            self.guild = None
            self.author = type(
                "User",
                (),
                {"id": user_id, "display_name": "Alice", "bot": False},
            )()
            self.type = discord.MessageType.default

    return _DMMessage()


async def _dm_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **overrides: object):
    monkeypatch.setattr(
        app_runtime,
        "build_provider_manager",
        lambda settings: StubProviderManager(settings),
    )
    config: dict[str, object] = {
        "discord_bot_token": "token",
        "model_api_key": "key",
        "config_dir": str(tmp_path / "config"),
        "database_path": str(tmp_path / "runtime.db"),
        "user_app_chat_enabled": True,
        "user_app_dm_enabled": True,
        "owner_user_id": "42",
    }
    config.update(overrides)
    app = app_runtime.build_app(Settings.model_validate(config))
    await LifecycleProbe(app).first_init_core()
    return app


@pytest.mark.asyncio
async def test_dm_personal_chat_tier_gates_on_setting_and_allowlist(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app = await _dm_app(tmp_path, monkeypatch)
    chat = PersonalChatDriver(app)
    # Owner is automatically user-app staff; nobody else is allowlisted here.
    assert chat.dm_tier(_dm_message(42)) is TrustTier.STAFF
    assert chat.dm_tier(_dm_message(999)) is None
    await app.database.close()

    off = await _dm_app(tmp_path / "off", monkeypatch, user_app_dm_enabled=False)
    assert PersonalChatDriver(off).dm_tier(_dm_message(42)) is None
    await off.database.close()


@pytest.mark.asyncio
async def test_dm_continues_the_owner_only_chat_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A DM after a /chat turn must resolve the existing owner-only root.

    ConversationStore rejects resolving an owner-only conversation under any
    other scope, so a DM that let access_scope default to channel-shared would
    raise PermissionError on every message after the first /chat turn.
    """
    app = await _dm_app(tmp_path, monkeypatch)
    store = app.conversation_store
    assert store is not None

    # Stand in for a prior /chat turn creating the personal root.
    await store.get_or_create(
        "userchat:42",
        "Personal chat",
        guild_id=None,
        channel_id="userapp",
        thread_id=None,
        root_discord_message_id="1",
        owner_user_id="42",
        access_scope=OWNER_ONLY,
    )

    resolved = await PersonalChatDriver(app).resolve_dm_conversation(_dm_message(42))
    assert resolved is not None
    assert resolved.key == "userchat:42"
    assert resolved.access_scope == OWNER_ONLY
    assert resolved.owner_user_id == "42"

    # The default scope is exactly what the resolver must not fall back to.
    with pytest.raises(PermissionError):
        await store.get_or_create(
            "userchat:42",
            "Personal chat",
            guild_id=None,
            channel_id="userapp",
            thread_id=None,
            root_discord_message_id="2",
            owner_user_id="42",
        )
    await app.database.close()


@pytest.mark.asyncio
async def test_personal_dm_uses_chat_execution_policy_without_onboarding(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app = await _dm_app(tmp_path, monkeypatch, new_user_onboarding_turns=9)
    captured: dict[str, Any] = {}

    async def capture_turn(
        turn_input: object,
        *,
        dependencies: object,
        preparation_config: object,
        execution_config: object,
    ) -> None:
        captured.update(
            turn_input=turn_input,
            dependencies=dependencies,
            preparation_config=preparation_config,
            execution_config=execution_config,
        )

    install_foreground_turn_handler(app, capture_turn)

    try:
        result = await app.handle_message(
            _dm_message(42),  # type: ignore[arg-type]
            lock_acquired=True,
        )

        assert result is None
        assert captured["execution_config"].command_template == "chat"
        assert captured["preparation_config"].new_user_onboarding_turns == 0
        assert captured["dependencies"].count_user_prior_messages is None
    finally:
        await app.database.close()


@pytest.mark.asyncio
async def test_dm_registers_provisional_work_in_personal_scope(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app = await _dm_app(tmp_path, monkeypatch)
    LifecycleProbe(app).set_gateway_ready()
    channels: list[str] = []
    original_register = app.active_operations.register_provisional

    @contextmanager
    def record_registration(**kwargs: object):
        channels.append(str(kwargs["channel_id"]))
        with original_register(**kwargs):  # type: ignore[arg-type]
            yield

    async def stop_after_registration(_message: object) -> None:
        return None

    monkeypatch.setattr(app.active_operations, "register_provisional", record_registration)
    monkeypatch.setattr(app.message_controller, "_on_message_for_user", stop_after_registration)

    await app.on_message(_dm_message(42))  # type: ignore[arg-type]

    assert channels == ["userapp"]
    await app.database.close()
