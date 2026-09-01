from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

import discord
from discord.ext import commands

from agent.backfill import clean_message_text, message_source_timestamp
from agent.turn import TurnPreparationInput, TurnResult
from app.admission import AdmissionRejection, TURN_ADMISSION_BUSY_MESSAGE, TurnAdmissionController
from app.cancellation import ActiveOperationRegistry
from app.conversation_routing import ResolvedConversation
from app.foreground_turn import (
    ForegroundTurnInvocation,
    ForegroundTurnRunner,
    TurnConversationSpec,
)
from app.root_locks import RootLockPool
from app.turn_entry import _PERSONAL_CHAT_BLOCKED_TOOLS, TurnEntryHooks
from app.user_app_consent import UserAppConsentPrompter
from app.user_app_turn_adapter import UserAppInteractionTurnAdapter
from discord_adapter.interaction_io import send_interaction_status
from storage.conversations import OWNER_ONLY, ConversationStore
from tools.registry import USER_APP_SCOPE_CHANNEL_ID
from trust.tiers import TrustTier
from trust.user_app import UserAppAccess
from utils.privacy_barrier import PrivacyDeletionPendingError, UserPrivacyBarrier
from workspace import user_app_workspace_key

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from app.lifecycle import ShutdownSignal
    from app.message_runtime import BlockedUserCheck


class PersonalWorkCanceller(Protocol):
    async def __call__(
        self,
        *,
        user_id: str,
        channel_id: str,
        root_key: str,
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class UserAppChatConfig:
    timeout_seconds: float
    dm_enabled: bool


@dataclass(frozen=True, slots=True)
class UserAppChatRequest:
    user_id: str
    generation: int


@dataclass(frozen=True, slots=True)
class _UserAppMessageSource:
    """The small discord.Message-shaped surface attachment preparation needs."""

    id: int
    content: str
    author: Any
    channel: Any
    guild: Any
    attachments: list[discord.Attachment]
    created_at: Any
    reference: Any | None = None


def is_user_integration(interaction: discord.Interaction) -> bool:
    is_user = getattr(interaction, "is_user_integration", None)
    if not callable(is_user):
        return False
    try:
        return bool(is_user())
    except Exception:
        return False


def is_guild_integration(interaction: discord.Interaction) -> bool:
    is_guild = getattr(interaction, "is_guild_integration", None)
    if not callable(is_guild):
        return False
    try:
        return bool(is_guild())
    except Exception:
        return False


def is_user_only_interaction(interaction: discord.Interaction) -> bool:
    return is_user_integration(interaction) and not is_guild_integration(interaction)


def interaction_can_post_publicly(interaction: discord.Interaction) -> bool:
    if interaction.guild_id is None:
        return True

    user_only = is_user_only_interaction(interaction)
    permission_source = "permissions" if user_only else "app_permissions"
    permissions = getattr(interaction, permission_source, None)
    if permissions is None:
        return False
    if user_only and not bool(getattr(permissions, "use_external_apps", False)):
        return False
    channel = interaction.channel
    if isinstance(channel, discord.Thread):
        return bool(getattr(permissions, "send_messages_in_threads", False))
    return bool(getattr(permissions, "send_messages", False))


async def _send_user_app_status(
    interaction: discord.Interaction,
    content: str,
    *,
    requested_public: bool,
) -> None:
    """Resolve a deferred personal-chat response at its selected visibility."""

    await send_interaction_status(
        interaction,
        content,
        ephemeral=not requested_public,
        original_ephemeral=not requested_public,
    )


class UserAppChatController:
    def __init__(
        self,
        *,
        config: UserAppChatConfig,
        bot: commands.Bot,
        access: UserAppAccess,
        user_blocked: BlockedUserCheck,
        consent: UserAppConsentPrompter,
        conversation_store: ConversationStore,
        active_operations: ActiveOperationRegistry,
        privacy_barrier: UserPrivacyBarrier,
        turn_admission: TurnAdmissionController,
        root_locks: RootLockPool,
        turn_runner: ForegroundTurnRunner,
        shutdown: ShutdownSignal,
        cancel_personal_work: PersonalWorkCanceller,
        turn_entry_hooks: TurnEntryHooks,
    ) -> None:
        self._config = config
        self._bot = bot
        self._access = access
        self._user_blocked = user_blocked
        self._consent = consent
        self._conversation_store = conversation_store
        self._active_operations = active_operations
        self._privacy_barrier = privacy_barrier
        self._turn_admission = turn_admission
        self._root_locks = root_locks
        self._turn_runner = turn_runner
        self._shutdown = shutdown
        self._cancel_personal_work = cancel_personal_work
        self._turn_entry_hooks = turn_entry_hooks
        self._generations: dict[str, int] = {}

    async def handle(
        self,
        interaction: discord.Interaction,
        message: str,
        attachment: discord.Attachment | None,
        public: bool,
    ) -> None:
        user_id = str(interaction.user.id)
        # Capture before the first await. A reset/privacy deletion that starts
        # while access, block, or consent state is being read must invalidate
        # this already-submitted command rather than let it create a new root.
        request = self.capture_request(user_id)
        tier = self._access.resolve(user_id)
        if tier is None:
            await interaction.response.send_message(
                "You don't have access to this app's personal chat.",
                ephemeral=True,
            )
            return
        if await self._user_blocked(user_id):
            await interaction.response.send_message(
                "You can't use personal chat right now.",
                ephemeral=True,
            )
            return
        if not message.strip() and attachment is None:
            await interaction.response.send_message(
                "Add a message or attachment first.", ephemeral=True
            )
            return
        if public and not interaction_can_post_publicly(interaction):
            await interaction.response.send_message(
                "I can't post publicly in this location. Run `/chat` again with "
                "`visibility:Only me`.",
                ephemeral=True,
            )
            return

        async def execute(resume_interaction: discord.Interaction) -> None:
            await self.run(
                resume_interaction,
                message=message,
                attachment=attachment,
                public=public,
                request=request,
            )

        if await self._consent.prompt_if_needed(
            interaction,
            on_accept=execute,
            public_response=public,
        ):
            return

        # A deferred response cannot change visibility later. The selected
        # visibility therefore applies to live activity, results, and failures.
        await interaction.response.defer(ephemeral=not public, thinking=True)
        await execute(interaction)

    def capture_request(self, user_id: str) -> UserAppChatRequest:
        return UserAppChatRequest(user_id=user_id, generation=self.generation(user_id))

    def generation(self, user_id: str) -> int:
        return self._generations.get(user_id, 0)

    def invalidate_requests(self, user_id: str) -> None:
        self._generations[user_id] = self.generation(user_id) + 1

    async def run(
        self,
        interaction: discord.Interaction,
        *,
        message: str,
        attachment: discord.Attachment | None,
        public: bool,
        request: UserAppChatRequest,
    ) -> TurnResult | None:
        user_id = str(interaction.user.id)
        root_key = f"userchat:{user_id}"
        turn_stop_event = asyncio.Event()
        deadline = asyncio.get_running_loop().time() + self._config.timeout_seconds
        try:
            # Publish the operation synchronously before the first await and keep
            # it registered through transcript persistence and Discord delivery.
            # Reset/privacy can therefore cancel and drain every older request
            # before reporting that deletion completed.
            with self._active_operations.register_provisional(
                user_id=user_id,
                channel_id=USER_APP_SCOPE_CHANNEL_ID,
                stop_event=turn_stop_event,
            ):
                self._active_operations.bind_current_provisional(root_key)
                async with self._privacy_barrier.activity(user_id):
                    if request.generation != self.generation(user_id):
                        await _send_user_app_status(
                            interaction,
                            (
                                "That chat request expired because your personal thread "
                                "was reset or deleted. Run `/chat` again if you still want it."
                            ),
                            requested_public=public,
                        )
                        return None
                    trust_tier = self._access.resolve(user_id)
                    if trust_tier is None:
                        await _send_user_app_status(
                            interaction,
                            "You no longer have access to this app's personal chat.",
                            requested_public=public,
                        )
                        return None
                    if await self._user_blocked(user_id):
                        await _send_user_app_status(
                            interaction,
                            "You can't use personal chat right now.",
                            requested_public=public,
                        )
                        return None
                    try:
                        async with asyncio.timeout_at(deadline):
                            admission = await self._turn_admission.try_acquire(user_id)
                            if admission.lease is None:
                                if admission.rejection is AdmissionRejection.SHUTTING_DOWN:
                                    log.info(
                                        "Ignoring personal chat turn from user %s during shutdown",
                                        user_id,
                                    )
                                    return None
                                log.info(
                                    "Rejecting personal chat turn from user %s at admission "
                                    "boundary: %s",
                                    user_id,
                                    admission.rejection,
                                )
                                await _send_user_app_status(
                                    interaction,
                                    TURN_ADMISSION_BUSY_MESSAGE,
                                    requested_public=public,
                                )
                                return None

                            source_message = _UserAppMessageSource(
                                id=int(interaction.id),
                                content=message,
                                author=interaction.user,
                                channel=interaction.channel,
                                guild=interaction.guild,
                                attachments=[attachment] if attachment is not None else [],
                                created_at=interaction.created_at,
                            )
                            actual_guild_id = (
                                str(interaction.guild_id) if interaction.guild_id else None
                            )
                            actual_channel_id = str(interaction.channel_id or "")
                            actual_thread_id = (
                                actual_channel_id
                                if isinstance(interaction.channel, discord.Thread)
                                else None
                            )
                            adapter = UserAppInteractionTurnAdapter(
                                interaction=interaction,
                                requested_public=public,
                                context_channel_id=USER_APP_SCOPE_CHANNEL_ID,
                            )

                            try:
                                async with admission.lease:
                                    async with self._root_locks.hold(root_key):
                                        current_trust_tier = self._access.resolve(user_id)
                                        if current_trust_tier is None:
                                            await _send_user_app_status(
                                                interaction,
                                                (
                                                    "You no longer have access to this app's "
                                                    "personal chat."
                                                ),
                                                requested_public=public,
                                            )
                                            return None
                                        if await self._user_blocked(user_id):
                                            await _send_user_app_status(
                                                interaction,
                                                "You can't use personal chat right now.",
                                                requested_public=public,
                                            )
                                            return None
                                        trust_tier = current_trust_tier
                                        turn_input = TurnPreparationInput(
                                            raw_content=clean_message_text(message),
                                            source_message=source_message,
                                            bot_user=self._bot.user,
                                            guild_id=None,
                                            channel_id=USER_APP_SCOPE_CHANNEL_ID,
                                            thread_id=None,
                                            channel_name="Personal chat",
                                            user_id=user_id,
                                            user_name=interaction.user.display_name,
                                            trust_tier=trust_tier,
                                            conversation_key=root_key,
                                            trigger_discord_message_id=str(interaction.id),
                                            conversation_owner_user_id=user_id,
                                            conversation_access_scope=OWNER_ONLY,
                                            personal_chat=True,
                                            platform_guild_id=actual_guild_id,
                                            platform_channel_id=actual_channel_id,
                                            platform_thread_id=actual_thread_id,
                                            workspace_key=user_app_workspace_key(user_id),
                                        )
                                        invocation = ForegroundTurnInvocation(
                                            conversation=TurnConversationSpec(
                                                key=root_key,
                                                channel_name="Personal chat",
                                                guild_id=None,
                                                channel_id=USER_APP_SCOPE_CHANNEL_ID,
                                                thread_id=None,
                                                root_discord_message_id=str(interaction.id),
                                                owner_user_id=user_id,
                                                access_scope=OWNER_ONLY,
                                            ),
                                            source=turn_input,
                                            prepared_user_discord_message_id=(
                                                f"userapp:{interaction.id}"
                                            ),
                                            prepared_user_source_created_at=(
                                                message_source_timestamp(source_message)
                                            ),
                                            prepared_user_context_channel_id=(
                                                USER_APP_SCOPE_CHANNEL_ID
                                            ),
                                            collect_reply_context=(
                                                self._turn_entry_hooks.collect_reply_context
                                            ),
                                            strip_mention=(
                                                lambda content, **_kwargs: content.strip()
                                            ),
                                            stop_event=turn_stop_event,
                                            hooks=self._turn_entry_hooks,
                                            collect_turn_attachments=(
                                                self._turn_entry_hooks.collect_turn_attachments
                                            ),
                                            command_template="chat",
                                            count_user_prior_messages=None,
                                            new_user_onboarding_turns=0,
                                            timeout_seconds=self._config.timeout_seconds,
                                            thread_handoff_suggest_after_tool_calls=0,
                                            extra_blocked_tools=_PERSONAL_CHAT_BLOCKED_TOOLS,
                                        )
                                        return await self._turn_runner.run(
                                            invocation,
                                            adapter=adapter,
                                        )
                            except PrivacyDeletionPendingError:
                                await _send_user_app_status(
                                    interaction,
                                    (
                                        "Your data deletion is still in progress. Try again "
                                        "when it finishes."
                                    ),
                                    requested_public=public,
                                )
                                return None
                            except Exception:
                                log.exception(
                                    "Personal user-app chat failed for user %s",
                                    user_id,
                                )
                                with suppress(discord.HTTPException):
                                    await _send_user_app_status(
                                        interaction,
                                        "I couldn't complete that chat turn. Please try again.",
                                        requested_public=public,
                                    )
                                return None
                    except TimeoutError:
                        await _send_user_app_status(
                            interaction,
                            "That personal chat turn timed out. Run `/chat` again to retry.",
                            requested_public=public,
                        )
        except PrivacyDeletionPendingError:
            await _send_user_app_status(
                interaction,
                "Your data deletion is still in progress. Try again when it finishes.",
                requested_public=public,
            )
        except asyncio.CancelledError:
            # Shutdown must stay cancellable: the client is already closing, so
            # never await another interaction edit here. A user-initiated /stop
            # is an ordinary outcome and is reported to the caller instead of
            # propagating as an error, matching the guild message path.
            if self._shutdown.closed:
                raise
            with suppress(discord.HTTPException):
                await _send_user_app_status(
                    interaction,
                    "Stopped.",
                    requested_public=public,
                )
            log.info("Stopped personal chat response for user %s", user_id)
        return None

    async def reset(self, interaction: discord.Interaction) -> str:
        user_id = str(interaction.user.id)
        root_key = f"userchat:{user_id}"
        clean = await self._cancel_personal_work(
            user_id=user_id,
            root_key=root_key,
            channel_id=USER_APP_SCOPE_CHANNEL_ID,
        )
        if not clean:
            return "I couldn't finish stopping active work, so I did not clear the chat. Try again shortly."
        async with self._root_locks.hold(root_key):
            deleted = await self._conversation_store.delete_owner_conversation(root_key, user_id)
        if deleted:
            return "Your personal chat thread was cleared. Memory and workspace files were kept."
        return "Your personal chat thread is already clear."

    async def resolve_dm_conversation(
        self,
        message: discord.Message,
    ) -> ResolvedConversation:
        """Continue this user's one personal root instead of opening a new one."""

        user_id = str(message.author.id)
        root_key = f"userchat:{user_id}"
        conversation_id = await self._conversation_store.get_or_create(
            root_key,
            "Personal chat",
            guild_id=None,
            channel_id=USER_APP_SCOPE_CHANNEL_ID,
            thread_id=None,
            root_discord_message_id=str(message.id),
            owner_user_id=user_id,
            access_scope=OWNER_ONLY,
        )
        return ResolvedConversation(
            key=root_key,
            db_conversation_id=conversation_id,
            owner_user_id=user_id,
            access_scope=OWNER_ONLY,
        )

    def classify_dm(self, message: discord.Message) -> TrustTier | None:
        """Access tier for an ambient DM entering personal chat, else None."""

        if not self._config.dm_enabled:
            return None
        if not isinstance(message.channel, discord.DMChannel):
            return None
        return self._access.resolve(str(message.author.id))
