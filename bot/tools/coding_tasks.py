from __future__ import annotations

import json
from typing import Protocol

from tools._common import tool_error
from tools.registry import MessageContext, ToolRegistry, TurnHandoff
from trust.tiers import TrustTier

MAX_OBJECTIVE_CHARS = 12_000
MAX_CONTEXT_CHARS = 12_000
MAX_STEERING_CHARS = 8_000
MAX_PROGRESS_CHARS = 500
MAX_PLAN_STEPS = 30
MAX_PLAN_STEP_CHARS = 200
PLAN_STATUSES = frozenset({"pending", "in_progress", "completed", "blocked"})
CODING_CONTROL_TOOLS = frozenset(
    {
        "start_coding_task",
        "coding_task_status",
        "coding_task_message",
        "coding_task_cancel",
        "coding_task_retry_delivery",
    }
)

CODING_WORKSPACE_TOOLS = frozenset(
    {
        "import_attachment",
        "edit_file",
        "multi_edit",
        "read_file",
        "write_file",
        "move_file",
        "queue_file",
        "list_workspace",
        "delete_file",
        "grep_workspace",
        "glob_workspace",
    }
)


class CodingTaskControls(Protocol):
    async def start_from_tool(
        self,
        ctx: MessageContext,
        *,
        objective: str,
        acceptance_criteria: list[str],
        context_text: str,
    ) -> dict[str, object]: ...

    async def status_from_tool(
        self, ctx: MessageContext, *, task_id: str | None
    ) -> dict[str, object] | None: ...

    async def steer_from_tool(
        self, ctx: MessageContext, *, task_id: str, message: str
    ) -> dict[str, object] | None: ...

    async def cancel_from_tool(
        self, ctx: MessageContext, *, task_id: str, reason: str
    ) -> dict[str, object] | None: ...

    async def retry_delivery_from_tool(
        self, ctx: MessageContext, *, task_id: str
    ) -> dict[str, object] | None: ...

    async def set_plan(self, task_id: str, steps: list[dict[str, str]]) -> None: ...

    async def set_progress(self, task_id: str, message: str) -> None: ...

    async def request_input(self, task_id: str, message: str) -> None: ...

    async def start_job(self, task_id: str, request: dict[str, object]) -> str: ...

    async def job_status(
        self, task_id: str, job_id: str, wait_seconds: float
    ) -> dict[str, object] | None: ...

    async def cancel_job(self, task_id: str, job_id: str) -> bool: ...


def init_coding_control_tools(registry: ToolRegistry, controls: CodingTaskControls) -> None:
    async def start(args: dict, ctx: MessageContext) -> str:
        if ctx.stop_event is not None and ctx.stop_event.is_set():
            return tool_error("The response was stopped before delegation began")
        objective = str(args.get("task", "")).strip()
        if not objective:
            return tool_error("task is required")
        if len(objective) > MAX_OBJECTIVE_CHARS:
            return tool_error(f"task exceeds {MAX_OBJECTIVE_CHARS} characters")
        raw_criteria = args.get("acceptance_criteria") or []
        if not isinstance(raw_criteria, list) or not all(
            isinstance(item, str) for item in raw_criteria
        ):
            return tool_error("acceptance_criteria must be a list of strings")
        context_text = str(args.get("context", "")).strip()
        if len(context_text) > MAX_CONTEXT_CHARS:
            return tool_error(f"context exceeds {MAX_CONTEXT_CHARS} characters")
        result = await controls.start_from_tool(
            ctx,
            objective=objective,
            acceptance_criteria=[item.strip() for item in raw_criteria if item.strip()],
            context_text=context_text,
        )
        if ctx.stop_event is not None and ctx.stop_event.is_set():
            task_id = str(result.get("task_id", ""))
            if task_id:
                await controls.cancel_from_tool(
                    ctx,
                    task_id=task_id,
                    reason="Foreground response was stopped during delegation",
                )
            return tool_error("The response was stopped; the delegated task was cancelled")
        task_id = str(result.get("task_id", "")).strip()
        if result.get("accepted") is True and task_id:
            ctx.terminal_handoff = TurnHandoff(
                response_text=(
                    f"Coding task `{task_id[:8]}` was queued. "
                    "Progress and the final result will appear here."
                ),
                reason="coding_task",
                task_id=task_id,
                allowed_followup_tools=frozenset({"move_to_thread"}),
            )
        return json.dumps(result)

    async def status(args: dict, ctx: MessageContext) -> str:
        task_id = str(args.get("task_id", "")).strip() or None
        result = await controls.status_from_tool(ctx, task_id=task_id)
        return json.dumps(result) if result is not None else tool_error("Coding task not found")

    async def message(args: dict, ctx: MessageContext) -> str:
        task_id = str(args.get("task_id", "")).strip()
        text = str(args.get("message", "")).strip()
        if not task_id or not text:
            return tool_error("task_id and message are required")
        if len(text) > MAX_STEERING_CHARS:
            return tool_error(f"message exceeds {MAX_STEERING_CHARS} characters")
        result = await controls.steer_from_tool(ctx, task_id=task_id, message=text)
        return json.dumps(result) if result is not None else tool_error("Coding task not found")

    async def cancel(args: dict, ctx: MessageContext) -> str:
        task_id = str(args.get("task_id", "")).strip()
        if not task_id:
            return tool_error("task_id is required")
        result = await controls.cancel_from_tool(
            ctx,
            task_id=task_id,
            reason=str(args.get("reason", "")).strip(),
        )
        return json.dumps(result) if result is not None else tool_error("Coding task not found")

    async def retry_delivery(args: dict, ctx: MessageContext) -> str:
        task_id = str(args.get("task_id", "")).strip()
        if not task_id:
            return tool_error("task_id is required")
        result = await controls.retry_delivery_from_tool(ctx, task_id=task_id)
        return json.dumps(result) if result is not None else tool_error("Coding task not found")

    registry.register(
        name="start_coding_task",
        description=(
            "Delegate repository-scale, multi-file, or investigate-edit-verify work to "
            "the durable coding agent. A successful delegation ends this foreground turn "
            "automatically; progress and the final result are delivered separately."
        ),
        parameters={
            "type": "object",
            "properties": {
                "task": {"type": "string"},
                "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
                "context": {"type": "string"},
            },
            "required": ["task"],
        },
        handler=start,
        min_tier=TrustTier.MEMBER,
    )
    registry.register(
        name="coding_task_status",
        description="Inspect a durable coding task without waiting for it to finish.",
        parameters={
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
        },
        handler=status,
        min_tier=TrustTier.MEMBER,
    )
    registry.register(
        name="coding_task_message",
        description="Send steering or requested input to an active coding task.",
        parameters={
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "message": {"type": "string"},
            },
            "required": ["task_id", "message"],
        },
        handler=message,
        min_tier=TrustTier.MEMBER,
    )
    registry.register(
        name="coding_task_cancel",
        description=(
            "Cancel a queued or running coding task. Running sandbox jobs are stopped "
            "before cancellation is confirmed; partial workspace changes are preserved."
        ),
        parameters={
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["task_id"],
        },
        handler=cancel,
        min_tier=TrustTier.MEMBER,
    )
    registry.register(
        name="coding_task_retry_delivery",
        description=(
            "Retry the final Discord report for a terminal coding task whose automatic "
            "delivery attempts were exhausted."
        ),
        parameters={
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
        handler=retry_delivery,
        min_tier=TrustTier.MEMBER,
    )


def build_coding_registry(source: ToolRegistry, controls: CodingTaskControls) -> ToolRegistry:
    registry = source.clone_only(set(CODING_WORKSPACE_TOOLS))

    def task_id(ctx: MessageContext) -> str:
        prefix = "coding:"
        return ctx.context_key.removeprefix(prefix) if ctx.context_key.startswith(prefix) else ""

    async def plan(args: dict, ctx: MessageContext) -> str:
        steps = args.get("steps")
        if not isinstance(steps, list) or not steps:
            return tool_error("steps must be a non-empty list")
        if len(steps) > MAX_PLAN_STEPS:
            return tool_error(f"plan accepts at most {MAX_PLAN_STEPS} steps")
        parsed: list[dict[str, str]] = []
        for index, step in enumerate(steps, start=1):
            if not isinstance(step, dict):
                return tool_error(f"step {index} must be an object")
            content = str(step.get("content", "")).strip()
            status = str(step.get("status", "pending")).strip().lower()
            if not content:
                return tool_error(f"step {index} content is required")
            if status not in PLAN_STATUSES:
                return tool_error(f"step {index} has an invalid status")
            parsed.append({"content": content[:MAX_PLAN_STEP_CHARS], "status": status})
        current_task_id = task_id(ctx)
        if not current_task_id:
            return tool_error("coding task context is unavailable")
        ctx.plan = parsed
        await controls.set_plan(current_task_id, parsed)
        return json.dumps({"plan": parsed})

    async def progress(args: dict, ctx: MessageContext) -> str:
        text = str(args.get("message", "")).strip()
        if not text:
            return tool_error("message is required")
        current_task_id = task_id(ctx)
        if not current_task_id:
            return tool_error("coding task context is unavailable")
        text = text[:MAX_PROGRESS_CHARS]
        await controls.set_progress(current_task_id, text)
        return json.dumps({"milestone": text})

    async def request_input(args: dict, ctx: MessageContext) -> str:
        text = str(args.get("message", "")).strip()
        if not text:
            return tool_error("message is required")
        current_task_id = task_id(ctx)
        if not current_task_id:
            return tool_error("coding task context is unavailable")
        text = text[:MAX_PROGRESS_CHARS]
        await controls.request_input(current_task_id, text)
        return json.dumps({"waiting_for_input": True, "message": text})

    async def start_job(args: dict, ctx: MessageContext) -> str:
        current_task_id = task_id(ctx)
        if not current_task_id:
            return tool_error("coding task context is unavailable")
        job_id = await controls.start_job(current_task_id, dict(args))
        return json.dumps({"job_id": job_id, "status": "queued"})

    async def job_status(args: dict, ctx: MessageContext) -> str:
        current_task_id = task_id(ctx)
        job_id = str(args.get("job_id", "")).strip()
        if not current_task_id or not job_id:
            return tool_error("job_id is required")
        try:
            # Omitted means one event-driven wait for the job's configured
            # lifetime. The manager clamps this to the operator's actual job
            # maximum, while an explicit zero remains a non-blocking status read.
            wait_seconds = float(args.get("wait_seconds", 7_200))
        except TypeError, ValueError:
            return tool_error("wait_seconds must be a number")
        result = await controls.job_status(current_task_id, job_id, wait_seconds)
        return json.dumps(result) if result is not None else tool_error("Coding job not found")

    async def cancel_job(args: dict, ctx: MessageContext) -> str:
        current_task_id = task_id(ctx)
        job_id = str(args.get("job_id", "")).strip()
        if not current_task_id or not job_id:
            return tool_error("job_id is required")
        cancelled = await controls.cancel_job(current_task_id, job_id)
        return json.dumps({"job_id": job_id, "cancelled": cancelled})

    registry.register(
        name="coding_plan",
        description=(
            "Set the complete durable coding checklist. After initial read-only discovery, "
            "call this before edits or jobs and update it as work advances."
        ),
        parameters={
            "type": "object",
            "properties": {
                "steps": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string"},
                            "status": {"type": "string", "enum": sorted(PLAN_STATUSES)},
                        },
                        "required": ["content"],
                    },
                }
            },
            "required": ["steps"],
        },
        handler=plan,
        min_tier=TrustTier.MEMBER,
    )
    registry.register(
        name="coding_progress",
        description="Publish one meaningful, user-readable milestone to the task status.",
        parameters={
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
        },
        handler=progress,
        min_tier=TrustTier.MEMBER,
    )
    registry.register(
        name="coding_request_input",
        description=(
            "Pause the durable task when user input is genuinely required. State one "
            "specific question, then finish the current coding report."
        ),
        parameters={
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
        },
        handler=request_input,
        min_tier=TrustTier.MEMBER,
    )
    registry.register(
        name="coding_job_start",
        description=(
            "Start a managed sandbox job from a workspace file and return immediately. "
            "Write a shell script first for compound commands."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "mode": {"type": "string", "enum": ["python", "shell", "direct"]},
                "argv": {"type": "array", "items": {"type": "string"}},
                "stdin": {"type": "string"},
            },
            "required": ["path"],
        },
        handler=start_job,
        min_tier=TrustTier.MEMBER,
    )
    registry.register(
        name="coding_job_status",
        description=(
            "Wait for a managed job to finish without repeated model polling, then return "
            "its terminal output. Pass wait_seconds=0 only for a non-blocking status read."
        ),
        parameters={
            "type": "object",
            "properties": {
                "job_id": {"type": "string"},
                "wait_seconds": {"type": "number", "minimum": 0},
            },
            "required": ["job_id"],
        },
        handler=job_status,
        min_tier=TrustTier.MEMBER,
    )
    registry.register(
        name="coding_job_cancel",
        description="Stop a managed coding job and confirm sandbox teardown.",
        parameters={
            "type": "object",
            "properties": {"job_id": {"type": "string"}},
            "required": ["job_id"],
        },
        handler=cancel_job,
        min_tier=TrustTier.MEMBER,
    )
    return registry
