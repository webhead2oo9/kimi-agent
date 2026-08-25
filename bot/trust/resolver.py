from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from trust.tiers import TrustTier

if TYPE_CHECKING:
    import discord


@dataclass(frozen=True)
class GuildTrust:
    """Per-guild trust lists read from a guild fragment's frontmatter.

    These are *additive*: ``TrustResolver`` merges (OR) them with the global
    ``STAFF_*``/``REGULAR_*`` allowlists, never replacing them. A guild can grant
    local staff/regular standing, but can never strip someone the global config
    trusts. See ``config/fragments/guild_config.py``.
    """

    staff_user_ids: frozenset[str] = frozenset()
    staff_role_ids: frozenset[str] = frozenset()
    regular_role_ids: frozenset[str] = frozenset()

    @property
    def is_empty(self) -> bool:
        return not (self.staff_user_ids or self.staff_role_ids or self.regular_role_ids)


EMPTY_GUILD_TRUST = GuildTrust()


class TrustResolver:
    def __init__(
        self,
        staff_role_ids: set[str],
        regular_role_ids: set[str],
        staff_ids: set[str],
        *,
        guild_trust_loader: Callable[[str], GuildTrust] | None = None,
    ):
        self._staff_role_ids = staff_role_ids
        self._regular_role_ids = regular_role_ids
        self._staff_ids = staff_ids
        # Reads per-guild trust from operator config, fresh each call so staff
        # edits take effect without a restart. None => global lists only.
        self._guild_trust_loader = guild_trust_loader

    def resolve(
        self,
        member: discord.Member | None,
        user_id: str,
        guild_id: str | None = None,
    ) -> TrustTier:
        staff_ids = self._staff_ids
        staff_role_ids = self._staff_role_ids
        regular_role_ids = self._regular_role_ids

        if guild_id and self._guild_trust_loader is not None:
            guild = self._guild_trust_loader(guild_id)
            if not guild.is_empty:
                staff_ids = staff_ids | guild.staff_user_ids
                staff_role_ids = staff_role_ids | guild.staff_role_ids
                regular_role_ids = regular_role_ids | guild.regular_role_ids

        if user_id in staff_ids:
            return TrustTier.STAFF

        if member is None:
            return TrustTier.MEMBER

        member_role_ids = {
            str(role.id) for role in member.roles if getattr(role, "id", None) is not None
        }

        if member_role_ids & staff_role_ids:
            return TrustTier.STAFF

        if member_role_ids & regular_role_ids:
            return TrustTier.REGULAR

        return TrustTier.MEMBER
