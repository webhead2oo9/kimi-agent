"""Private coding delivery and the shared scheduled-task review actions."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from contextlib import nullcontext
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any

from aiohttp import web

from agent.context import ConversationContext
from app.admission import TurnAdmissionController
from app.cancellation import ActiveOperationRegistry
from app.coding_delivery import CodingDelivery, CodingTaskController
from app.dashboard_access import DashboardAccess
from app.dashboard_files import DashboardFiles
from app.root_locks import RootLockPool
from app.scheduled_tasks import ScheduledTaskService
from app.task_preview import render_preview, render_task_details
from config.fragments.tool_policy import load_blocked_tools
from moderation.types import Direction
from storage.coding_tasks import ACTIVE_TASK_STATUSES, CodingTask
from storage.conversations import ChannelMessageRecord
from storage.dashboard import DashboardConversation, DashboardStore
from tools.registry import MessageContext, TaskPreviewRequest
from tools.scheduled_tasks import TaskDefinition
from utils.asyncio import await_uncancellable
from utils.privacy_barrier import UserPrivacyBarrier
from workspace import WorkspaceKey, workspace_owner_key

log = logging.getLogger(__name__)


class DashboardTasks:
    def __init__(
        self,
        *,
        store: DashboardStore,
        files: DashboardFiles,
        access: DashboardAccess,
        coding: CodingTaskController,
        delivery: CodingDelivery,
        scheduled: ScheduledTaskService,
        roots: RootLockPool,
        privacy: UserPrivacyBarrier,
        operations: ActiveOperationRegistry,
        admission: TurnAdmissionController,
    ) -> None:
        self.store, self.files, self.access = store, files, access
        self.coding, self.delivery, self.scheduled = coding, delivery, scheduled
        self.roots, self.privacy, self.operations, self.admission = (
            roots,
            privacy,
            operations,
            admission,
        )
        self._workers: set[asyncio.Task[None]] = set()
        self._closed = False

    async def context(self, chat: DashboardConversation) -> MessageContext:
        current = await self.access.resolve(
            user_id=chat.user_id,
            guild_id=chat.guild_id,
            channel_id=chat.channel_id,
            continuing=True,
        )
        if await self.access.consent_required(chat.user_id):
            raise web.HTTPForbidden(reason="Accept the privacy notice before using tasks")
        return MessageContext(
            user_id=chat.user_id,
            user_name=current.member.display_name,
            guild_id=chat.guild_id,
            channel_id=chat.channel_id,
            thread_id=chat.channel_id if chat.parent_channel_id != chat.channel_id else None,
            trust_tier=current.tier,
            platform_member=current.member,
            context_key=chat.key,
            conversation_id=chat.conversation_id,
            workspace_key_override=workspace_owner_key(chat.user_id, chat.guild_id),
            blocked_tools=await asyncio.to_thread(
                load_blocked_tools, chat.guild_id, chat.parent_channel_id
            ),
        )

    async def preview(
        self, chat: DashboardConversation, request: TaskPreviewRequest
    ) -> dict[str, Any]:
        ctx = await self.context(chat)
        task = await self.scheduled.authority.task(ctx, request.task_id)
        if task["revision"] != request.revision or task["proposer_id"] != chat.user_id:
            raise ValueError("The proposal was replaced. Ask for its latest preview")
        definition = TaskDefinition.from_stored(task["definition"])
        await self.scheduled.authority.validate_definition(ctx, definition)
        text = render_preview(task, definition, now=time.time())
        # Discord timestamp/mention markup has no native meaning in an Activity.
        # Export the same schedule in its explicit timezone with channel links.
        text = re.sub(
            r"<t:(\d+):F> · <t:\1:R>",
            lambda match: datetime.fromtimestamp(
                int(match[1]), ZoneInfo(definition.schedule.timezone)
            ).isoformat(sep=" "),
            text,
        ).replace("(your local time)", f"({definition.schedule.timezone})")
        text = re.sub(
            r"<#(\d+)>",
            lambda match: (
                f"[Channel {match[1]}](https://discord.com/channels/{chat.guild_id}/{match[1]})"
            ),
            text,
        )
        result = {
            "id": task["id"],
            "revision": task["revision"],
            "name": definition.name,
            "status": task["approval_status"],
            "text": text,
            "details": render_task_details(task, definition),
            "skill": definition.skill,
            "python": definition.python.code if definition.python else None,
        }
        await self.scheduled.authority.moderate(ctx, json.dumps(result), Direction.OUTPUT)
        await self.store.link_task(chat, request.task_id, request.revision)
        return result

    async def states(self, chat: DashboardConversation) -> list[dict[str, Any]]:
        result = []
        for task_id, revision in await self.store.linked_tasks(chat):
            try:
                task = await self.scheduled.r.store.get(task_id)
                approval = await self.scheduled.r.store.revision_approval(task_id, revision)
                if (
                    task["guild_id"] != chat.guild_id
                    or approval is None
                    or approval[0] != chat.user_id
                ):
                    continue
                result.append(
                    {
                        "id": task_id,
                        "revision": revision,
                        "status": approval[1] if task["revision"] == revision else "superseded",
                    }
                )
            except ValueError:
                continue
        return result

    async def publish_coding(self, task: CodingTask, context: ConversationContext | None) -> None:
        if task.delivery_surface != "dashboard":
            raise ValueError("Expected a private dashboard task")
        terminal = task.status not in ACTIVE_TASK_STATUSES
        # Progress can be published while the coding worker owns the workspace.
        # A foreground turn holds the chat root while waiting for that workspace,
        # so only terminal delivery (after the writer exits) may acquire the root.
        # event() already discards late progress for a deleted conversation.
        root_guard = self.roots.hold(task.root_key) if terminal else nullcontext()
        async with root_guard:
            chat = await self.store.for_root(task.root_key)
            if chat is None or chat.user_id != task.user_id or chat.guild_id != task.guild_id:
                return
            if task.delivery_state == "delivered":
                return
            event_key = f"coding:{task.id}:final"
            if terminal and await self.store.event_by_key(chat.id, event_key):
                await self.coding.store.mark_delivered(task.id, None)
                return
            if terminal:
                text = (
                    task.result_text.strip()
                    or task.error_text.strip()
                    or f"Coding task {task.status.value}."
                )
            else:
                text = f"**{task.display_summary or 'Coding task'}**\n{task.milestone}"
                for step in task.plan[:30]:
                    marker = "x" if step.get("status") == "completed" else " "
                    text += f"\n- [{marker}] {step.get('content', '')[:500]}"
            moderated = await self.delivery.moderate_text(task, text, status=not terminal)
            payload: dict[str, Any] = {
                "id": task.id,
                "status": task.status.value,
                "text": moderated.text,
                "files": [],
            }
            if terminal:
                delivery = task.checkpoint.get("delivery", {})
                paths = delivery.get("output_files", []) if isinstance(delivery, dict) else []
                if not moderated.blocked and isinstance(paths, list):
                    async with self.files.locks.activity(WorkspaceKey(task.workspace_key)):
                        payload["files"] = await self.files.snapshot(
                            chat,
                            tuple(str(p) for p in paths),
                            source_context=f"coding-delivery-{task.id}",
                            workspace_guard_held=True,
                        )
                if not moderated.blocked:
                    await self.store.conversations.save_channel_messages(
                        chat.conversation_id,
                        [
                            ChannelMessageRecord(
                                None, "assistant", None, None, moderated.text, source_id=event_key
                            ),
                        ],
                        context_channel_id=chat.channel_id,
                    )
                await self.store.event(chat.id, "coding_task", payload, key=event_key)
                await self.coding.store.mark_delivered(task.id, None)
            else:
                digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[
                    :20
                ]
                await self.store.event(
                    chat.id,
                    "coding_task",
                    payload,
                    key=f"coding:{task.id}:{task.updated_at}:{digest}",
                )

    async def submit_action(
        self,
        chat: DashboardConversation,
        *,
        request_id: str,
        task_id: str,
        action: str,
        revision: int = 0,
        message: str = "",
    ) -> str:
        if self._closed:
            raise web.HTTPServiceUnavailable(reason="The bot is shutting down")
        if not request_id or len(request_id) > 100 or len(task_id) > 100 or len(message) > 8000:
            raise web.HTTPBadRequest(reason="Invalid task action")
        if action not in {"approve", "reject", "test", "steer", "cancel"}:
            raise web.HTTPBadRequest(reason="Unknown task action")
        ready: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        worker = asyncio.create_task(
            self._action(chat, request_id, task_id, action, revision, message, ready)
        )
        self._workers.add(worker)

        def done(task: asyncio.Task[None]) -> None:
            self._workers.discard(task)
            if not task.cancelled():
                task.exception()
            if ready.done() and not ready.cancelled():
                ready.exception()

        worker.add_done_callback(done)
        return await asyncio.shield(ready)

    async def _action(
        self,
        chat: DashboardConversation,
        request_id: str,
        task_id: str,
        action: str,
        revision: int,
        message: str,
        ready: asyncio.Future[str],
    ) -> None:
        try:
            with self.operations.register_provisional(
                user_id=chat.user_id, channel_id=chat.channel_id
            ):
                self.operations.bind_current_provisional(chat.key)
                # A coding cancellation can await a child finalizer that needs
                # the same root to publish. Its task ID has independent owner
                # authority, and cancellation cannot recreate deleted chat data.
                root_guard = nullcontext() if action == "cancel" else self.roots.hold(chat.key)
                async with self.privacy.activity(chat.user_id), root_guard:
                    await self._action_leased(
                        chat, request_id, task_id, action, revision, message, ready
                    )
        finally:
            if not ready.done():
                ready.set_exception(
                    web.HTTPServiceUnavailable(reason="This action expired. Reopen the dashboard")
                )

    async def _action_leased(
        self,
        chat: DashboardConversation,
        request_id: str,
        task_id: str,
        action: str,
        revision: int,
        message: str,
        ready: asyncio.Future[str],
    ) -> None:
        action_id: str | None = None
        fresh = False
        try:
            ctx = await self.context(chat)
            if await self.store.get(chat.id, user_id=chat.user_id, guild_id=chat.guild_id) is None:
                raise web.HTTPNotFound(reason="Conversation no longer exists")
            if action in {"approve", "reject", "test"}:
                if not await self.store.has_task(chat, task_id, revision):
                    raise web.HTTPNotFound(reason="Task proposal not found in this chat")
            else:
                task = await self.coding.store.get_task(task_id)
                if (
                    task is None
                    or task.root_key != chat.key
                    or task.user_id != chat.user_id
                    or task.delivery_surface != "dashboard"
                ):
                    raise web.HTTPNotFound(reason="Coding task not found in this chat")
                tool = "coding_task_message" if action == "steer" else "coding_task_cancel"
                ctx.activated_tools.add(tool)
                if self.scheduled.r.tools.registry.dispatch_gate(tool, ctx) is not None:
                    raise web.HTTPForbidden(
                        reason="Your current tool policy does not allow this task action"
                    )
            # Stopping existing work must remain possible when new-work capacity
            # is full. Access and task-control policy have already been checked.
            lease = None
            if action != "cancel":
                decision = await self.admission.try_acquire(chat.user_id)
                lease = decision.lease
                if lease is None:
                    raise web.HTTPTooManyRequests(
                        reason="Please wait for your current work to finish"
                    )
            async with lease if lease is not None else nullcontext():
                action_id, fresh = await self.store.accept_action(chat, request_id, task_id, action)
                ready.set_result(action_id)
                if not fresh:
                    return
                payload: dict[str, Any] = {"task_id": task_id, "revision": revision}
                if action == "test":
                    preview = await self.scheduled.executor.test_preview(ctx, task_id, revision)
                    payload.update(
                        {
                            "text": preview["detail"],
                            "outcome": preview["outcome"],
                            "posts": [
                                {
                                    "channel_id": str(p["channel_id"]),
                                    "content": str(p.get("content", "")),
                                }
                                for p in preview["posts"]
                            ],
                            "files": [],
                        }
                    )
                    for name, _, data in preview["files"][:10]:
                        payload["files"].append(
                            (await self.files.save(chat, name, data, kind="output")).public()
                        )
                elif action in {"approve", "reject"}:
                    payload["text"] = await self.scheduled.approvals.decide(
                        ctx, task_id, revision, approve=action == "approve"
                    )
                elif action == "steer":
                    if not message.strip():
                        raise ValueError("Add an answer or instruction")
                    await self.scheduled.authority.moderate(ctx, message, Direction.INPUT)
                    result = await self.coding.steer_task(ctx, task_id, message)
                    if not result or not result.get("accepted"):
                        raise ValueError("This task cannot accept input right now")
                    payload["text"] = "Your input was sent to the coding task."
                else:
                    await self.coding.cancel_task(task_id, reason="Stopped from the dashboard")
                    payload["text"] = "Stop requested. Partial file changes are kept."
                await self.store.finish_action(chat.id, action_id, "completed", payload)
        except BaseException as exc:
            if not ready.done():
                ready.set_exception(
                    exc
                    if isinstance(exc, web.HTTPException)
                    else web.HTTPBadRequest(reason="This task action is unavailable")
                )
            if action_id and fresh:
                notice = (
                    str(exc)
                    if isinstance(exc, ValueError)
                    else "The task action was interrupted. Check its current state before trying again."
                )
                await await_uncancellable(
                    self.store.finish_action(
                        chat.id,
                        action_id,
                        "failed",
                        {"text": notice[:500], "task_id": task_id, "revision": revision},
                    )
                )
            if not isinstance(
                exc, web.HTTPException | ValueError | asyncio.CancelledError | TimeoutError
            ):
                log.exception("Dashboard task action failed")
            if isinstance(exc, asyncio.CancelledError):
                raise

    async def close(self) -> None:
        self._closed = True
        workers = list(self._workers)
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
