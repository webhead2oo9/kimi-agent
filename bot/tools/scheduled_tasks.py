"""Conversational task management and explicit unattended outcomes."""

from __future__ import annotations

import json
from collections.abc import Callable, Coroutine
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from skills.manager import MAX_CONTENT_SIZE, validate_skill_content
from tools._common import tool_error
from tools.registry import MessageContext, ToolRegistry
from utils.schedules import Schedule


class TaskDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    objective: str = Field(min_length=1, max_length=4000)
    sources: list[str] = Field(default_factory=list, max_length=50)
    skill: str = Field(min_length=1, max_length=MAX_CONTENT_SIZE)
    schedule: Schedule
    destinations: list[str] = Field(min_length=1, max_length=10)
    mention_users: list[str] = Field(default_factory=list, max_length=20)
    mention_roles: list[str] = Field(default_factory=list, max_length=20)
    condition: str = Field(default="", max_length=4000)
    first_check: Literal["silent", "publish"] = "silent"
    log_channel: str | None = None
    reset_state: bool = False

    @model_validator(mode="after")
    def validate_definition(self) -> Self:
        for value in [*self.destinations, *self.mention_users, *self.mention_roles]:
            if not value.isdigit() or int(value) <= 0:
                raise ValueError("Channels and recipients must be numeric Discord IDs")
        if self.log_channel is not None and not self.log_channel.isdigit():
            raise ValueError("log_channel must be a Discord channel ID")
        error = validate_skill_content(self.skill)
        if error or not self.skill.strip():
            raise ValueError(error or "Skill instructions cannot be empty")
        return self


WIZARD = """Task setup is a guided conversation. Ask only for missing details, grouped sensibly.
Capture objective, sources, procedure, schedule with timezone, destinations, optional condition,
explicit user/role recipients, optional log channel, and catch-up or skip behavior after downtime.
Keep setup conversation where the user is speaking. Approval is published separately, by default
in a quiet thread when outside a thread. If the user asks to keep approval here, pass
approval_in_channel=true to setup. After draft, say only that approval is pending; the host
publishes the full preview and button separately. Never repeat the skill or settings.
Explicitly confirm the scheduling timezone; use the server default unless the user chooses another
valid IANA timezone. Native Discord run times display in each viewer's local timezone.
For change monitoring ask whether the first check should be silent (default) or publish a baseline.
Write a dedicated skill: goal, sources, steps, state to retain, condition checks before actions,
output format, when to do nothing, and failure handling. Never invent tools or available access.
Use task_manage draft to save the complete task and show its confirmation preview. The user must
click Approve; do not claim activation before that. Use inspect before edits, preserve unrelated
requirements, and supply the revision you inspected. Skills are editable only through task edits.
Task skills and schedules are versioned together. A copied personal skill changes independently.
Do not attempt @everyone or @here notifications. They are never permitted.
"""


def init_task_tools(
    registry: ToolRegistry,
    manage: Callable[[dict[str, Any], MessageContext], Coroutine[Any, Any, str]],
    post: Callable[[dict[str, Any], MessageContext], Coroutine[Any, Any, str]],
    discover: Callable[[dict[str, Any], MessageContext], Coroutine[Any, Any, str]],
) -> None:
    registry.register(
        name="task_manage",
        description=(
            "Set up or manage scheduled tasks. Call setup FIRST for scheduling requests to load "
            "the required conversational wizard. Drafts automatically include a dedicated task "
            "skill and require the user's confirmation. Never activate tasks yourself."
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "setup",
                        "cancel_setup",
                        "draft",
                        "list",
                        "inspect",
                        "pause",
                        "resume",
                        "delete",
                        "run_now",
                        "retry_delivery",
                        "history",
                        "export_skill",
                    ],
                },
                "approval_in_channel": {
                    "type": "boolean",
                    "description": "setup only: true when the user explicitly wants the approval in the current channel.",
                },
                "task_id": {"type": "string"},
                "expected_revision": {"type": "integer"},
                "answer": {
                    "type": "string",
                    "description": "Answer the paused task's question when resuming or running now.",
                },
                "definition": {
                    "type": "object",
                    "description": (
                        "Complete task definition. setup returns the schema. For an edit supply "
                        "task_id and expected_revision from inspect."
                    ),
                },
                "personal_skill_name": {"type": "string"},
            },
            "required": ["action"],
        },
        handler=manage,
        untrusted=False,
    )

    async def complete(args: dict, ctx: MessageContext) -> str:
        if not ctx.scheduled_run_id or ctx.scheduled_result is None:
            return tool_error("This tool is only available inside a scheduled run")
        if ctx.scheduled_result:
            return tool_error("This run already has an outcome")
        outcome = args.get("outcome")
        if outcome not in {"completed", "no_change", "needs_input"}:
            return tool_error("Choose completed, no_change, or needs_input")
        state = args.get("state", {})
        if not isinstance(state, dict) or len(json.dumps(state)) > 64_000:
            return tool_error("state must be an object of at most 64000 characters")
        detail = args.get("detail", "")
        content = args.get("content", "")
        if not isinstance(detail, str) or not isinstance(content, str):
            return tool_error("detail and content must be text")
        if len(detail) > 4000 or len(content) > 60_000:
            return tool_error("detail or content is too long")
        if outcome == "needs_input" and not detail.strip():
            return tool_error("Specify the question in detail")
        ctx.scheduled_result.update(outcome=outcome, state=state, detail=detail, content=content)
        return json.dumps(
            {"outcome": outcome, "instruction": "Finish this run without more tools."}
        )

    registry.register(
        name="task_complete",
        description="Finish a scheduled run explicitly. no_change sends no destination message. "
        "Use needs_input to ask a question and pause. state replaces the saved task notes. "
        "For completed, content is the final post to the task's default destinations; omit "
        "content when posts were already queued with discord_post.",
        parameters={
            "type": "object",
            "properties": {
                "outcome": {"type": "string", "enum": ["completed", "no_change", "needs_input"]},
                "state": {"type": "object"},
                "detail": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["outcome", "state", "detail"],
        },
        handler=complete,
        searchable=True,
        category="Scheduled tasks",
    )
    registry.register(
        name="discord_channels",
        description="List accessible Discord source channels and configured "
        "posting destinations in this server. Resolve names before using channel IDs. "
        "Follow next_cursor to find sources on later pages; a final page may be empty. "
        "Restart without a cursor if "
        "the channel inventory changes. sources_error means discovery was incomplete.",
        parameters={
            "type": "object",
            "properties": {
                "cursor": {
                    "type": "string",
                    "description": "next_cursor from the previous page; omit or use empty for page one",
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 200},
            },
        },
        handler=discover,
        searchable=True,
        category="Discord",
        untrusted=True,
    )
    registry.register(
        name="discord_post",
        description="Post in an explicitly requested, configured Discord "
        "destination. Scheduled runs queue the post until successful completion. Never pings "
        "everyone or here. User and role notifications must be explicitly requested.",
        parameters={
            "type": "object",
            "properties": {
                "channel_id": {"type": "string"},
                "content": {"type": "string"},
                "include_output": {
                    "type": "boolean",
                    "description": "Send pending generated files/embed with this post; scheduled outputs attach at completion.",
                },
                "mention_users": {"type": "array", "items": {"type": "string"}},
                "mention_roles": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["channel_id", "content"],
        },
        handler=post,
        searchable=True,
        category="Discord",
    )
