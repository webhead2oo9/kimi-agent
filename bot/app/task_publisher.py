"""Publication capture, durable delivery, and public conversation mapping."""

from __future__ import annotations
from storage.task_types import TaskRecord, DeliveryRecord
import asyncio
import json
import io
import logging
from typing import Any
import discord
from app.task_output import snapshot_output
from discord_adapter.io import chunk_message, build_embed
from moderation.types import Direction
from storage.conversations import ChannelMessageRecord
from tools.registry import MessageContext
from tools.workspace.common import workspace_activity
from tools.embeds import EmbedSpec
from tools.scheduled_tasks import TaskDefinition
from app.task_runtime import ScheduledTaskRuntime, ActiveRuns

from app.task_authority import TaskAuthority

log = logging.getLogger(__name__)


class TaskPublisher:
    def __init__(
        self, runtime: ScheduledTaskRuntime, authority: TaskAuthority, runs: ActiveRuns
    ) -> None:
        self.r, self.authority, self.runs = runtime, authority, runs

    async def post(self, args: dict[str, Any], ctx: MessageContext) -> Any:
        original_ctx = ctx
        ctx = await self.authority.fresh(ctx)
        channel_id = str(args.get("channel_id", ""))
        content = args.get("content", "")
        if not isinstance(content, str) or not content.strip() or len(content) > 60000:
            raise ValueError("content must contain 1–60000 characters")
        users, roles = (args.get("mention_users", []), args.get("mention_roles", []))
        if (
            not isinstance(users, list)
            or not isinstance(roles, list)
            or any(not isinstance(item, str) for item in [*users, *roles])
        ):
            raise ValueError("Recipients must be lists of string IDs")
        channel = await self.r.access.channel(ctx, channel_id, posting=True)
        mentions = await self.r.access.mentions(ctx, channel, users, roles)
        payload = {
            "channel_id": channel_id,
            "content": content,
            "mention_users": users,
            "mention_roles": roles,
        }
        if ctx.scheduled_run_id:
            run = self.runs.get(ctx.scheduled_run_id)
            if run is None:
                raise ValueError("Scheduled run is no longer active")
            definition = run.definition
            if (
                channel_id not in definition.destinations
                or not set(users).issubset(definition.mention_users)
                or (not set(roles).issubset(definition.mention_roles))
            ):
                raise ValueError("This destination or recipient was not approved for the task")
            queued = self.runs[ctx.scheduled_run_id].posts
            if len(queued) >= 10:
                raise ValueError("At most ten posts can be queued per run")
            queued.append(payload)
            return {"status": "queued"}
        await self.authority.moderate(ctx, content, Direction.OUTPUT)
        output_files: list[tuple[str, str | None, bytes]] = []
        output_embed: dict[str, Any] | None = None
        if args.get("include_output") is True:
            async with workspace_activity(self.r.tools.workspace_locks, ctx):
                await self.authority.moderate(
                    ctx,
                    content,
                    Direction.OUTPUT,
                    embed=ctx.outbox.embed,
                    embed_attachment=ctx.outbox.embed_attachment,
                )
                output_files, output_embed = await asyncio.to_thread(
                    snapshot_output, channel, ctx.outbox, []
                )
        links: list[str] = []
        for index, chunk in enumerate(chunk_message(self.notify_content(content, users, roles))):
            files = (
                [
                    discord.File(io.BytesIO(data), filename=name, description=description)
                    for name, description, data in output_files
                ]
                if index == 0
                else []
            )
            try:
                embed = (
                    build_embed(EmbedSpec(**output_embed)) if output_embed and index == 0 else None
                )
                message = await channel.send(
                    chunk, allowed_mentions=mentions, files=files, embed=embed
                )
            finally:
                for file in files:
                    file.close()
            await self.record_message(ctx, message)
            links.append(message.jump_url)
        if args.get("include_output") is True:
            original_ctx.update_outbox(
                output_files=(),
                output_file_descriptions=(),
                allowed_file_roots=(),
                embed=None,
                embed_attachment=None,
            )
        return {"status": "sent", "messages": links}

    async def record_message(self, ctx: MessageContext, message: discord.Message) -> None:
        key = f"scheduled-publication:{message.channel.id}:{message.id}"
        conversation_id = await self.r.conversations.get_or_create(
            key,
            getattr(message.channel, "name", ""),
            guild_id=ctx.guild_id,
            channel_id=str(message.channel.id),
            root_discord_message_id=str(message.id),
            owner_user_id=ctx.user_id,
        )
        await self.r.conversations.save_channel_messages(
            conversation_id,
            [
                ChannelMessageRecord(
                    discord_message_id=str(message.id),
                    role="assistant",
                    author_id=None,
                    author_name=None,
                    content=message.content,
                    source_created_at=message.created_at.timestamp(),
                )
            ],
            context_channel_id=str(message.channel.id),
        )

    async def finish(
        self,
        task: TaskRecord,
        run_id: str,
        outcome: str,
        detail: str,
        state: dict[str, Any],
        posts: list[dict[str, Any]],
        *,
        recover_reads: bool = True,
    ) -> None:
        definition = TaskDefinition.from_stored(task["definition"])
        deliveries: list[dict[str, Any]] = []
        for post in posts:
            for index, chunk in enumerate(
                chunk_message(
                    self.notify_content(
                        post["content"],
                        post.get("mention_users", []),
                        post.get("mention_roles", []),
                    )
                )
            ):
                deliveries.append(
                    {
                        **post,
                        "content": chunk,
                        "file_ids": post.get("file_ids", []) if index == 0 else [],
                        "embed": post.get("embed") if index == 0 else None,
                    }
                )
        if definition.log_channel:
            deliveries.append(
                {
                    "channel_id": definition.log_channel,
                    "is_log": True,
                    "content": f"Task {definition.name}: {outcome}. {detail}"[:1800],
                }
            )
        if outcome in {"needs_input", "failed"}:
            deliveries.append(
                {
                    "channel_id": task["channel_id"],
                    "is_log": True,
                    "management": True,
                    "content": f"Task {definition.name} needs attention: {detail}\n"
                    f"Task ID: {task['id']}. Inspect or edit it, then resume.",
                }
            )
        await self.r.store.finish(
            run_id,
            "delivery" if posts else outcome,
            detail,
            state,
            deliveries,
            recover_reads=recover_reads,
        )

    @staticmethod
    def notify_content(content: str, users: list[str], roles: list[str]) -> str:
        recipients = [*(f"<@{user}>" for user in users), *(f"<@&{role}>" for role in roles)]
        prefix = " ".join(token for token in recipients if token not in content)
        return f"{prefix}\n{content}" if prefix else content

    async def deliver_pending(self) -> None:
        workers: dict[str, asyncio.Task[None]] = {}
        attempted: set[str] = set()
        limit = self.r.settings.scheduled_task_delivery_max_concurrency
        try:
            while True:
                for run_id in await self.r.store.delivery_runs(limit - len(workers), attempted):
                    attempted.add(run_id)
                    workers[run_id] = asyncio.create_task(self._deliver_run(run_id))
                if not workers:
                    return
                done, _ = await asyncio.wait(workers.values(), return_when=asyncio.FIRST_COMPLETED)
                for result in await asyncio.gather(*done, return_exceptions=True):
                    if isinstance(result, Exception):
                        log.error("Task publication worker failed", exc_info=result)
                workers = {
                    run_id: worker for run_id, worker in workers.items() if worker not in done
                }
        finally:
            for worker in workers.values():
                worker.cancel()
            await asyncio.gather(*workers.values(), return_exceptions=True)

    async def _deliver_run(self, run_id: str) -> None:
        while pending := await self.r.store.deliveries(run_id=run_id):
            if not await self._deliver(pending[0]):
                return

    async def _deliver(self, delivery: DeliveryRecord) -> bool:
        try:
            task = await self.r.store.get(delivery["task_id"], active=True)
        except ValueError:
            return False
        payload = json.loads(delivery["payload_json"])
        sending = False
        if delivery["is_log"] and not payload.get("management"):
            payload["content"] = (
                f"Task {task['id']} (revision {delivery['revision']}): "
                f"{delivery['run_status']}. {delivery['run_detail']}"
            )[:1800]
        try:
            async with self.r.privacy.activity(task["owner_id"]):
                ctx = await self.r.access.context(
                    task["guild_id"], task["owner_id"], task["channel_id"]
                )
                ctx = await self.authority.fresh(ctx)
                await self.r.access.owner_allowed(ctx)
                channel = await self.r.access.channel(
                    ctx, delivery["channel_id"], posting=not payload.get("management")
                )
                mentions = await self.r.access.mentions(
                    ctx,
                    channel,
                    payload.get("mention_users", []),
                    payload.get("mention_roles", []),
                )
                await self.authority.moderate(ctx, payload["content"], Direction.OUTPUT)
                files: list[discord.File] = []
                try:
                    for file_id in payload.get("file_ids", []):
                        row = await self.r.store.output_file(delivery["run_id"], file_id)
                        if len(row["data"]) > channel.guild.filesize_limit:
                            raise ValueError(
                                "Saved attachment exceeds the destination upload limit"
                            )
                        files.append(
                            discord.File(
                                io.BytesIO(row["data"]),
                                filename=row["filename"],
                                description=row["description"],
                            )
                        )
                    embed = (
                        build_embed(EmbedSpec(**payload["embed"])) if payload.get("embed") else None
                    )
                    if not await self.r.store.begin_delivery(delivery["id"], self.authority.token):
                        return False
                    sending = True
                    message = await channel.send(
                        payload["content"], allowed_mentions=mentions, files=files, embed=embed
                    )
                finally:
                    for file in files:
                        file.close()
                await self.r.store.delivery_status(
                    delivery["id"],
                    "sent",
                    message_id=str(message.id),
                    published_embed=dict(embed.to_dict()) if embed is not None else None,
                )
                if not delivery["is_log"]:
                    try:
                        await self.record_message(ctx, message)
                    except Exception:
                        log.exception("Sent task message could not be mapped to a conversation")
            return True
        except discord.HTTPException as exc:
            # A returned rejection is retryable; transport ambiguity is handled separately.
            status = "pending" if exc.status == 429 and delivery["attempts"] < 10 else "failed"
            if sending and exc.status >= 500:
                status = "uncertain"
            await self.r.store.delivery_status(delivery["id"], status, error=str(exc))
            if status in {"failed", "uncertain"} and not delivery["is_log"]:
                await self.r.store.attention(task["id"], delivery["run_id"])
        except Exception as exc:
            log.warning("Task delivery failed", exc_info=True)
            await self.r.store.delivery_status(
                delivery["id"], "uncertain" if sending else "failed", error=str(exc)
            )
            if not delivery["is_log"]:
                await self.r.store.attention(task["id"], delivery["run_id"])
        return False
