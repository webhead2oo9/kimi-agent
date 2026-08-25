from __future__ import annotations

import json

from tools._common import tool_error
from tools.registry import MessageContext, ToolRegistry
from trust.tiers import TrustTier

MAX_PLAN_STEPS = 30
MAX_PLAN_STEP_CHARS = 200
ALLOWED_STATUSES = ("pending", "in_progress", "completed")


def init_plan_tool(registry: ToolRegistry) -> None:
    """Register the in-turn `plan` checklist tool.

    The plan is per-turn scratch state: the handler stashes it on
    MessageContext.plan (which dies when the turn returns and never reaches the
    SQLite transcript) and echoes it back so the model can re-read it across
    ReAct iterations. The live activity surface renders the current checklist
    as muted progress lines while the turn runs (agent/core.py emits updates;
    discord_adapter/io.py renders), and the compactor re-appends it verbatim to
    progress notes, but it is never persisted past the reply.
    """

    async def _plan(args: dict, ctx: MessageContext) -> str:
        steps = args.get("steps")
        if not isinstance(steps, list) or not steps:
            return tool_error("steps must be a non-empty list")
        if len(steps) > MAX_PLAN_STEPS:
            return tool_error(f"plan accepts at most {MAX_PLAN_STEPS} steps")
        parsed: list[dict[str, str]] = []
        for index, step in enumerate(steps, start=1):
            if isinstance(step, str):
                content = step.strip()
                status = "pending"
            elif isinstance(step, dict):
                content = str(step.get("content", "")).strip()
                status = str(step.get("status", "pending")).strip().lower() or "pending"
            else:
                return tool_error(f"step {index}: must be a string or object")
            if not content:
                return tool_error(f"step {index}: content must not be empty")
            if len(content) > MAX_PLAN_STEP_CHARS:
                content = content[:MAX_PLAN_STEP_CHARS] + "…"
            if status not in ALLOWED_STATUSES:
                return tool_error(
                    f"step {index}: status must be one of {', '.join(ALLOWED_STATUSES)}"
                )
            parsed.append({"content": content, "status": status})
        ctx.plan = parsed
        return json.dumps({"plan": parsed, "count": len(parsed)})

    if registry.has_tool("plan"):
        return
    registry.register(
        name="plan",
        description=(
            "Track an ordered checklist for the current reply. Use it on "
            "multi-step requests to lay out the steps up front, then call it "
            "again to update statuses as you finish each one. The user sees the "
            "checklist live as progress lines under your status while you work, "
            "so keep steps short and user-readable. It is not remembered after "
            "this reply."
        ),
        parameters={
            "type": "object",
            "properties": {
                "steps": {
                    "type": "array",
                    "description": (
                        "The full ordered checklist; replaces any previous plan set this turn."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {
                                "type": "string",
                                "description": "What this step does.",
                            },
                            "status": {
                                "type": "string",
                                "enum": list(ALLOWED_STATUSES),
                                "description": "Step status; defaults to pending.",
                            },
                        },
                        "required": ["content"],
                    },
                },
            },
            "required": ["steps"],
        },
        handler=_plan,
        min_tier=TrustTier.MEMBER,
    )
