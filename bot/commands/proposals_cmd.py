"""Owner-only Discord review surface for durable module proposals."""

from __future__ import annotations

import json

import discord
from discord import app_commands
from discord.ext import commands

from commands._shared import send_message
from kimi_agent_module_api import (
    ConfigurationService,
    ProposalError,
    ProposalRecord,
    ProposalService,
)


def _short_id(record: ProposalRecord) -> str:
    return record.proposal_id[:12]


def _detail(record: ProposalRecord) -> str:
    changes = json.dumps(
        record.preview.redacted_changes,
        ensure_ascii=False,
        indent=2,
        default=str,
    )
    if len(changes) > 2800:
        changes = changes[:2797] + "..."
    warnings = "\n".join(f"- {warning}" for warning in record.preview.warnings)
    text = (
        f"**{record.summary}**\n"
        f"ID: `{record.proposal_id}`\n"
        f"State: `{record.state}` · Action: `{record.action}`\n"
        f"Module: `{record.module_name}` · Activation: `{record.preview.activation}`\n"
        f"Proposed by: <@{record.actor.user_id}> via `{record.actor.source}`\n"
        f"Base revision: `{record.preview.revision}`\n"
        f"```json\n{changes}\n```"
    )
    if warnings:
        text += f"\nWarnings:\n{warnings}"
    if record.result_message:
        text += f"\nResult: {record.result_message}"
    return text[:4000]


class ProposalsGroup(app_commands.Group):
    def __init__(
        self,
        service: ProposalService,
        *,
        owner_user_id: str,
        configuration: ConfigurationService | None = None,
    ) -> None:
        super().__init__(name="proposals", description="Review Kimi configuration proposals")
        self._service = service
        self._owner_user_id = owner_user_id
        self._configuration = configuration

    async def _owner(self, interaction: discord.Interaction) -> bool:
        if self._owner_user_id and str(interaction.user.id) == self._owner_user_id:
            return True
        await send_message(interaction, "Bot owner only.")
        return False

    @app_commands.command(name="list", description="List recent configuration proposals")
    async def list_proposals(self, interaction: discord.Interaction) -> None:
        if not await self._owner(interaction):
            return
        records = await self._service.list()
        if not records:
            await send_message(interaction, "No proposals have been created.")
            return
        lines = [
            f"`{_short_id(record)}` · **{record.state}** · {record.summary[:100]}"
            for record in records[:20]
        ]
        await send_message(interaction, "\n".join(lines))

    @app_commands.command(
        name="stage-secret",
        description="Store a credential and return an opaque reference",
    )
    async def stage_secret(
        self,
        interaction: discord.Interaction,
        name: str,
        value: str,
    ) -> None:
        if not await self._owner(interaction):
            return
        if self._configuration is None:
            await send_message(interaction, "Managed credential storage is unavailable.")
            return
        try:
            reference = await self._configuration.stage_secret(name, value)
        except (ValueError, RuntimeError) as exc:
            await send_message(interaction, f"Credential was not stored: {exc}")
            return
        await send_message(
            interaction,
            f"Credential stored. Use `{reference}` in a proposal; its value cannot be read back.",
        )

    @app_commands.command(name="show", description="Show one proposal and its redacted diff")
    async def show(self, interaction: discord.Interaction, proposal_id: str) -> None:
        if not await self._owner(interaction):
            return
        record = await self._resolve(proposal_id)
        if record is None:
            await send_message(interaction, "No matching proposal.")
            return
        await send_message(interaction, _detail(record))

    @app_commands.command(name="approve", description="Approve and apply one proposal")
    async def approve(self, interaction: discord.Interaction, proposal_id: str) -> None:
        if not await self._owner(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        record = await self._resolve(proposal_id)
        if record is None:
            await interaction.followup.send("No matching proposal.", ephemeral=True)
            return
        try:
            applied = await self._service.approve(
                record.proposal_id, owner_user_id=str(interaction.user.id)
            )
        except (ProposalError, ValueError, RuntimeError) as exc:
            await interaction.followup.send(f"Proposal was not applied: {exc}", ephemeral=True)
            return
        await interaction.followup.send(_detail(applied), ephemeral=True)

    @app_commands.command(name="reject", description="Reject one pending proposal")
    async def reject(
        self, interaction: discord.Interaction, proposal_id: str, reason: str = ""
    ) -> None:
        if not await self._owner(interaction):
            return
        record = await self._resolve(proposal_id)
        if record is None:
            await send_message(interaction, "No matching proposal.")
            return
        try:
            rejected = await self._service.reject(
                record.proposal_id,
                owner_user_id=str(interaction.user.id),
                reason=reason,
            )
        except ProposalError as exc:
            await send_message(interaction, f"Proposal was not rejected: {exc}")
            return
        await send_message(interaction, _detail(rejected))

    async def _resolve(self, token: str) -> ProposalRecord | None:
        exact = await self._service.get(token.strip())
        if exact is not None:
            return exact
        matches = [
            record
            for record in await self._service.list()
            if record.proposal_id.startswith(token.strip())
        ]
        return matches[0] if len(matches) == 1 else None


def register_proposals_command(
    bot: commands.Bot,
    service: ProposalService,
    *,
    owner_user_id: str,
    configuration: ConfigurationService | None = None,
) -> None:
    bot.tree.add_command(
        ProposalsGroup(
            service,
            owner_user_id=owner_user_id,
            configuration=configuration,
        ),
        override=True,
    )


__all__ = ["ProposalsGroup", "register_proposals_command"]
