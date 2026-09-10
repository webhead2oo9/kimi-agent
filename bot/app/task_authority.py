"""Current authority and validation shared by task execution and management."""

from __future__ import annotations
import asyncio
import logging
from dataclasses import replace
from typing import Any
from app.task_access import may_manage
from config.fragments.tool_policy import load_blocked_tools
from moderation.types import Direction
from tools.registry import MessageContext
from tools.scheduled_tasks import TaskDefinition
from tools.task_python import DiscordPythonInput
from storage.task_types import TaskRecord
from app.task_runtime import ScheduledTaskRuntime

log = logging.getLogger(__name__)


class TaskAuthority:
    def __init__(self, runtime: ScheduledTaskRuntime, token: str) -> None:
        self.r, self.token = runtime, token

    async def fresh(self, ctx: MessageContext) -> MessageContext:
        current = await self.r.access.context(
            ctx.guild_id or "",
            ctx.user_id,
            ctx.channel_id,
            run_id=ctx.scheduled_run_id,
        )
        if await self.r.user_blocked(ctx.user_id):
            raise ValueError("Task access is unavailable")
        return replace(ctx, platform_member=current.platform_member, trust_tier=current.trust_tier)

    async def task(self, ctx: MessageContext, task_id: str) -> TaskRecord:
        task = await self.r.store.get(task_id)
        if not may_manage(ctx, task):
            raise ValueError("Task not found")
        return task

    async def guard_run(
        self,
        ctx: MessageContext,
        task: TaskRecord,
        run_id: str,
        *,
        preview_actor: MessageContext | None = None,
        python_tool: str | None = None,
    ) -> None:
        """Recheck the decision/run fence and current owner authority after awaits."""
        if preview_actor is not None:
            actor = await self.fresh(preview_actor)
            latest = await self.task(actor, task["id"])
            self.check_test_revision(latest, actor, task["revision"])
        elif not await self.r.store.live(task["id"], run_id, self.token):
            raise asyncio.CancelledError
        current = await self.fresh(ctx)
        await self.r.access.owner_allowed(current)
        ctx.platform_member = current.platform_member
        ctx.trust_tier = current.trust_tier
        if python_tool is not None:
            await self.python_access(ctx, "run_code")
            if python_tool != "run_code":
                await self.python_access(ctx, python_tool)

    async def validate_definition(
        self,
        ctx: MessageContext,
        definition: TaskDefinition,
        *,
        validate_execution: bool = True,
    ) -> None:
        await self.r.access.owner_allowed(ctx)
        if validate_execution and definition.python is not None:
            await self.python_access(ctx, "run_code")
            for source in definition.python.inputs:
                if isinstance(source, DiscordPythonInput):
                    await self.python_access(ctx, "get_channel_context")
                    await self.r.access.channel(ctx, source.channel_id, posting=False)
                else:
                    await self.python_access(ctx, "fetch_url")
        for target in definition.destinations:
            channel = await self.r.access.channel(ctx, target, posting=True)
            await self.r.access.mentions(
                ctx, channel, definition.mention_users, definition.mention_roles
            )
        if definition.log_channel:
            await self.r.access.channel(ctx, definition.log_channel, posting=True)

    async def python_available(self, ctx: MessageContext) -> bool:
        try:
            await self.python_access(ctx, "run_code")
        except ValueError:
            return False
        return True

    async def python_access(self, ctx: MessageContext, tool: str) -> None:
        config = getattr(self.r.tools, "task_python_sandbox_config", None)
        if config is None or config.network_mode != "none":
            raise ValueError("Scheduled Python requires an available offline code sandbox")
        home = await self.r.access.channel(ctx, ctx.channel_id, posting=False)
        parent = str(getattr(home, "parent_id", None) or home.id)
        blocked = await asyncio.to_thread(load_blocked_tools, ctx.guild_id or "", parent)
        # These application-owned reads do not activate a tool in the conversation and
        # do not inherit the LLM preview's narrower allowlist. All dispatch privileges apply.
        checked = replace(ctx, blocked_tools=blocked, activated_tools={tool})
        error = self.r.tools.registry.dispatch_gate(tool, checked)
        if error is not None:
            raise ValueError(f"Scheduled Python cannot use {tool} with the owner's current access")

    @staticmethod
    def check_test_revision(task: TaskRecord, ctx: MessageContext, revision: int) -> None:
        if task["proposer_id"] != ctx.user_id:
            raise ValueError("Only the person who requested this revision can test it")
        if task["revision"] != revision or task["approval_status"] != "pending":
            raise ValueError("This draft was decided or replaced; use its latest proposal")

    async def moderate(
        self, ctx: MessageContext, text: str, direction: Direction, **kwargs: Any
    ) -> None:
        service = self.r.moderation
        if service is None or not service.enabled:
            return
        decision = await service.check(
            text=text,
            direction=direction,
            user_id=ctx.user_id,
            channel_id=ctx.channel_id,
            thread_id=ctx.thread_id,
            trust_tier=ctx.trust_tier.value,
            **kwargs,
        )
        if decision.blocked:
            raise ValueError(service.refusal_for(direction))
