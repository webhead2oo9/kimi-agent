"""Approval decisions and independently reconciled Discord receipts."""

from __future__ import annotations
from collections.abc import Mapping
import asyncio
import io
import logging
import time
from collections.abc import Callable
from typing import Any
import discord
from moderation.types import Direction
from storage.task_previews import TaskPreviewStore
from app.task_preview import render_preview, render_task_details
from app.thread_handoff_boundary import ThreadHandoffBoundary
from tools.threads import ThreadRequest
from tools.registry import TaskPreviewRequest
from tools.scheduled_tasks import TaskDefinition
from app.task_runtime import ScheduledTaskRuntime

from app.task_authority import TaskAuthority

log = logging.getLogger(__name__)


class TaskApprovals:
    def __init__(
        self,
        runtime: ScheduledTaskRuntime,
        authority: TaskAuthority,
        confirmation: Callable[[str, int], discord.ui.View],
        manage_view: Callable[[str], discord.ui.View],
    ) -> None:
        self.r, self.authority = runtime, authority
        self.previews = TaskPreviewStore(runtime.store.db)
        self.confirmation, self.manage_view = confirmation, manage_view
        self._locks: dict[str, asyncio.Lock] = {}

    @staticmethod
    def _preview(task: Mapping[str, Any], definition: TaskDefinition) -> str:
        return render_preview(task, definition, now=time.time())

    async def deliver_preview(
        self,
        message: discord.Message,
        threads: ThreadHandoffBoundary,
        conversation_id: int,
        request: TaskPreviewRequest,
        context_key: str,
    ) -> str:
        try:
            ctx = await self.r.access.context(
                str(message.guild.id) if message.guild else "",
                str(message.author.id),
                str(message.channel.id),
            )
            ctx = await self.authority.fresh(ctx)
            task = await self.authority.task(ctx, request.task_id)
            if task["revision"] != request.revision or task["proposer_id"] != ctx.user_id:
                raise ValueError("This draft was replaced; request its latest preview")
            definition = TaskDefinition.model_validate(task["definition"])
            await self.authority.validate_definition(ctx, definition)
            target = await self.r.access.channel(ctx, ctx.channel_id, posting=False)
            fallback = False
            if not request.in_channel and not isinstance(target, discord.Thread):
                try:
                    thread = await threads.create_handoff_thread(
                        message,
                        ThreadRequest(
                            name=f"Task approval: {definition.name}"[:100], auto_respond=False
                        ),
                        conversation_id,
                    )
                except discord.HTTPException:
                    log.warning(
                        "Could not open approval thread; using current channel", exc_info=True
                    )
                    thread = None
                if thread is not None:
                    target = thread
                else:
                    fallback = True
            preview = self._preview(task, definition)
            await self.authority.moderate(ctx, preview, Direction.OUTPUT)
            files = [
                discord.File(
                    io.BytesIO(render_task_details(task, definition).encode()),
                    filename="task-details.md",
                ),
                discord.File(io.BytesIO(definition.skill.encode()), filename="SKILL.md"),
            ]
            if definition.python is not None:
                files.append(
                    discord.File(io.BytesIO(definition.python.code.encode()), filename="task.py")
                )
            try:
                sent = await target.send(
                    preview,
                    view=self.confirmation(task["id"], task["revision"]),
                    files=files,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            finally:
                for file in files:
                    file.close()
            await self.previews.remember(task["id"], task["revision"], str(target.id), str(sent.id))
            notice = f"Task pending approval: [Review task]({sent.jump_url})"
            if fallback:
                notice = "I couldn't open an approval thread, so the preview is here. " + notice
            return notice
        except (ValueError, OSError, discord.HTTPException) as exc:
            log.warning("Task preview delivery failed", exc_info=True)
            return f"The draft is saved, but I couldn't provide its approval message: {str(exc)[:300]}. Ask me to retry the draft."

    async def confirm(
        self, interaction: discord.Interaction, task_id: str, revision: int, *, approve: bool = True
    ) -> str:
        if interaction.guild_id is None or interaction.channel_id is None:
            raise ValueError("Use this preview in its server")
        ctx = await self.r.access.context(
            str(interaction.guild_id), str(interaction.user.id), str(interaction.channel_id)
        )
        async with self.r.privacy.activity(ctx.user_id):
            ctx = await self.authority.fresh(ctx)
            task = await self.authority.task(ctx, task_id)
            record = await self.r.store.revision_approval(task_id, revision)
            if record is None or record[0] != ctx.user_id:
                raise ValueError(
                    "Only the person who requested this revision can approve or deny it"
                )
            if interaction.message is not None:
                await self.previews.remember(
                    task_id, revision, str(interaction.channel_id), str(interaction.message.id)
                )
            if record[1] != "pending" or task["revision"] != revision:
                if interaction.message is not None:
                    await self.reconcile(message_id=str(interaction.message.id))
                return "This revision has already been decided or superseded; nothing was changed."
            if approve:
                owner = await self.r.access.context(
                    task["guild_id"], task["owner_id"], task["channel_id"]
                )
                owner = await self.authority.fresh(owner)
                definition = TaskDefinition.model_validate(task["definition"])
                await self.authority.validate_definition(owner, definition)
                next_run = definition.schedule.next_after(time.time())
                if next_run is None:
                    raise ValueError("The proposed execution time has passed; edit the task")
                await self.r.store.activate(
                    task_id, revision, ctx.user_id, next_run, reset_state=definition.reset_state
                )
            else:
                await self.r.store.reject(task_id, revision, ctx.user_id)
            updated = True
            if interaction.message is not None:
                updated = await self.reconcile(message_id=str(interaction.message.id))
            return (
                "Task activated."
                if approve
                else "Task denied. Any previously approved version is unchanged."
            ) + (
                ""
                if updated
                else " The decision succeeded, but its receipt or thread closure could not be completed; a retry is queued."
            )

    async def reconcile(self, *, message_id: str | None = None) -> bool:
        success = True
        for row in await self.previews.updates(message_id=message_id):
            try:
                # Confirmations already hold privacy activity: acquire it before the shared
                # reconciliation lock so a pending deletion cannot invert the lock order.
                async with (
                    self.r.privacy.activity(row["owner_id"]),
                    self._locks.setdefault(row["message_id"], asyncio.Lock()),
                ):
                    current = await self.previews.updates(message_id=row["message_id"])
                    if not current:
                        continue
                    row = current[0]
                    channel = self.r.bot.get_channel(int(row["channel_id"]))
                    if channel is None:
                        channel = await self.r.bot.fetch_channel(int(row["channel_id"]))
                    if not isinstance(channel, discord.TextChannel | discord.Thread):
                        raise ValueError("Approval channel unavailable")
                    if str(channel.guild.id) != row["guild_id"]:
                        raise ValueError("Approval server mismatch")
                    definition = TaskDefinition.model_validate_json(row["definition_json"])
                    needed_by_task = bool(
                        row["close_pending"]
                        and isinstance(channel, discord.Thread)
                        and await self.previews.thread_in_use(row["guild_id"], row["channel_id"])
                    )
                    thread_closed = bool(
                        isinstance(channel, discord.Thread) and channel.archived and channel.locked
                    )
                    if row["rendered_state"] != row["render_key"] and not thread_closed:
                        await channel.get_partial_message(int(row["message_id"])).edit(
                            content=render_preview(
                                row,
                                definition,
                                now=time.time(),
                                state=row["desired_state"],
                                next_run=row["task_next_run"],
                            )
                            + (
                                "\nThread left open because an approved task uses it."
                                if needed_by_task
                                else ""
                            ),
                            view=(
                                self.confirmation(row["task_id"], row["revision"])
                                if row["desired_state"] == "pending"
                                else (
                                    self.manage_view(row["task_id"])
                                    if row["desired_state"] == "activated"
                                    else None
                                )
                            ),
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                    if (
                        row["close_pending"]
                        and isinstance(channel, discord.Thread)
                        and not thread_closed
                        and not needed_by_task
                    ):
                        # Decisions remain valid even when thread management isn't permitted.
                        try:
                            member = await channel.guild.fetch_member(int(row["proposer_id"]))
                            owner_allowed = (
                                channel.owner_id == member.id
                                or channel.permissions_for(member).manage_threads
                            )
                            if not owner_allowed:
                                creator = await self.r.conversations.get_thread_creator_user_id(
                                    str(channel.id)
                                )
                                owner_allowed = creator == str(member.id)
                            bot_member = channel.guild.me
                            if (
                                owner_allowed
                                and bot_member
                                and channel.permissions_for(bot_member).manage_threads
                            ):
                                if row["signoff_message_id"] is None:
                                    signoff = (
                                        "Task approved. Use `/tasks` to manage it or view run history."
                                        if row["desired_state"] == "activated"
                                        else "Task rejected. Any previously approved version keeps running."
                                    )
                                    sent = await channel.send(
                                        signoff + " Closing this approval thread.",
                                        allowed_mentions=discord.AllowedMentions.none(),
                                    )
                                    await self.previews.signoff_sent(
                                        row["message_id"], str(sent.id)
                                    )
                                await channel.edit(
                                    locked=True, archived=True, reason="Task approval decided"
                                )
                                thread_closed = True
                        except discord.Forbidden:
                            pass
                    await self.previews.rendered(
                        row["message_id"],
                        row["render_key"],
                        closed=True,
                        thread_closed=thread_closed,
                    )
            except discord.HTTPException, ValueError, OSError:
                log.warning("Could not update task approval message", exc_info=True)
                await self.previews.failed(row["message_id"])
                success = False
        return success
