"""The ``/privacy`` slash command: a plain-language privacy TL;DR plus the
on-demand deletion controls that let a user purge their data immediately rather
than waiting out the 30-day transcript retention window.

Two buttons ride the TL;DR:

* **Delete my data**: prompts, transcripts, files, and memory.
* **Delete memory**: long-term memory only (the former ``/memory forget-me``).

This module is a Discord-`View` boundary (importing ``discord`` is expected). The
TL;DR text mirrors ``docs/privacy-policy.md``; keep the two in sync. Deletion runs
the same building blocks the rest of the bot uses (``ConversationStore`` for the
SQLite transcript and ``memory/privacy.py:forget_user_memory`` for Hindsight), so
there is no parallel deletion path.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

import discord
from discord import app_commands
from discord.ext import commands

from workspace import WorkspaceManager
from utils.privacy_barrier import UserPrivacyBarrier
from memory.client import MemoryClient
from memory.mutations import user_memory_mutation
from memory.privacy import forget_user_memory
from storage.auto_retain import AutoRetainStore
from storage.conversations import ConversationStore
from storage.memory_banks import UserMemoryBankStateStore
from storage.preferences import PreferenceStore
from storage.privacy import (
    PrivacyDeletionRequest,
    PrivacyDeletionRequestStore,
    PrivacyDeletionScope,
)
from tools.workspace.common import UserLocks

CancelUserWork = Callable[[str], Awaitable[None]]

log = logging.getLogger(__name__)

DeleteScope = PrivacyDeletionScope


ConversationTurnLock = Callable[[str], contextlib.AbstractAsyncContextManager[None]]


class BrowserDataStore(Protocol):
    async def delete_user_data(self, user_id: str) -> int: ...


class VideoDataStore(Protocol):
    async def delete_user_data(self, user_id: str) -> tuple[int, bool]: ...


@dataclass(frozen=True)
class PrivacyDeletionOutcome:
    """Result of an on-demand deletion.

    ``durable_request_completed`` is ``None`` when no durable request store was
    involved, ``True`` only when this exact request generation was removed, and
    ``False`` when authorization/finalization failed or a newer generation now
    owns the tombstone.  Keeping that distinct from ``ok`` matters because the
    destructive work for one generation can succeed while activity must remain
    paused for a newer request.
    """

    ok: bool
    lines: list[str]
    durable_request_completed: bool | None = None
    effective_scope: DeleteScope | None = None


_CONFIRMED_PRIVACY_DELETIONS: set[asyncio.Task[PrivacyDeletionOutcome]] = set()


def _track_confirmed_privacy_deletion(
    task: asyncio.Task[PrivacyDeletionOutcome],
) -> None:
    """Keep a confirmed deletion alive until it finishes or shutdown drains it."""

    _CONFIRMED_PRIVACY_DELETIONS.add(task)

    def done(completed: asyncio.Task[PrivacyDeletionOutcome]) -> None:
        _CONFIRMED_PRIVACY_DELETIONS.discard(completed)
        try:
            completed.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("Confirmed privacy deletion failed")

    task.add_done_callback(done)


async def drain_confirmed_privacy_deletions() -> None:
    """Wait for every deletion authorized before application shutdown.

    Confirmed deletion tasks are tracked as soon as they start, rather than only
    after Discord cancels their interaction callback. Re-snapshot after each
    batch so a task registered while an earlier batch is finishing is drained
    too. Individual task failures are logged by the completion callback and do
    not prevent the remaining confirmed deletions from finishing.
    """

    while _CONFIRMED_PRIVACY_DELETIONS:
        tasks = tuple(_CONFIRMED_PRIVACY_DELETIONS)
        await asyncio.gather(*tasks, return_exceptions=True)
        _CONFIRMED_PRIVACY_DELETIONS.difference_update(tasks)


async def _forget_memory_line(
    *,
    user_id: str,
    preference_store: PreferenceStore,
    memory_client: MemoryClient | None,
    auto_retain_watermarks: AutoRetainStore | None,
    memory_bank_state_store: UserMemoryBankStateStore | None,
    memory_backend_required: bool,
) -> tuple[bool, str]:
    try:
        result = await forget_user_memory(
            memory_client=memory_client,
            preference_store=preference_store,
            user_id=user_id,
            auto_retain_watermarks=auto_retain_watermarks,
            bank_state_store=memory_bank_state_store,
        )
    except Exception:
        log.exception("Privacy deletion: memory forget failed for %s", user_id)
        return False, (
            "⚠️ Long-term memory could not be wiped. Please ask staff to check the bot logs."
        )
    if result.bank_deleted:
        return True, "Long-term memory wiped and future memory disabled."
    if memory_client is None:
        if memory_backend_required:
            return False, (
                "Future memory disabled, but the memory backend required by your "
                "confirmed deletion is currently unavailable. The deletion remains "
                "pending; retry `/privacy` or ask staff."
            )
        return True, "No long-term memory backend is configured, so there was none to wipe."
    # Future memory is disabled, but the bank delete did not confirm: treat as a
    # partial failure so the user isn't told everything is gone.
    return False, "Future memory disabled, but the stored memory bank could not be deleted."


async def _requires_memory_backend(
    *,
    user_id: str,
    memory_client: MemoryClient | None,
    memory_bank_state_store: UserMemoryBankStateStore | None,
) -> bool:
    if memory_client is not None:
        return True
    if memory_bank_state_store is None:
        return False
    return await memory_bank_state_store.may_exist(user_id)


async def run_privacy_deletion(
    *,
    scope: DeleteScope,
    user_id: str,
    conversation_store: ConversationStore,
    preference_store: PreferenceStore,
    memory_client: MemoryClient | None,
    auto_retain_watermarks: AutoRetainStore | None,
    # Required even for scope='memory', where they go unused: every caller has
    # them, and a scope='all' run that quietly skipped the workspace wipe would
    # report success while leaving the user's uploaded files on disk.
    workspace_manager: WorkspaceManager,
    workspace_locks: UserLocks,
    privacy_barrier: UserPrivacyBarrier | None = None,
    deletion_request_store: PrivacyDeletionRequestStore | None = None,
    pending_request: PrivacyDeletionRequest | None = None,
    memory_bank_state_store: UserMemoryBankStateStore | None = None,
    conversation_turn_lock: ConversationTurnLock | None = None,
    memory_backend_required: bool | None = None,
    browser_data_store: BrowserDataStore | None = None,
    video_data_store: VideoDataStore | None = None,
) -> PrivacyDeletionOutcome:
    """Run the on-demand deletion for one user, returning summary lines.

    ``scope='all'`` deletes the SQLite transcript (via
    ``ConversationStore.delete_user_data``), the user's per-(user, guild)
    workspace dirs (via
    ``WorkspaceManager.delete_owner_dirs``), and then long-term memory;
    ``scope='memory'`` deletes only long-term memory.

    When the application supplies ``privacy_barrier``, deletion waits for the
    user's already-started turns to finish, prevents later turns from starting,
    wipes the resulting state, and then allows genuinely new interactions. A
    hard transcript failure aborts before later stores are touched and reports
    ``ok=False``.
    """
    if pending_request is not None and deletion_request_store is None:
        raise ValueError("A pending privacy request requires its durable store.")
    if pending_request is not None and pending_request.user_id != user_id:
        raise ValueError("Pending privacy request belongs to a different user.")
    discovered_memory_backend = await _requires_memory_backend(
        user_id=user_id,
        memory_client=memory_client,
        memory_bank_state_store=memory_bank_state_store,
    )

    if privacy_barrier is not None:

        async def delete_under_barrier() -> PrivacyDeletionOutcome:
            durable_request = pending_request
            effective_scope = scope
            required_memory_backend = discovered_memory_backend
            if memory_backend_required is not None:
                required_memory_backend = memory_backend_required or discovered_memory_backend
            if deletion_request_store is not None:
                if durable_request is None:
                    try:
                        durable_request = await deletion_request_store.request(
                            user_id=user_id,
                            scope=scope,
                            memory_backend_required=required_memory_backend,
                        )
                    except Exception:
                        log.exception(
                            "Privacy deletion: durable authorization failed for %s",
                            user_id,
                        )
                        return PrivacyDeletionOutcome(
                            ok=False,
                            lines=[
                                (
                                    "❌ Your deletion request could not be saved, so "
                                    "nothing was deleted. Please try again or ask "
                                    "staff to check the bot logs."
                                )
                            ],
                            durable_request_completed=False,
                            effective_scope=scope,
                        )
                effective_scope = durable_request.scope
                required_memory_backend = (
                    durable_request.memory_backend_required or discovered_memory_backend
                )
                # This follows the durable commit and precedes every destructive
                # step. If an attempt is partial, later activity stays blocked so
                # a retry cannot erase state created after authorization.
                await privacy_barrier.mark_deletion_pending(user_id)

            async with privacy_barrier.deletion(user_id):
                if deletion_request_store is not None and durable_request is not None:
                    # Auto-retain is not an ordinary Discord activity lease. Drain
                    # any flusher that passed its pending-row recheck immediately
                    # before this authorization. Later flushers see the durable
                    # row and decline to write.
                    async with user_memory_mutation(user_id):
                        pass
                outcome = await run_privacy_deletion(
                    scope=effective_scope,
                    user_id=user_id,
                    conversation_store=conversation_store,
                    preference_store=preference_store,
                    memory_client=memory_client,
                    auto_retain_watermarks=auto_retain_watermarks,
                    workspace_manager=workspace_manager,
                    workspace_locks=workspace_locks,
                    privacy_barrier=None,
                    memory_bank_state_store=memory_bank_state_store,
                    conversation_turn_lock=conversation_turn_lock,
                    memory_backend_required=required_memory_backend,
                    browser_data_store=browser_data_store,
                    video_data_store=video_data_store,
                )
                if deletion_request_store is None or durable_request is None:
                    return outcome
                if not outcome.ok:
                    return PrivacyDeletionOutcome(
                        ok=False,
                        lines=outcome.lines,
                        durable_request_completed=False,
                        effective_scope=effective_scope,
                    )
                try:
                    completed = await deletion_request_store.complete(durable_request)
                except Exception:
                    log.exception("Privacy deletion: durable completion failed for %s", user_id)
                    return PrivacyDeletionOutcome(
                        ok=False,
                        lines=[
                            *outcome.lines,
                            (
                                "⚠️ Your data was wiped, but the durable deletion "
                                "request could not be finalized. Activity remains "
                                "paused; retry `/privacy` or ask staff."
                            ),
                        ],
                        durable_request_completed=False,
                        effective_scope=effective_scope,
                    )
                if completed:
                    await privacy_barrier.clear_deletion_pending(user_id)
                # False means a newer generation replaced this one. Its own
                # tracked worker (or startup replay) retains the tombstone and
                # performs the wider/newer request.
                return PrivacyDeletionOutcome(
                    ok=outcome.ok,
                    lines=(
                        outcome.lines
                        if completed
                        else [
                            *outcome.lines,
                            (
                                "A newer deletion request is still pending; activity "
                                "remains paused until that request finishes."
                            ),
                        ]
                    ),
                    durable_request_completed=completed,
                    effective_scope=effective_scope,
                )

        # Confirmation is the authorization point. If Discord cancels the
        # callback afterward (disconnect/shutdown), finish the destructive work
        # under the exclusive barrier rather than release it while a worker-
        # thread workspace wipe or SQLite operation is still mutating state.
        deletion_task = asyncio.create_task(delete_under_barrier())
        _track_confirmed_privacy_deletion(deletion_task)
        return await asyncio.shield(deletion_task)

    if deletion_request_store is not None:
        raise ValueError("Durable privacy deletion requires a privacy barrier.")

    lines: list[str] = []
    ok = True
    if scope == "all":
        turn_lock = (
            conversation_turn_lock(user_id)
            if conversation_turn_lock is not None
            else contextlib.nullcontext()
        )
        try:
            # A different participant's turn can already be reading this user's
            # messages from a shared root. Drain every affected root through its
            # normal delivery/persistence critical section before removing the
            # transcript, so no response derived from deleted data lands later.
            async with turn_lock:
                deletion = await conversation_store.delete_user_data(user_id)
        except Exception:
            log.exception("Privacy deletion: transcript delete failed for %s", user_id)
            return PrivacyDeletionOutcome(
                ok=False,
                lines=[
                    (
                        "Something went wrong deleting your conversation history. Nothing "
                        "else was removed. Please ask staff to check the bot logs."
                    )
                ],
                effective_scope=scope,
            )
        lines.append(
            f"Deleted **{deletion.conversations_deleted}** conversation(s) you "
            f"started and scrubbed **{deletion.messages_scrubbed}** of your "
            "message(s) from shared conversations."
        )
        lines.append(f"Deleted **{deletion.coding_tasks_deleted}** coding task record(s).")
        try:
            async with workspace_locks.maintenance():
                removed = await asyncio.to_thread(workspace_manager.delete_owner_dirs, user_id)
        except Exception:
            log.exception("Privacy deletion: workspace wipe failed for %s", user_id)
            ok = False
            lines.append(
                "⚠️ Your uploaded workspace files could not be wiped. Please "
                "ask staff to check the bot logs."
            )
        else:
            lines.append(f"Wiped your workspace files across **{removed}** community workspace(s).")
        if browser_data_store is not None:
            try:
                removed = await browser_data_store.delete_user_data(user_id)
            except Exception:
                log.exception("Privacy deletion: browser profile wipe failed for %s", user_id)
                ok = False
                lines.append(
                    "⚠️ Your browser profile could not be wiped. Please ask staff "
                    "to check the bot logs."
                )
            else:
                lines.append(f"Wiped **{removed}** persistent browser profile(s).")
        if video_data_store is not None:
            try:
                removed, provider_cleanup_pending = await video_data_store.delete_user_data(user_id)
            except Exception:
                log.exception("Privacy deletion: video session wipe failed for %s", user_id)
                ok = False
                lines.append(
                    "⚠️ Stored video sessions could not be deleted locally. "
                    "Please retry `/privacy` or ask staff to check the bot logs."
                )
            else:
                lines.append(f"Deleted **{removed}** stored video session(s).")
                if provider_cleanup_pending:
                    lines.append(
                        "Gemini video data deletion remains durably queued and will "
                        "retry when provider access is available."
                    )

    memory_ok, memory_line = await _forget_memory_line(
        user_id=user_id,
        preference_store=preference_store,
        memory_client=memory_client,
        auto_retain_watermarks=auto_retain_watermarks,
        memory_bank_state_store=memory_bank_state_store,
        memory_backend_required=(
            discovered_memory_backend
            if memory_backend_required is None
            else memory_backend_required or discovered_memory_backend
        ),
    )
    ok = ok and memory_ok
    lines.append(memory_line)
    return PrivacyDeletionOutcome(ok=ok, lines=lines, effective_scope=scope)


def _build_tldr_embed(
    retention_days: int,
    *,
    bot_name: str = "",
    policy_url: str = "",
) -> discord.Embed:
    # Discord embed descriptions cap at 4096 chars; this stays well under.
    retention = (
        f"after **{retention_days} days**"
        if retention_days > 0
        else "**(retention sweep disabled on this deployment)**"
    )
    name = bot_name.strip() or "This bot"
    # An unset policy URL drops the link rather than pointing members at a page
    # this deployment does not control.
    policy_link = (
        f" [Read the full privacy policy]({policy_url.strip()})." if policy_url.strip() else ""
    )
    return discord.Embed(
        title=f"{name}: Privacy in brief",
        color=discord.Color.blurple(),
        description=(
            f"Here's the short version of how I handle your data.{policy_link}\n\n"
            "**When I'm listening**\n"
            f'Only when you call on me: an @mention, a reply, "hey {name.lower()}", or a '
            "thread I started. I **ignore DMs** entirely. During a requested task, "
            "I may read recent public channel messages for context, but I don't save "
            "ordinary chatter as my conversation history. Optional staff moderation "
            "logging can separately copy server events to a staff Discord channel.\n\n"
            "**What I collect**\n"
            "Your messages to me (text, images, files you share), basic Discord "
            "identifiers (user ID, display name, server/channel), and usage counts "
            "for cost tracking, never your message content for ads. If I browse "
            "the web for you, cookies and site storage stay in your private "
            "profile.\n\n"
            "**Who it's shared with**\n"
            "Your message and recent conversation go to the AI provider that powers "
            "my replies. Memory, moderation, search providers such as Exa or Brave, "
            "the optional Gemini video service, websites, and operator-added "
            "tools receive only what their task needs. "
            "Staff can also teach public messages into shared community knowledge. "
            "I **never sell your data or use it for ads**.\n\n"
            "**How long I keep it**\n"
            f"Conversation history auto-deletes {retention} of going quiet. "
            "Workspace files and browser profiles clear after 7 days idle; video "
            "sessions clear locally after at most 24 hours idle and queue provider deletion. "
            "Long-term memory is enabled by default; you can opt out or wipe it "
            "at any time. Usage, moderation, diagnostic logs, skills, and shared "
            "community knowledge have separate lifecycles.\n\n"
            "**Your controls**\n"
            "- `/memory status` / `/memory opt-out` / `/memory opt-in`: manage "
            "long-term memory.\n"
            "- **Delete memory** (button below): wipe your long-term memory now and "
            "disable future memory.\n"
            "- **Delete my data** (button below): immediately delete your "
            "local conversation history, workspace files, browser profile, video "
            "sessions, *and* personal memory, without waiting for automatic expiry. "
            "Known Gemini video Interactions and uploaded Files are also submitted "
            "for deletion.\n"
            "- This cannot erase Discord messages, provider safety logs or backups, "
            "community knowledge, skills, usage or moderation records, blocks, or "
            "your saved consent choice.\n"
            "- Ask me to block you, or Decline the privacy prompt if it appears.\n\n"
            "Questions? Reach the bot owner or server staff."
        ),
    )


def _result_embed(description: str, *, color: discord.Color) -> discord.Embed:
    return discord.Embed(description=description, color=color)


class _AuthorGuardedView(discord.ui.View):
    """A view only the invoking user may interact with."""

    def __init__(self, *, author_id: int, timeout: float) -> None:
        super().__init__(timeout=timeout)
        self._author_id = author_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user is not None and interaction.user.id == self._author_id:
            return True
        await interaction.response.send_message("This prompt isn't for you.", ephemeral=True)
        return False


class _DeleteConfirmView(_AuthorGuardedView):
    """Second-step confirmation for an irreversible delete action."""

    def __init__(
        self,
        *,
        author_id: int,
        scope: DeleteScope,
        conversation_store: ConversationStore,
        preference_store: PreferenceStore,
        memory_client: MemoryClient | None,
        auto_retain_watermarks: AutoRetainStore | None,
        deletion_request_store: PrivacyDeletionRequestStore | None = None,
        memory_bank_state_store: UserMemoryBankStateStore | None = None,
        conversation_turn_lock: ConversationTurnLock | None = None,
        workspace_manager: WorkspaceManager,
        workspace_locks: UserLocks,
        browser_data_store: BrowserDataStore | None = None,
        video_data_store: VideoDataStore | None = None,
        privacy_barrier: UserPrivacyBarrier | None = None,
        cancel_user_work: CancelUserWork | None = None,
        timeout: float = 120.0,
    ) -> None:
        super().__init__(author_id=author_id, timeout=timeout)
        self._scope = scope
        self._conversation_store = conversation_store
        self._preference_store = preference_store
        self._memory_client = memory_client
        self._auto_retain_watermarks = auto_retain_watermarks
        self._deletion_request_store = deletion_request_store
        self._memory_bank_state_store = memory_bank_state_store
        self._conversation_turn_lock = conversation_turn_lock
        self._workspace_manager = workspace_manager
        self._workspace_locks = workspace_locks
        self._browser_data_store = browser_data_store
        self._video_data_store = video_data_store
        self._privacy_barrier = privacy_barrier
        self._cancel_user_work = cancel_user_work

    @discord.ui.button(label="Yes, delete", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        user_id = str(interaction.user.id)

        async def execute(
            request: PrivacyDeletionRequest | None,
        ) -> PrivacyDeletionOutcome:
            if self._cancel_user_work is not None:
                await self._cancel_user_work(user_id)
            return await run_privacy_deletion(
                scope=self._scope,
                user_id=user_id,
                conversation_store=self._conversation_store,
                preference_store=self._preference_store,
                memory_client=self._memory_client,
                auto_retain_watermarks=self._auto_retain_watermarks,
                workspace_manager=self._workspace_manager,
                workspace_locks=self._workspace_locks,
                privacy_barrier=self._privacy_barrier,
                deletion_request_store=self._deletion_request_store,
                pending_request=request,
                memory_bank_state_store=self._memory_bank_state_store,
                conversation_turn_lock=self._conversation_turn_lock,
                browser_data_store=self._browser_data_store,
                video_data_store=self._video_data_store,
            )

        authorization_ready = asyncio.Event()
        authorization_error: Exception | None = None

        async def authorize_and_execute() -> PrivacyDeletionOutcome:
            nonlocal authorization_error
            request: PrivacyDeletionRequest | None = None
            try:
                if self._deletion_request_store is not None:
                    # The confirmation click is the authorization point. Persist
                    # it before acknowledging Discord, and track this whole
                    # workflow immediately so graceful shutdown also drains an
                    # authorization write that is still in flight.
                    memory_backend_required = await _requires_memory_backend(
                        user_id=user_id,
                        memory_client=self._memory_client,
                        memory_bank_state_store=self._memory_bank_state_store,
                    )
                    request = await self._deletion_request_store.request(
                        user_id=user_id,
                        scope=self._scope,
                        memory_backend_required=memory_backend_required,
                    )
            except Exception as exc:
                authorization_error = exc
                log.exception(
                    "Privacy deletion: durable authorization failed for %s",
                    user_id,
                )
                return PrivacyDeletionOutcome(
                    ok=False,
                    lines=[
                        (
                            "❌ Your deletion request could not be saved, so nothing "
                            "was deleted. Please try again or ask staff."
                        )
                    ],
                )
            finally:
                authorization_ready.set()
            return await execute(request)

        deletion_task = asyncio.create_task(authorize_and_execute())
        _track_confirmed_privacy_deletion(deletion_task)
        await authorization_ready.wait()
        if authorization_error is not None:
            outcome = await asyncio.shield(deletion_task)
            await interaction.response.edit_message(
                embed=_result_embed("\n".join(outcome.lines), color=discord.Color.red()),
                view=None,
            )
            return

        await interaction.response.defer()
        outcome = await asyncio.shield(deletion_task)
        body = "\n".join(f"- {line}" for line in outcome.lines)
        if outcome.ok and outcome.durable_request_completed is not False:
            embed = _result_embed("✅ Done.\n" + body, color=discord.Color.green())
        else:
            embed = _result_embed(body, color=discord.Color.red())
        await interaction.edit_original_response(embed=embed, view=None)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(
            embed=_result_embed(
                "Cancelled. Nothing was deleted.", color=discord.Color.light_grey()
            ),
            view=None,
        )


_CONFIRM_PROMPTS: dict[DeleteScope, str] = {
    "all": (
        "**This permanently deletes, right now:**\n"
        "- my local copy of every conversation you started, including messages "
        "other people added there\n"
        "- your messages in conversations someone else started\n"
        "- your workspace files\n"
        "- your persistent browser profile and video sessions\n"
        "- your personal memory and persona (and disables future memory)\n\n"
        "Known Gemini video Interactions and uploaded Files are submitted for provider deletion. "
        "It does **not** delete Discord messages, provider safety or diagnostic logs, "
        "backups, community knowledge, skills, usage or moderation records, blocks, "
        "or your saved consent choice.\n\n"
        "This **cannot be undone**. Continue?"
    ),
    "memory": (
        "**This permanently deletes, right now:**\n"
        "- your personal long-term memory and persona (and disables future memory)\n\n"
        "Your conversation history is untouched and still clears on the 30-day "
        "schedule. Community knowledge and skills are also untouched. This "
        "**cannot be undone**. Continue?"
    ),
}


class _PrivacyView(_AuthorGuardedView):
    """The TL;DR's actions: open a delete-confirmation step for each scope."""

    def __init__(
        self,
        *,
        author_id: int,
        conversation_store: ConversationStore,
        preference_store: PreferenceStore,
        memory_client: MemoryClient | None,
        auto_retain_watermarks: AutoRetainStore | None,
        deletion_request_store: PrivacyDeletionRequestStore | None = None,
        memory_bank_state_store: UserMemoryBankStateStore | None = None,
        conversation_turn_lock: ConversationTurnLock | None = None,
        workspace_manager: WorkspaceManager,
        workspace_locks: UserLocks,
        browser_data_store: BrowserDataStore | None = None,
        video_data_store: VideoDataStore | None = None,
        privacy_barrier: UserPrivacyBarrier | None = None,
        cancel_user_work: CancelUserWork | None = None,
        timeout: float = 180.0,
    ) -> None:
        super().__init__(author_id=author_id, timeout=timeout)
        self._conversation_store = conversation_store
        self._preference_store = preference_store
        self._memory_client = memory_client
        self._auto_retain_watermarks = auto_retain_watermarks
        self._deletion_request_store = deletion_request_store
        self._memory_bank_state_store = memory_bank_state_store
        self._conversation_turn_lock = conversation_turn_lock
        self._workspace_manager = workspace_manager
        self._workspace_locks = workspace_locks
        self._browser_data_store = browser_data_store
        self._video_data_store = video_data_store
        self._privacy_barrier = privacy_barrier
        self._cancel_user_work = cancel_user_work

    async def _open_confirm(self, interaction: discord.Interaction, scope: DeleteScope) -> None:
        self.stop()
        confirm = _DeleteConfirmView(
            author_id=self._author_id,
            scope=scope,
            conversation_store=self._conversation_store,
            preference_store=self._preference_store,
            memory_client=self._memory_client,
            auto_retain_watermarks=self._auto_retain_watermarks,
            deletion_request_store=self._deletion_request_store,
            memory_bank_state_store=self._memory_bank_state_store,
            conversation_turn_lock=self._conversation_turn_lock,
            workspace_manager=self._workspace_manager,
            workspace_locks=self._workspace_locks,
            browser_data_store=self._browser_data_store,
            video_data_store=self._video_data_store,
            privacy_barrier=self._privacy_barrier,
            cancel_user_work=self._cancel_user_work,
        )
        await interaction.response.edit_message(
            embed=_result_embed(_CONFIRM_PROMPTS[scope], color=discord.Color.orange()),
            view=confirm,
        )

    @discord.ui.button(label="Delete memory", style=discord.ButtonStyle.secondary, emoji="🧠")
    async def delete_memory(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._open_confirm(interaction, "memory")

    @discord.ui.button(label="Delete my data", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def delete_all(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._open_confirm(interaction, "all")


def register_privacy_command(
    bot: commands.Bot,
    conversation_store: ConversationStore,
    preference_store: PreferenceStore,
    *,
    memory_client: MemoryClient | None = None,
    auto_retain_watermarks: AutoRetainStore | None = None,
    deletion_request_store: PrivacyDeletionRequestStore | None = None,
    memory_bank_state_store: UserMemoryBankStateStore | None = None,
    conversation_turn_lock: ConversationTurnLock | None = None,
    workspace_manager: WorkspaceManager,
    workspace_locks: UserLocks,
    browser_data_store: BrowserDataStore | None = None,
    video_data_store: VideoDataStore | None = None,
    privacy_barrier: UserPrivacyBarrier | None = None,
    cancel_user_work: CancelUserWork | None = None,
    retention_days: int = 30,
    bot_name: str = "",
    policy_url: str = "",
) -> None:
    display_name = bot_name.strip() or "this bot"

    @app_commands.command(
        name="privacy",
        description=f"See how {display_name} handles your data, and delete it on demand",
    )
    async def privacy(interaction: discord.Interaction) -> None:
        view = _PrivacyView(
            author_id=interaction.user.id,
            conversation_store=conversation_store,
            preference_store=preference_store,
            memory_client=memory_client,
            auto_retain_watermarks=auto_retain_watermarks,
            deletion_request_store=deletion_request_store,
            memory_bank_state_store=memory_bank_state_store,
            conversation_turn_lock=conversation_turn_lock,
            workspace_manager=workspace_manager,
            workspace_locks=workspace_locks,
            browser_data_store=browser_data_store,
            video_data_store=video_data_store,
            privacy_barrier=privacy_barrier,
            cancel_user_work=cancel_user_work,
        )
        await interaction.response.send_message(
            embed=_build_tldr_embed(
                retention_days,
                bot_name=bot_name,
                policy_url=policy_url,
            ),
            view=view,
            ephemeral=True,
        )

    bot.tree.add_command(privacy, override=True)
