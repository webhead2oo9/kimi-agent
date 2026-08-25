from __future__ import annotations

import json
from collections.abc import Callable
from typing import Protocol

from tools._common import get_string, tool_error
from tools.registry import MessageContext, ToolRegistry
from trust.tiers import TrustTier


class BlockedUserStoreProtocol(Protocol):
    async def block_user(self, user_id: str, *, blocked_by: str, reason: str = "") -> bool: ...


StoreProvider = BlockedUserStoreProtocol | Callable[[], BlockedUserStoreProtocol | None]


def init_block_user_tool(registry: ToolRegistry, store: StoreProvider) -> None:
    async def _block_user(args: dict, ctx: MessageContext) -> str:
        # Structural mirror of the /moderation block invariant: staff cannot be
        # blocked. Without this, injected untrusted content could steer the model
        # into silently blocking the staff speaker mid-conversation.
        if ctx.trust_tier >= TrustTier.STAFF:
            return tool_error("Staff users cannot be blocked.")
        active_store = _resolve_store(store)
        if active_store is None:
            return tool_error("Blocked-user store is not initialized.")
        try:
            reason = get_string(args, "reason", max_chars=500)
        except ValueError as exc:
            return tool_error(str(exc))

        user_id = ctx.user_id
        created = await active_store.block_user(user_id, blocked_by=ctx.user_id, reason=reason)
        return json.dumps(
            {
                "blocked": True,
                "created": created,
                "user_id": user_id,
                "reason": reason,
                "message": "Blocked the current user from using the bot.",
            }
        )

    if registry.has_tool("block_user"):
        return
    registry.register(
        name="block_user",
        description=(
            "Block the current user from using the bot when continuing the conversation "
            "with them would be unsafe or abusive. The target is always the current user "
            "from message context; this tool cannot block another Discord user by ID or name, "
            "and staff users cannot be blocked. A blocked user is ignored before reactions, "
            "transcript writes, tool calls, or model calls."
        ),
        parameters={
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Optional concise reason the current user should be blocked.",
                },
            },
            "additionalProperties": False,
        },
        handler=_block_user,
        min_tier=TrustTier.MEMBER,
        category="Moderation",
    )


def _resolve_store(store: StoreProvider) -> BlockedUserStoreProtocol | None:
    if callable(store):
        return store()
    return store
