from __future__ import annotations

import json
from difflib import get_close_matches

from tools._common import tool_error
from tools.registry import MessageContext, ToolRegistry
from trust.tiers import TrustTier


def init_browse_tools(registry: ToolRegistry) -> None:
    async def _browse_tools(args: dict, ctx: MessageContext) -> str:
        raw_load = args.get("load")
        if not raw_load:
            return json.dumps(_catalog_payload(registry, ctx))
        if not isinstance(raw_load, list):
            return tool_error("load must be an array of exact tool names.")

        visible_entries = {
            entry.name: entry
            for entry in registry.catalog(
                ctx.trust_tier, ctx.user_id, ctx.guild_id, ctx.blocked_tools
            )
        }
        visible_names = sorted(visible_entries)
        loaded: list[str] = []
        unknown: list[str] = []
        seen: set[str] = set()

        for raw_name in raw_load:
            name = str(raw_name)
            if name in seen:
                continue
            seen.add(name)
            if name in visible_entries:
                ctx.activated_tools.add(name)
                # Recorded separately so a load of a currently channel-pinned
                # tool (already in activated_tools via the pin merge) still gets
                # persisted to conversation_activated_tools.
                ctx.explicitly_loaded_tools.add(name)
                loaded.append(name)
            else:
                unknown.append(name)

        did_you_mean = {}
        for name in unknown:
            matches = get_close_matches(name, visible_names, n=1)
            if matches:
                did_you_mean[name] = matches[0]

        return json.dumps(
            {
                "loaded": loaded,
                "unknown": unknown,
                "did_you_mean": did_you_mean,
                "note": (
                    "Loaded tools are now available for the rest of this conversation. "
                    "Call browse_tools with no arguments to see the full catalog."
                ),
            }
        )

    registry.register(
        name="browse_tools",
        description=(
            "Discover and enable hidden tools. Many specialized tools stay hidden to "
            "keep the prompt small, so if a request might need a capability you don't "
            "see in your tool list, or you're unsure one exists, call this with no "
            "arguments first to scan the catalog (cheap: names and one-line "
            'descriptions only). Then call with load:["tool_name", ...] to enable the '
            "exact tools you need for the rest of this conversation."
        ),
        parameters={
            "type": "object",
            "properties": {
                "load": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Exact names of hidden tools to enable for the rest of this conversation."
                    ),
                },
            },
        },
        handler=_browse_tools,
        min_tier=TrustTier.MEMBER,
    )


def _catalog_payload(registry: ToolRegistry, ctx: MessageContext) -> dict:
    categories: dict[str, list[dict[str, str]]] = {}
    for entry in registry.catalog(ctx.trust_tier, ctx.user_id, ctx.guild_id, ctx.blocked_tools):
        category = entry.category.strip() or "Other"
        categories.setdefault(category, []).append(
            {"name": entry.name, "description": entry.description}
        )

    sorted_categories = {
        category: sorted(entries, key=lambda item: item["name"])
        for category, entries in sorted(categories.items())
    }
    count = sum(len(entries) for entries in sorted_categories.values())
    return {
        "note": ('Call browse_tools with load:["name", ...] to enable tools, then call them.'),
        "count": count,
        "categories": sorted_categories,
    }
