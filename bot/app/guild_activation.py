from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import discord

from app.modules import ModuleManager
from config import paths

log = logging.getLogger(__name__)


class GuildActivationBot(Protocol):
    @property
    def guilds(self) -> Sequence[Any]: ...

    def get_channel(self, channel_id: int, /) -> Any | None: ...

    async def fetch_channel(self, channel_id: int, /) -> Any: ...


class GuildActivationCacheFactory(Protocol):
    def __call__(
        self,
        config_dir: Path,
        parser: paths.ActivationParser,
        /,
    ) -> paths.GuildActivationCache: ...


@dataclass(frozen=True, slots=True)
class GuildActivationConfig:
    config_dir: Path
    allowed_guilds: frozenset[int]
    refresh_seconds: float


@dataclass(frozen=True, slots=True)
class GuildActivationState:
    active: bool
    activation: str
    setup_state: str
    environment_approved: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "active": self.active,
            "activation": self.activation,
            "setup_state": self.setup_state,
            "environment_approved": self.environment_approved,
        }


class GuildActivationService:
    def __init__(
        self,
        *,
        config: GuildActivationConfig,
        bot: GuildActivationBot,
        module_manager: ModuleManager,
        activation_parser: paths.ActivationParser,
        cache_factory: GuildActivationCacheFactory = paths.GuildActivationCache,
    ) -> None:
        self._config = config
        self._bot = bot
        self._module_manager = module_manager
        self._cache = cache_factory(config.config_dir, activation_parser)
        self._cache.refresh()
        self._refresh_task: asyncio.Task[None] | None = None

    @property
    def refresh_task(self) -> asyncio.Task[None] | None:
        return self._refresh_task

    def active_guilds(self) -> set[int]:
        """Guilds enabled by validated setup or the deployment allowlist.

        A validated explicit deactivation wins over the environment allowlist.
        This hot-path read uses an immutable cache; it never scans the config
        directory synchronously while processing a Discord event.
        """

        setup = self._cache.snapshot()
        active = (set(self._config.allowed_guilds) | set(setup.active)) - set(setup.deactivated)
        guild_settings = self._module_manager.guild_settings
        if guild_settings is not None:
            # An enforcement module with an invalid guild document takes the
            # guild offline rather than running unmoderated.
            active -= guild_settings.blocked_guilds()
        return active

    def guild_activation_state(self, guild_id: int) -> GuildActivationState:
        setup = self._cache.snapshot()
        environment_approved = guild_id in self._config.allowed_guilds
        if guild_id in setup.deactivated:
            setup_state = "deactivated"
            activation = "deactivated"
            active = False
        elif guild_id in setup.active:
            setup_state = "active"
            activation = "server_setup"
            active = True
        elif guild_id in setup.invalid:
            setup_state = "invalid"
            active = environment_approved
            activation = "environment" if active else "invalid_setup"
        else:
            setup_state = "missing"
            active = environment_approved
            activation = "environment" if active else "pending"
        return GuildActivationState(
            active=active,
            activation=activation,
            setup_state=setup_state,
            environment_approved=environment_approved,
        )

    async def refresh_guild_activation(self, guild_id: int | None = None) -> None:
        if guild_id is None:
            await asyncio.to_thread(self._cache.refresh)
        else:
            await asyncio.to_thread(self._cache.refresh_guild, guild_id)
        await self.refresh_module_guild_settings(guild_id)

    def known_guild_ids(self) -> set[int]:
        setup = self._cache.snapshot()
        known = set(self._config.allowed_guilds) | set(setup.active) | set(setup.deactivated)
        known |= {int(guild.id) for guild in self._bot.guilds}
        return known

    async def refresh_module_guild_settings(self, guild_id: int | None) -> None:
        service = self._module_manager.guild_settings
        if service is None:
            return
        targets = {guild_id} if guild_id is not None else self.known_guild_ids()
        batch = await asyncio.to_thread(service.build_refresh, targets)
        service.apply_refresh(batch)

    async def channel_guild_id(self, channel_id: int) -> int | None:
        channel = self._bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self._bot.fetch_channel(channel_id)
            except discord.NotFound, discord.Forbidden, discord.HTTPException:
                return None
        guild = getattr(channel, "guild", None)
        return None if guild is None else int(guild.id)

    def proposal_guild_health(self, guild_id: int) -> str:
        if self.guild_activation_state(guild_id).setup_state == "invalid":
            return "the guild configuration is invalid"
        service = self._module_manager.guild_settings
        if service is not None and guild_id in service.blocked_guilds():
            return "module guild settings would disable this guild"
        return ""

    async def on_guild_join(self, guild: discord.Guild) -> None:
        if guild.id in self.active_guilds():
            log.info("Joined active guild %s (%s)", guild.id, getattr(guild, "name", "?"))
            return
        log.warning(
            "Joined inactive guild %s (%s); staying connected but ignoring "
            "guild messages and commands until activation changes",
            guild.id,
            getattr(guild, "name", "?"),
        )

    def start(self) -> None:
        if self._refresh_task is not None:
            return
        self._refresh_task = asyncio.create_task(self.run_refresh_loop())
        log.info(
            "Guild activation refresher started (every %.0fs)",
            self._config.refresh_seconds,
        )

    async def close(self) -> None:
        task = self._refresh_task
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("Error stopping guild activation refresher")
        self._refresh_task = None

    async def run_refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(self._config.refresh_seconds)
            try:
                await self.refresh_guild_activation()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Could not refresh guild activation config")
