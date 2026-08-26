from __future__ import annotations

import json
from dataclasses import dataclass, field

from agent.backfill import BackfilledMessage
from app.tool_surfaces import surface_tools
from discord_adapter.gateway import MemberLookup, TurnSourceSnapshot
from tools.registry import MessageContext, ToolEntry, ToolRegistry

# Tools that perform an external WRITE to a shared production surface. We replace
# them with canned acks so an eval run cannot write into the shared community
# memory bank or accumulate per-user memories in the live Hindsight backend
# (repeated harness runs would otherwise recall their own prior runs, making
# repetitions non-comparable). Read-only tools stay fully live. Plugins add their
# own writing tools to this surface via app/tool_surfaces.py.
SAFE_STUB_TOOLS = ("teach", "remember_user_memory")


class StubBlockedUserStore:
    """In-memory BlockedUserStore so `block_user` is registered (and callable) in evals.

    Production registers block_user against the real DB-backed store; here attempts
    land in a plain dict, so safety scenarios can present (and grade misuse of) the
    tool without ever blocking a real user.
    """

    def __init__(self) -> None:
        self.blocked: dict[str, str] = {}

    async def block_user(self, user_id: str, *, blocked_by: str, reason: str = "") -> bool:
        created = user_id not in self.blocked
        self.blocked[user_id] = reason
        return created


class StubGateway:
    """Headless stand-in for DiscordGateway: read-only fixtures, no live Discord.

    Implements only the surface the Discord-bound tools call:
    read_turn_source, collect_recent_channel_context, resolve_member.
    """

    def __init__(self) -> None:
        self._trigger_content = ""
        self._trigger_author_id = "0"
        self._member: MemberLookup = MemberLookup(match="none")

    def set_fixture(
        self,
        *,
        trigger_content: str = "",
        trigger_author_id: str = "0",
        member: MemberLookup | None = None,
    ) -> None:
        """Trigger fields reset each call; the member fixture is sticky until re-supplied."""
        self._trigger_content = trigger_content
        self._trigger_author_id = trigger_author_id
        if member is not None:
            self._member = member

    def read_turn_source(self, ctx: MessageContext) -> TurnSourceSnapshot | None:
        return TurnSourceSnapshot(
            content=self._trigger_content,
            author_id=self._trigger_author_id,
            is_bot=False,
        )

    async def collect_recent_channel_context(
        self, ctx: MessageContext, *, limit: int = 15
    ) -> list[BackfilledMessage]:
        # No live channel; scenarios that need history can extend this later.
        return []

    async def resolve_member(
        self, ctx: MessageContext, *, user_id: str | None = None, query: str | None = None
    ) -> MemberLookup:
        return self._member


def install_safe_stubs(registry: ToolRegistry) -> None:
    async def _ack(args: dict, ctx: MessageContext) -> str:
        return json.dumps({"status": "stubbed", "note": "external write suppressed in eval"})

    by_name: dict[str, ToolEntry] = {entry.name: entry for entry in registry.get_all_tools()}
    for name in (*SAFE_STUB_TOOLS, *sorted(surface_tools("eval_stub"))):
        entry = by_name.get(name)
        if entry is None:
            continue
        registry.remove_tools({name})
        registry.register(
            name=entry.name,
            description=entry.description,
            parameters=entry.parameters,
            handler=_ack,
            min_tier=entry.min_tier,
            searchable=entry.searchable,
            skill_name=entry.skill_name,
            category=entry.category,
            parameters_builder=entry.parameters_builder,
        )


@dataclass
class _StubCodingUserState:
    started: list[str] = field(default_factory=list)
    cancelled: list[str] = field(default_factory=list)
    steered: list[tuple[str, str]] = field(default_factory=list)
    delivery_retries: list[str] = field(default_factory=list)


class StubCodingControls:
    """In-memory CodingTaskControls so the chat-side coding tools exist in evals.

    Production registers these against the real scheduler in app/coding_tasks.py;
    here every call lands in this object. Mutable chat-control state is partitioned
    by the eval's synthetic user id, so arms and repetitions sharing one registry
    still receive independent task sequences. No job is ever spawned.
    """

    def __init__(self) -> None:
        self._users: dict[str, _StubCodingUserState] = {}
        # Coding-worker controls are not registered in evals; retain their no-op
        # storage only to satisfy the production control interface used by the cast.
        self.plans: dict[str, list[dict[str, str]]] = {}
        self.progress: dict[str, str] = {}

    def _state(self, ctx: MessageContext) -> _StubCodingUserState:
        return self._users.setdefault(ctx.user_id, _StubCodingUserState())

    async def start_from_tool(
        self,
        ctx: MessageContext,
        *,
        objective: str,
        acceptance_criteria: list[str],
        context_text: str,
    ) -> dict[str, object]:
        state = self._state(ctx)
        task_id = f"eval-task-{len(state.started) + 1}"
        state.started.append(objective)
        return {
            "accepted": True,
            "task_id": task_id,
            "status": "queued",
            "objective": objective,
        }

    async def status_from_tool(
        self, ctx: MessageContext, *, task_id: str | None
    ) -> dict[str, object]:
        return {"task_id": task_id or "eval-task-1", "status": "running", "plan": []}

    async def steer_from_tool(
        self, ctx: MessageContext, *, task_id: str, message: str
    ) -> dict[str, object]:
        self._state(ctx).steered.append((task_id, message))
        return {"task_id": task_id, "delivered": True}

    async def cancel_from_tool(
        self, ctx: MessageContext, *, task_id: str, reason: str
    ) -> dict[str, object]:
        self._state(ctx).cancelled.append(task_id)
        return {"task_id": task_id, "cancelled": True, "reason": reason}

    async def retry_delivery_from_tool(
        self, ctx: MessageContext, *, task_id: str
    ) -> dict[str, object]:
        self._state(ctx).delivery_retries.append(task_id)
        return {"task_id": task_id, "delivery_retry_requested": True}

    async def set_plan(self, task_id: str, steps: list[dict[str, str]]) -> None:
        self.plans[task_id] = steps

    async def set_progress(self, task_id: str, message: str) -> None:
        self.progress[task_id] = message
