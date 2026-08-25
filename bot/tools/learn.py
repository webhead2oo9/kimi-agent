"""The learn feature's shared, ``discord``-free vocabulary.

A "learn this" gesture can land in one of two sinks: community memory (the
``teach`` tool) or a shared skill document (``skill_create`` / ``skill_edit``).
The pieces that surround those sinks sit in packages that may not import each
other: ``tools/`` never imports ``discord``, and ``commands/`` must never
import ``app/`` (``app/runtime.py`` imports every command module, so that edge
would be an import-order cycle). This module is the neutral middle they share:

* :class:`LearnTarget` is the message a staff member asked the bot to learn
  from, reduced to plain data at the Discord boundary (``commands/learn_cmd.py``)
  and consumed by the scoped turn (``app/learn_turn.py``).
* :class:`LearnEvent` and :data:`LearnHook` are what a sink announces after it
  commits knowledge, rendered to a log card by ``app/learn_log.py``.

Emitting is best-effort by construction: :func:`emit_learn_event` swallows
everything, because a logging failure must never fail the tool call that
already committed the knowledge.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from dataclasses import dataclass

log = logging.getLogger(__name__)

SINK_SKILL = "skill"
SINK_COMMUNITY_MEMORY = "community_memory"

SCOPE_THIS_GUILD = "this server"
SCOPE_ALL_GUILDS = "all servers"


@dataclass(frozen=True)
class LearnTarget:
    """The Discord message a staff member asked the bot to learn from.

    ``message_id`` is carried as an id rather than recovered from ``jump_url``
    because the turn passes it through as the trigger message, which is what
    puts a source link on the resulting audit card.

    Every field except the ids is attacker-controlled: any member can choose the
    text staff points at, and a display name or filename is just as authored as
    the body.
    """

    content: str
    author_name: str
    author_id: str
    jump_url: str
    message_id: str = ""
    channel_id: str = ""
    attachment_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class LearnEvent:
    """One staff-initiated write into shared guild knowledge."""

    sink: str
    action: str
    guild_id: str | None
    user_id: str
    user_name: str
    subject: str
    summary: str = ""
    scope: str = ""
    source_url: str = ""


LearnHook = Callable[[LearnEvent], Awaitable[None]]


def jump_url(
    guild_id: str | None,
    channel_id: str,
    message_id: str,
) -> str:
    """Best-effort Discord permalink for the message behind a learn event.

    Returns ``""`` rather than a half-built URL when any part is missing, so a
    DM or a direct (non-Discord) caller simply logs no source link.
    """
    if not guild_id or not channel_id or not message_id:
        return ""
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"


async def emit_learn_event(
    hook: LearnHook | None,
    build_event: Callable[[], LearnEvent],
) -> None:
    """Fire the audit hook without ever surfacing a failure to the caller.

    Takes a *factory* rather than an event so that building the event is inside
    the same guard as delivering it. Callers run this after the knowledge is
    already committed, where any escaping exception would report failure for a
    write that actually succeeded and invite a duplicate retry.
    """
    if hook is None:
        return
    try:
        await hook(build_event())
    except Exception:
        log.exception("Failed to emit a learn audit event")
