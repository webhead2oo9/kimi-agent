"""Owner-only ``/modules`` commands: status and manifest."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from commands._shared import send_message
from kimi_agent_module_api import ModuleSpec
from kimi_agent_module_api.contracts import ModuleHealth

_STATE_ICON = {"healthy": "✅", "starting": "⏳", "degraded": "⚠️", "failed": "❌"}


def render_status(
    requested: tuple[str, ...],
    specs: Mapping[str, ModuleSpec],
    health: Mapping[str, ModuleHealth],
    *,
    now: float | None = None,
) -> str:
    if not requested:
        return "No application modules are configured (`KIMI_MODULES` is empty)."
    now = time.time() if now is None else now
    lines = ["**Modules**"]
    for name in requested:
        spec = specs.get(name)
        state = health.get(name)
        version = spec.version if spec is not None else "?"
        if state is None:
            lines.append(f"• `{name}` {version} — not loaded")
            continue
        age = max(0, int(now - state.updated_at))
        line = (
            f"• {_STATE_ICON.get(state.state, '•')} `{name}` {version} — {state.state} ({age}s ago)"
        )
        if state.detail:
            line += f": {state.detail}"
        if state.metrics:
            metrics = ", ".join(f"{k}={v:g}" for k, v in sorted(state.metrics.items())[:8])
            line += f"\n  {metrics}"
        lines.append(line)
    return "\n".join(lines)


def render_manifest(
    specs: Mapping[str, ModuleSpec],
    health: Mapping[str, ModuleHealth],
    tools: Callable[[str], tuple[str, ...]] | None = None,
) -> str:
    if not specs:
        return "No application modules are configured."
    blocks: list[str] = []
    for name, spec in specs.items():
        perms = spec.permissions
        state = health.get(name)
        lines = [f"**`{name}`** {spec.version} — {state.state if state else 'not loaded'}"]
        if spec.dependencies:
            lines.append(f"  depends on: {', '.join(spec.dependencies)}")
        if spec.requires_capabilities:
            lines.append(f"  capabilities: {', '.join(spec.requires_capabilities)}")
        if spec.provides:
            lines.append("  provides: " + ", ".join(f"{d.name}@{d.version}" for d in spec.provides))
        if spec.consumes:
            lines.append(
                "  consumes: "
                + ", ".join(f"{r.name}@{r.version} from {r.provider}" for r in spec.consumes)
            )
        lines.append(
            "  discord actions: "
            + (", ".join(sorted(perms.discord_actions)) if perms.discord_actions else "none")
        )
        if perms.event_topics:
            lines.append(f"  event topics: {', '.join(perms.event_topics)}")
        if perms.http_hosts:
            hosts = ", ".join(
                f"{rule.host} ({rule.network}, {'/'.join(rule.schemes)})"
                for rule in perms.http_hosts
            )
            lines.append(f"  http hosts: {hosts}")
        escapes = [flag for flag in ("raw_bot", "raw_storage") if getattr(perms, flag)]
        if perms.override_target_policy:
            escapes.append("override_target_policy")
        if escapes:
            lines.append(f"  ⚠️ escape hatches: {', '.join(escapes)}")
        if spec.table_aliases:
            lines.append(
                "  table aliases: "
                + ", ".join(f"{k}→{v}" for k, v in sorted(spec.table_aliases.items()))
            )
        if spec.guild_settings is not None:
            fields = ", ".join(f.name for f in spec.guild_settings.fields) or "none"
            lines.append(
                f"  guild settings: {fields} (invalid → {spec.guild_settings.invalid_policy})"
            )
        if tools is not None:
            names = tools(name)
            if names:
                lines.append(f"  llm tools: {', '.join(names)}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


class ModulesGroup(app_commands.Group):
    def __init__(
        self,
        *,
        owner_user_id: str,
        requested: Callable[[], tuple[str, ...]],
        specs: Callable[[], Mapping[str, ModuleSpec]],
        health: Callable[[], Mapping[str, ModuleHealth]],
        tools: Callable[[str], tuple[str, ...]] | None = None,
    ) -> None:
        super().__init__(name="modules", description="Application module status (bot owner only)")
        self._owner_user_id = owner_user_id
        self._requested = requested
        self._specs = specs
        self._health = health
        self._tools = tools

    async def _owner(self, interaction: discord.Interaction) -> bool:
        if self._owner_user_id and str(interaction.user.id) == self._owner_user_id:
            return True
        await send_message(interaction, "Bot owner only.")
        return False

    @app_commands.command(name="status", description="Health of every configured module")
    async def status(self, interaction: discord.Interaction) -> None:
        if not await self._owner(interaction):
            return
        await send_message(
            interaction, render_status(self._requested(), self._specs(), self._health())
        )

    @app_commands.command(name="manifest", description="What each module declares it uses")
    async def manifest(self, interaction: discord.Interaction) -> None:
        if not await self._owner(interaction):
            return
        await send_message(interaction, render_manifest(self._specs(), self._health(), self._tools))


def register_modules_command(
    bot: commands.Bot,
    *,
    owner_user_id: str,
    requested: Callable[[], tuple[str, ...]],
    specs: Callable[[], Mapping[str, ModuleSpec]],
    health: Callable[[], Mapping[str, ModuleHealth]],
    tools: Callable[[str], tuple[str, ...]] | None = None,
) -> Any:
    group = ModulesGroup(
        owner_user_id=owner_user_id,
        requested=requested,
        specs=specs,
        health=health,
        tools=tools,
    )
    bot.tree.add_command(group, override=True)
    return group


__all__ = ["ModulesGroup", "register_modules_command", "render_manifest", "render_status"]
