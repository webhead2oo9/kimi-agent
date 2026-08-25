from __future__ import annotations

import logging
from typing import Any

from memory.client import MemoryClient

log = logging.getLogger(__name__)

SKILLS_BANK = "bot-skills"


def user_bank_id(discord_id: str) -> str:
    return f"user:{discord_id}"


def community_bank_id(guild_id: str | None) -> str | None:
    """Resolve a guild's community bank id.

    Returns ``None`` when there is no guild (fail safe: no community read/write
    happens off a junk bank). Every guild has its own namespaced bank.
    """
    if not guild_id:
        return None
    return f"community:{guild_id}"


def forget_initialized_bank(bank_id: str) -> None:
    _initialized_banks.discard(bank_id)


# Bank personalities. ``reflect_mission`` frames recall/reflect; ``retain_mission``
# + ``retain_extraction_mode`` steer what auto-extraction keeps; ``observations_mission``
# steers consolidation/dedup. All are applied through the backend ``/config`` PATCH
# (``MemoryClient.create_bank`` -> ``update_bank_config``); bank-create does not
# consume the mission and disposition fields.
COMMUNITY_CONFIG: dict[str, Any] = {
    "name": "Community Knowledge",
    "reflect_mission": ("You are the shared knowledge base for a community Discord server."),
    "retain_mission": (
        "Retain durable, generalizable, factual knowledge the community has "
        "established: hardware quirks and fixes, troubleshooting steps, how-tos, "
        "product/spec facts, and public event or rule details. Prefer claims that "
        "would hold true regardless of who said them. Ignore personal preferences, "
        "individual setups, opinions, speculation, casual greetings, and off-topic chat."
    ),
    "retain_extraction_mode": "concise",
    "observations_mission": (
        "Consolidate into a compact, de-duplicated set of generally-true community "
        "facts. Merge repeats and drop superseded items."
    ),
    "disposition": {
        "skepticism": 3,
        "literalism": 4,
        "empathy": 2,
    },
}

USER_CONFIG: dict[str, Any] = {
    "reflect_mission": (
        "You hold durable, first-party facts about a single Discord community "
        "member. Reason only about this user."
    ),
    "retain_mission": (
        "Retain ONLY durable facts about THIS user that will still matter in future "
        "conversations: stable preferences, owned hardware and gear, ongoing projects, "
        "skills, and lasting personal context they volunteer. IGNORE one-off questions "
        "or things they merely asked about, transient state (today's weather, current "
        "game scores, mood), the assistant's own statements or persona, facts about "
        "other people, and in-character roleplay, jokes, or hypotheticals. Do NOT "
        "retain sensitive attributes (weight, health, ethnicity, religion, politics, "
        "weapons, precise home location) unless the user explicitly asks to be "
        "remembered for it. Write every fact in concise English."
    ),
    "retain_extraction_mode": "concise",
    "observations_mission": (
        "Consolidate into a compact, de-duplicated set of durable facts about the "
        "user. Merge repeats, drop superseded or transient items, and keep the most "
        "general true form of each fact."
    ),
    "disposition": {
        "skepticism": 4,
        "literalism": 3,
        "empathy": 3,
    },
}

SKILLS_CONFIG: dict[str, Any] = {
    "name": "Bot Skills",
    "reflect_mission": (
        "You store skill definitions and procedural knowledge for the bot itself. "
        "Each skill describes how to handle a specific type of request."
    ),
    "disposition": {
        "skepticism": 3,
        "literalism": 5,
        "empathy": 1,
    },
}


def bank_config_kwargs(config: dict[str, Any]) -> dict[str, Any]:
    """Map a ``*_CONFIG`` dict onto :meth:`MemoryClient.create_bank` keyword args.

    Missing keys default empty so a config may omit the levers it does not set
    (e.g. the skills bank declares no retain mission).
    """
    return {
        "reflect_mission": config.get("reflect_mission", ""),
        "retain_mission": config.get("retain_mission", ""),
        "retain_extraction_mode": config.get("retain_extraction_mode", ""),
        "observations_mission": config.get("observations_mission", ""),
        "disposition": config.get("disposition"),
    }


_initialized_banks: set[str] = set()


async def ensure_global_banks(client: MemoryClient) -> bool:
    """Ensure the globally-shared banks exist (probes memory readiness).

    Only the bot-skills bank is global; per-guild community banks are created
    lazily on first use via :func:`ensure_community_bank`. The boolean return is
    the bot's memory-readiness signal.
    """
    if SKILLS_BANK not in _initialized_banks:
        log.info("Ensuring skills bank exists")
        created = await client.create_bank(
            bank_id=SKILLS_BANK,
            name=SKILLS_CONFIG["name"],
            **bank_config_kwargs(SKILLS_CONFIG),
        )
        if created:
            _initialized_banks.add(SKILLS_BANK)
        else:
            return False

    return True


async def ensure_community_bank(client: MemoryClient, guild_id: str | None) -> str | None:
    """Lazily ensure a guild's community bank exists, returning its bank id.

    Returns ``None`` when there is no guild or when bank creation fails, so
    callers can degrade gracefully instead of reading/writing a junk bank.
    """
    bank_id = community_bank_id(guild_id)
    if bank_id is None:
        return None
    if bank_id not in _initialized_banks:
        log.info("Ensuring community bank %s exists", bank_id)
        created = await client.create_bank(
            bank_id=bank_id,
            name=COMMUNITY_CONFIG["name"],
            **bank_config_kwargs(COMMUNITY_CONFIG),
        )
        if not created:
            return None
        _initialized_banks.add(bank_id)
    return bank_id


async def ensure_user_bank(
    client: MemoryClient,
    discord_id: str,
    display_name: str,
) -> str | None:
    """Ensure a configured per-user bank, returning ``None`` on any create failure."""
    bank_id = user_bank_id(discord_id)
    if bank_id not in _initialized_banks:
        log.info("Ensuring user bank for %s (%s)", display_name, discord_id)
        created = await client.create_bank(
            bank_id=bank_id,
            name=f"{display_name}'s Memory",
            **bank_config_kwargs(USER_CONFIG),
        )
        if created:
            _initialized_banks.add(bank_id)
        else:
            return None
    return bank_id
