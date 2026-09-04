"""Process-wide live map of just-sent bot reply ids to their conversation.

Responses send chunk by chunk, but the durable ``message_contexts`` rows are
only written after every chunk lands. A user replying to the first visible
chunk while a later chunk is still sending would find no durable row and fall
through to a fresh root, bypassing serialization with the original turn.

Delivery registers each chunk here the moment ``channel.send()`` returns; reply
routing consults this map before the durable store. The durable write that
follows stays authoritative -- this map only bridges the send window.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass

from storage.conversations import CHANNEL_SHARED, ConversationAccessScope

_MAX_LIVE_ROUTES = 2000


@dataclass(frozen=True, slots=True)
class LiveReplyRoute:
    key: str
    db_conversation_id: int | None
    owner_user_id: str | None = None
    access_scope: ConversationAccessScope = CHANNEL_SHARED


_routes: OrderedDict[str, LiveReplyRoute] = OrderedDict()


def register_live_reply(
    discord_message_id: str,
    route: LiveReplyRoute,
) -> None:
    """Remember one just-sent chunk. Synchronous: runs between awaits."""
    if not discord_message_id:
        return
    _routes.pop(discord_message_id, None)
    _routes[discord_message_id] = route
    while len(_routes) > _MAX_LIVE_ROUTES:
        _routes.popitem(last=False)


def lookup_live_reply(discord_message_id: str) -> LiveReplyRoute | None:
    return _routes.get(discord_message_id)


def unregister_live_reply(discord_message_id: str) -> None:
    """Drop one bridge entry once its durable row has been attempted.

    Entries exist only for the send-to-persistence window; without this a
    stale route would keep overriding durable truth (including post-deletion)
    indefinitely.
    """
    _routes.pop(discord_message_id, None)
