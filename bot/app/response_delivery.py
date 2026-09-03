from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import discord

from app.foreground_turn import deliver_with_workspace_guard
from discord_adapter.gateway import DiscordGateway
from discord_adapter.io import SentMessages
from tools.workspace.common import UserLocks
from workspace import WorkspaceKey

if TYPE_CHECKING:
    from tools.embeds import EmbedSpec

log = logging.getLogger(__name__)


class DiscordResponseSender:
    """Deliver Discord responses while protecting workspace-backed files."""

    def __init__(
        self,
        *,
        gateway: DiscordGateway,
        workspace_locks: UserLocks,
    ) -> None:
        self._gateway = gateway
        self._workspace_locks = workspace_locks

    async def send(
        self,
        channel: discord.abc.Messageable,
        content: str,
        /,
        *,
        reference: discord.Message | None = None,
        output_files: list[str] | None = None,
        output_file_descriptions: dict[str, str] | None = None,
        allowed_file_roots: list[str | Path] | None = None,
        embed: EmbedSpec | None = None,
        mention_author: bool = False,
        workspace_key: WorkspaceKey | None = None,
        on_message_sent: Callable[[discord.Message], None] | None = None,
    ) -> SentMessages:
        async def send() -> SentMessages:
            return await self._gateway.send_response(
                channel,
                content,
                reference=reference,
                output_files=output_files,
                output_file_descriptions=output_file_descriptions,
                allowed_file_roots=allowed_file_roots,
                embed=embed,
                mention_author=mention_author,
                on_message_sent=on_message_sent,
            )

        return await deliver_with_workspace_guard(
            workspace_locks=self._workspace_locks,
            workspace_key=workspace_key,
            output_files=output_files,
            deliver=send,
        )
