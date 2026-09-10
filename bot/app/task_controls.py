"""Proposal-style Discord controls with private, freshly authorized task management."""

from __future__ import annotations

import io
import json
import logging
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import discord

from app.task_preview import clip, native_time, schedule_label
from storage.conversations import ChannelMessageRecord
from tools.registry import MessageContext
from tools.scheduled_tasks import TaskDefinition
from trust.tiers import TrustTier
from utils.privacy_barrier import PrivacyDeletionPendingError

if TYPE_CHECKING:
    from app.scheduled_tasks import ScheduledTaskService

log = logging.getLogger(__name__)


class TaskManageEntry(discord.ui.View):
    """Durable entry point; all details and actions are resolved at click time."""

    def __init__(self, service: ScheduledTaskService, task_id: str) -> None:
        super().__init__(timeout=None)
        button: discord.ui.Button[TaskManageEntry] = discord.ui.Button(
            label="Manage", custom_id=f"task-manage:{task_id}", style=discord.ButtonStyle.primary
        )

        async def callback(interaction: discord.Interaction) -> None:
            await TaskControls(service).handle(interaction, "inspect", task_id=task_id)

        button.callback = callback  # type: ignore[method-assign]
        self.add_item(button)


class TaskPanel(discord.ui.View):
    def __init__(self, controls: TaskControls, user_id: str) -> None:
        super().__init__(timeout=600)
        self.controls, self.user_id = controls, user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if str(interaction.user.id) == self.user_id:
            return True
        await interaction.response.send_message("Open your own panel with /tasks.", ephemeral=True)
        return False

    def action(
        self,
        label: str,
        action: str,
        *,
        task_id: str = "",
        page: int = 0,
        revision: int | None = None,
        style: discord.ButtonStyle = discord.ButtonStyle.secondary,
    ) -> None:
        button: discord.ui.Button[TaskPanel] = discord.ui.Button(label=label, style=style)

        async def callback(interaction: discord.Interaction) -> None:
            if action == "answer":
                await interaction.response.send_modal(TaskAnswer(self.controls, task_id))
            else:
                await self.controls.handle(
                    interaction, action, task_id=task_id, page=page, revision=revision
                )

        button.callback = callback  # type: ignore[method-assign]
        self.add_item(button)

    def tasks(self, rows: list[dict[str, Any]]) -> None:
        select: discord.ui.Select[TaskPanel] = discord.ui.Select(
            placeholder="Choose a task",
            options=[
                discord.SelectOption(
                    label=clip(row["name"], 100),
                    value=row["id"],
                    description=f"{row['status']} · {row['id'][:8]}",
                )
                for row in rows
            ],
        )

        async def callback(interaction: discord.Interaction) -> None:
            await self.controls.handle(interaction, "inspect", task_id=select.values[0])

        select.callback = callback  # type: ignore[method-assign]
        self.add_item(select)


class TaskAnswer(discord.ui.Modal, title="Answer and resume task"):
    answer: discord.ui.TextInput = discord.ui.TextInput(
        label="Your answer", style=discord.TextStyle.paragraph, max_length=4000
    )

    def __init__(self, controls: TaskControls, task_id: str) -> None:
        super().__init__()
        self.controls, self.task_id = controls, task_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.controls.handle(
            interaction, "resume", task_id=self.task_id, answer=str(self.answer)
        )


class TaskControls:
    def __init__(self, service: ScheduledTaskService) -> None:
        self.service = service

    async def handle(
        self,
        interaction: discord.Interaction,
        action: str,
        *,
        task_id: str = "",
        page: int = 0,
        revision: int | None = None,
        answer: str | None = None,
    ) -> None:
        private_panel = bool(
            getattr(getattr(interaction.message, "flags", None), "ephemeral", False)
        )
        await interaction.response.defer(
            ephemeral=True, thinking=not private_panel or action == "test_preview"
        )
        try:
            if interaction.guild_id is None or interaction.channel_id is None:
                raise ValueError("Use /tasks in the task's server")
            async with self.service.r.privacy.activity(str(interaction.user.id)):
                ctx = await self.service.r.access.context(
                    str(interaction.guild_id),
                    str(interaction.user.id),
                    str(interaction.channel_id),
                )
                ctx = await self.service.fresh(ctx)
                if action == "list":
                    await self._list(interaction, ctx, page)
                    return
                task = await self.service._task(ctx, task_id)
                if action == "test_preview":
                    if revision is None:
                        raise ValueError("Choose the proposal revision to test")
                    result = await self.service.test_preview(ctx, task_id, revision)
                    await self._test_result(interaction, result)
                    return
                if action == "history":
                    await self._history(interaction, ctx, task)
                    return
                if action == "edit":
                    await self._edit(interaction, ctx, task)
                    return
                if action == "delete":
                    view = TaskPanel(self, ctx.user_id)
                    view.action(
                        "Delete task",
                        "delete_confirmed",
                        task_id=task_id,
                        style=discord.ButtonStyle.danger,
                    )
                    view.action("Cancel", "inspect", task_id=task_id)
                    await self._send(
                        interaction,
                        content="Delete this task and its saved instructions, state, and run history?",
                        view=view,
                    )
                    return
                notice = ""
                if action != "inspect":
                    result = json.loads(
                        await self.service.manage(
                            {
                                "action": "delete" if action == "delete_confirmed" else action,
                                "task_id": task_id,
                                "answer": answer,
                            },
                            ctx,
                        )
                    )
                    if "error" in result:
                        raise ValueError(result["error"])
                    if action == "delete_confirmed":
                        await self._list(interaction, ctx, 0, notice="Task deleted.")
                        return
                    notice = {
                        "pause": "Task paused.",
                        "resume": "Task resumed.",
                        "run_now": "Run requested.",
                        "retry_delivery": "Saved delivery queued for retry.",
                    }.get(action, "Task updated.")
                    task = await self.service._task(ctx, task_id)
                await self._detail(interaction, ctx, task, notice=notice)
        except TimeoutError:
            await self._send(
                interaction, content="The test preview timed out. The proposal is still pending."
            )
        except (ValueError, OSError, discord.HTTPException, PrivacyDeletionPendingError) as exc:
            await self._send(interaction, content=clip(str(exc), 1800))
        except Exception:
            log.exception("Task control request failed")
            await self._send(
                interaction,
                content="I couldn't finish this request. Reopen /tasks to check its current status.",
            )

    @staticmethod
    async def _send(interaction: discord.Interaction, **kwargs: Any) -> None:
        if (
            getattr(interaction.response, "type", None)
            == discord.InteractionResponseType.deferred_message_update
        ):
            file = kwargs.pop("file", None)
            await interaction.edit_original_response(
                content=kwargs.pop("content", None),
                embed=kwargs.pop("embed", None),
                view=kwargs.pop("view", None),
                attachments=[file] if file else [],
                allowed_mentions=discord.AllowedMentions.none(),
                **kwargs,
            )
            return
        await interaction.followup.send(
            **kwargs, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
        )

    async def _list(
        self, interaction: discord.Interaction, ctx: MessageContext, page: int, *, notice: str = ""
    ) -> None:
        page = max(0, page)
        rows = await self.service.r.store.list_tasks(
            ctx.guild_id or "",
            None if ctx.trust_tier >= TrustTier.STAFF else ctx.user_id,
            limit=11,
            offset=page * 10,
        )
        shown = rows[:10]
        lines = [
            f"**{clip(row['name'], 100)}** · {row['status']}\n"
            + (
                f"Next: <t:{int(row['next_run'])}:R>"
                if row["status"] == "active" and row["next_run"]
                else "No run scheduled"
            )
            for row in shown
        ]
        embed = discord.Embed(
            title="Scheduled tasks",
            description="\n\n".join(lines) or "No tasks on this page.",
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=f"Page {page + 1} · Ask me to create a task in conversation.")
        view = TaskPanel(self, ctx.user_id)
        if shown:
            view.tasks(shown)
        if page:
            view.action("Previous", "list", page=page - 1)
        if len(rows) > 10:
            view.action("Next", "list", page=page + 1)
        view.action("Refresh", "list", page=page)
        await self._send(interaction, content=notice or None, embed=embed, view=view)

    async def _detail(
        self,
        interaction: discord.Interaction,
        ctx: MessageContext,
        task: dict[str, Any],
        *,
        notice: str = "",
    ) -> None:
        active = (
            await self.service.r.store.get(task["id"], active=True)
            if task["active_revision"] is not None
            else task
        )
        definition = TaskDefinition.model_validate(active["definition"])
        history = await self.service.r.store.history(task["id"])
        latest = history[0] if history else None
        embed = discord.Embed(
            title=clip(definition.name, 100),
            description=clip(definition.objective, 1000),
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Status", value=task["status"].replace("attention", "Needs attention"))
        embed.add_field(
            name="Schedule", value=f"{schedule_label(definition)}\n{definition.schedule.timezone}"
        )
        embed.add_field(
            name="Next run",
            value=(
                native_time(task["next_run"])
                if task["status"] == "active" and task["next_run"]
                else "None"
            ),
            inline=False,
        )
        embed.add_field(
            name="Destinations", value=", ".join(f"<#{x}>" for x in definition.destinations)
        )
        if latest:
            embed.add_field(
                name="Last run",
                value=f"{latest['status']} · <t:{int(latest['created_at'])}:R>\n"
                + clip(latest["detail"], 800),
                inline=False,
            )
        pending = task["approval_status"] == "pending"
        if pending:
            embed.add_field(
                name="Pending proposal",
                value=f"Revision {task['revision']} awaits approval.",
                inline=False,
            )
        embed.set_footer(
            text=f"Task {task['id']} · Approved revision {task['active_revision'] or 'none'}"
        )
        view = TaskPanel(self, ctx.user_id)
        if task["active_revision"] is not None:
            if task["status"] == "active":
                view.action("Pause", "pause", task_id=task["id"])
            elif task["status"] in {"paused", "attention"} and (
                latest is None or latest["status"] not in {"needs_input", "delivery_failed"}
            ):
                view.action("Resume", "resume", task_id=task["id"])
            view.action("Run now", "run_now", task_id=task["id"])
            if latest and latest["status"] == "needs_input":
                view.action("Answer & resume", "answer", task_id=task["id"])
            if await self.service.r.store.can_retry_delivery(task["id"]):
                view.action("Retry delivery", "retry_delivery", task_id=task["id"])
        if pending and task["proposer_id"] == ctx.user_id:
            view.action(
                "Test preview", "test_preview", task_id=task["id"], revision=task["revision"]
            )
            async with self.service.r.store.db.conn.execute(
                "SELECT channel_id,message_id FROM scheduled_task_previews WHERE task_id=? "
                "AND revision=? ORDER BY rowid DESC LIMIT 1",
                (task["id"], task["revision"]),
            ) as cursor:
                proposal = await cursor.fetchone()
            if proposal:
                view.add_item(
                    discord.ui.Button(
                        label="Open proposal",
                        url=f"https://discord.com/channels/{ctx.guild_id}/{proposal['channel_id']}/{proposal['message_id']}",
                    )
                )
        view.action("History", "history", task_id=task["id"])
        view.action("Edit", "edit", task_id=task["id"])
        view.action("Delete", "delete", task_id=task["id"], style=discord.ButtonStyle.danger)
        view.action("All tasks", "list")
        await self._send(interaction, content=notice or None, embed=embed, view=view)

    async def _history(
        self, interaction: discord.Interaction, ctx: MessageContext, task: dict[str, Any]
    ) -> None:
        result = json.loads(
            await self.service.manage({"action": "history", "task_id": task["id"]}, ctx)
        )
        if "error" in result:
            raise ValueError(result["error"])
        entries = []
        for run in result["runs"][:8]:
            links = [
                f"[Message](https://discord.com/channels/{ctx.guild_id}/{d['channel_id']}/{d['message_id']})"
                for d in result["deliveries"]
                if d["run_id"] == run["id"] and d["status"] == "sent"
            ][:3]
            entries.append(
                f"**{run['status']}** · <t:{int(run['created_at'])}:f>\n"
                + clip(run["detail"], 250)
                + ("\n" + " · ".join(links) if links else "")
            )
        embed = discord.Embed(
            title="Task history", description="\n\n".join(entries)[:4000] or "No runs yet."
        )
        view = TaskPanel(self, ctx.user_id)
        view.action("Back to task", "inspect", task_id=task["id"])
        # The attachment preserves detailed delivery errors and the full retained history.
        file = discord.File(
            io.BytesIO(json.dumps(result, indent=2).encode()), filename="task-history.json"
        )
        try:
            await self._send(interaction, embed=embed, view=view, file=file)
        finally:
            file.close()

    async def _edit(
        self, interaction: discord.Interaction, ctx: MessageContext, task: dict[str, Any]
    ) -> None:
        await self.service.r.access.owner_allowed(ctx)
        channel = await self.service.r.access.channel(ctx, ctx.channel_id, posting=False)
        prompt = f"Editing task **{clip(task['definition']['name'], 100)}**. Reply to this message with the changes you want."
        message = await channel.send(prompt, allowed_mentions=discord.AllowedMentions.none())
        key = f"task-edit:{task['id']}:{message.id}"
        conversation_id = await self.service.r.conversations.get_or_create(
            key,
            channel.name,
            guild_id=ctx.guild_id,
            channel_id=ctx.channel_id,
            root_discord_message_id=str(message.id),
            owner_user_id=ctx.user_id,
            access_scope="owner_only",
        )
        await self.service.r.conversations.save_channel_messages(
            conversation_id,
            [
                ChannelMessageRecord(
                    discord_message_id=str(message.id),
                    role="assistant",
                    author_id=None,
                    author_name=None,
                    content=prompt,
                    source_created_at=message.created_at.timestamp(),
                )
            ],
            context_channel_id=ctx.channel_id,
        )
        await self.service._bind_wizard(replace(ctx, context_key=key), task["id"])
        await self._send(
            interaction,
            content=f"[Continue editing here]({message.jump_url}). Changes will need a new approval.",
        )

    async def _test_result(self, interaction: discord.Interaction, result: dict[str, Any]) -> None:
        outcome = {
            "completed": "Would publish" if result["posts"] else "Would finish without a post",
            "no_change": "Would not publish",
            "needs_input": "Needs input or unsupported action",
        }[result["outcome"]]
        report = [
            f"# Test preview: {result['task_name']} (revision {result['revision']})",
            outcome,
            result["detail"],
        ]
        for post in result["posts"]:
            report.extend([f"\n## Destination: {post['channel_id']}", post["content"]])
        samples = result.get("files", [])
        if samples:
            report.append("## Sample files\n" + "\n".join(name for name, _, _ in samples))
        embed = discord.Embed(
            title=clip(f"Test preview: {result['task_name']}", 200),
            description=f"**{outcome}**\n{clip(result['detail'], 1200)}",
            color=discord.Color.blurple(),
        )
        if result["posts"]:
            first = result["posts"][0]
            embed.add_field(
                name=f"Sample for #{first['channel_id']}",
                value=first["content"][:1000],
                inline=False,
            )
        embed.set_footer(
            text=f"Revision {result['revision']} · Nothing published; schedule and saved state unchanged."
        )
        file = discord.File(
            io.BytesIO("\n\n".join(report).encode()), filename="task-test-preview.md"
        )
        try:
            await self._send(interaction, embed=embed, file=file)
        finally:
            file.close()
        for offset in range(0, len(samples), 10):
            attachments = [
                discord.File(io.BytesIO(data), filename=name, description=description)
                for name, description, data in samples[offset : offset + 10]
            ]
            try:
                await interaction.followup.send(
                    content="Private sample files from this test preview.",
                    files=attachments,
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            finally:
                for attachment in attachments:
                    attachment.close()
