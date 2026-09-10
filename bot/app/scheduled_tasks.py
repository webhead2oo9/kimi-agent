"""Compose scheduled-task management, execution, approval UI, and background workers."""

from __future__ import annotations

import json
import uuid
from typing import Any

import discord
from discord import app_commands

from app.task_runtime import ScheduledTaskRuntime as ScheduledTaskRuntime, ActiveRuns
from app.task_authority import TaskAuthority
from app.task_manager import TaskManager
from app.task_executor import TaskExecutor
from app.task_publisher import TaskPublisher
from app.task_approvals import TaskApprovals
from app.task_scheduler import TaskScheduler
from app.task_controls import TaskControls, TaskConfirmation, TaskManageEntry
from app.thread_handoff_boundary import ThreadHandoffBoundary
from tools.registry import MessageContext, TaskPreviewRequest
from tools.scheduled_tasks import init_task_tools
from tools._common import tool_error


class ScheduledTaskService:
    def __init__(self, runtime: ScheduledTaskRuntime) -> None:
        self.r = runtime
        self.runs = ActiveRuns()
        self.authority = TaskAuthority(runtime, uuid.uuid4().hex)
        self.publisher = TaskPublisher(runtime, self.authority, self.runs)
        self.executor = TaskExecutor(runtime, self.authority, self.runs, self.publisher)
        self.manager = TaskManager(runtime, self.authority, self._cancel)
        self.approvals = TaskApprovals(
            runtime,
            self.authority,
            lambda task_id, revision: TaskConfirmation(self.controls, task_id, revision),
            lambda task_id: TaskManageEntry(self.controls, task_id),
        )
        self.controls = TaskControls(
            runtime, self.authority, self.manager, self.executor, self.approvals
        )
        self.scheduler = TaskScheduler(
            runtime, self.authority, self.runs, self.executor, self.publisher, self.approvals
        )
        init_task_tools(runtime.tools.registry, self.manage, self.post, self.discover)
        runtime.tools.registry.prompt_instructions = self.manager.wizard_instructions
        runtime.tools.plugin_privacy_callbacks.register(
            "core_scheduled_tasks", self.scheduler.delete_user, scopes=frozenset({"all"})
        )
        self._register_command()

    async def _cancel(self, task_id: str) -> None:
        await self.scheduler.cancel(task_id)

    async def manage(self, args: dict[str, Any], ctx: MessageContext) -> str:
        try:
            return json.dumps(await self.manager.dispatch(args, ctx))
        except (ValueError, TypeError, OSError, discord.HTTPException) as exc:
            return tool_error(str(exc))

    async def post(self, args: dict[str, Any], ctx: MessageContext) -> str:
        try:
            return json.dumps(await self.publisher.post(args, ctx))
        except (ValueError, discord.HTTPException) as exc:
            return tool_error(str(exc))

    async def discover(self, args: dict[str, Any], ctx: MessageContext) -> str:
        try:
            return json.dumps(await self.manager.discover(args, ctx))
        except (ValueError, discord.HTTPException) as exc:
            return tool_error(str(exc))

    async def deliver_preview(
        self,
        message: discord.Message,
        threads: ThreadHandoffBoundary,
        conversation_id: int,
        request: TaskPreviewRequest,
        context_key: str,
    ) -> str:
        return await self.approvals.deliver_preview(
            message, threads, conversation_id, request, context_key
        )

    async def start(self) -> None:
        revisions, tasks = await self.r.store.persistent_views()
        for task_id, revision in revisions:
            self.r.bot.add_view(TaskConfirmation(self.controls, task_id, revision))
        for task_id in tasks:
            self.r.bot.add_view(TaskManageEntry(self.controls, task_id))
        await self.scheduler.start()

    async def close(self) -> None:
        await self.scheduler.close()

    def _register_command(self) -> None:
        @app_commands.command(name="tasks", description="Inspect and manage your scheduled tasks")
        @app_commands.guild_only()
        @app_commands.choices(
            action=[
                app_commands.Choice(name=name, value=name)
                for name in (
                    "list",
                    "inspect",
                    "history",
                    "pause",
                    "resume",
                    "run_now",
                    "retry_delivery",
                    "delete",
                )
            ]
        )
        async def tasks(
            interaction: discord.Interaction,
            action: str = "list",
            task_id: str = "",
            answer: str | None = None,
        ) -> None:
            await self.controls.handle(interaction, action, task_id=task_id, answer=answer)

        self.r.bot.tree.add_command(tasks)
