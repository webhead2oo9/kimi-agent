"""Conversational task management and explicit unattended outcomes."""

from __future__ import annotations

import json
from collections.abc import Callable, Coroutine
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from skills.manager import MAX_CONTENT_SIZE, validate_skill_content
from tools._common import tool_error
from tools.registry import MessageContext, ToolRegistry
from tools.task_python import TaskPythonSpec
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
    execution: Literal["llm", "python_gate", "python_only"] = "llm"
    python: TaskPythonSpec | None = None

    @classmethod
    def from_stored(cls, value: dict[str, Any]) -> Self:
        """Read old definitions with their original UTC default; new submissions stay strict."""
        schedule = value.get("schedule")
        if isinstance(schedule, dict) and "timezone" not in schedule:
            value = {**value, "schedule": {**schedule, "timezone": "UTC"}}
        return cls.model_validate(value)

    @model_validator(mode="after")
    def validate_definition(self) -> Self:
        if (self.execution == "llm") != (self.python is None):
            raise ValueError("Python modes require a script; LLM mode must not include one")
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
Use a timezone the user explicitly supplied in the current setup conversation, or preserve an
unchanged timezone when editing. If it is missing or ambiguous (for example CST), ask which
timezone they intend before drafting; a server default is only a suggestion, never assumed consent.
Require an explicit IANA timezone in the submitted schedule. Do not ask again when it is clear.
Call validate_schedule with the schedule to verify the interpreted recurrence and next runs.
Use its native_time values verbatim whenever showing concrete run dates/times: Discord's full
date and relative pair display in each viewer's local timezone. Recurrence labels retain the
schedule's wall-clock time and timezone. Do not calculate Unix timestamps yourself.
For change monitoring ask whether the first check should be silent (default) or publish a baseline.
Write a dedicated skill: goal, sources, steps, state to retain, condition checks before actions,
output format, when to do nothing, and failure handling. Never invent tools or available access.
Use task_manage draft to save the complete task and show its confirmation preview. The user must
click Approve; do not claim activation before that. Use inspect before edits, preserve unrelated
requirements, and supply the revision you inspected. Skills are editable only through task edits.
The proposal also offers Test preview: the requester can read actual sources and see sample
posts privately, without publishing or changing task state. Its LLM cannot execute browser
actions, generate files, or use tools outside its restricted reading tools. Python
proposals can generate sample files in their isolated test workspace. /tasks reopens private
management controls. Edit opens a reply conversation already bound to the selected task.
Task skills and schedules are versioned together. A copied personal skill changes independently.
Do not attempt @everyone or @here notifications. They are never permitted.
For deterministic checks or output, choose execution=python_only, or python_gate when Python
may decide an LLM is needed. Keep execution=llm for ordinary agent procedures. setup reports
whether Python is available. Author python.code and named python.inputs together; inputs are
predefined Discord windows or public HTTPS URLs fetched by the application, not by the script.
Use the scheduled-tasks skill's Python contract and examples before writing a script. The script
reads /work/input.json and writes /work/result.json. It may write files under /work/outputs/.
Reuse installed sandbox packages; install missing packages only during setup via run_code when
available. Never install packages or access the network from a scheduled script. Test preview
can exercise the exact code and dependencies before approval. The task.py attachment is part of
the approved revision. Explain that changing code, inputs, or execution mode resets saved state.
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
                        "validate_schedule",
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
                "schedule": {
                    "type": "object",
                    "description": "validate_schedule only: schedule with explicit IANA timezone. Returns interpreted recurrence and native Discord run timestamps.",
                },
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
