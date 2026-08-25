"""Thread-handoff participation state (docs/thread-handoff.md).

``ThreadHandoffManager`` owns the durable mapping of bot-created threads to their
root conversations (``thread_conversations``) plus the two in-memory id sets the
message path consults: every managed thread, and the subset currently answering
without a mention. Only threads the bot created through ``move_to_thread`` are
ever enrolled; a stray @mention inside someone else's thread stays a one-shot
mention-gated reply.

Managed and auto-responding are deliberately separate facts. A paused thread is
still managed, so it still resolves to its root conversation when someone
mentions the bot in it. Dropping it from the mapping would sever the transcript.
Both sets are private: callers ask ``is_managed`` / ``is_auto_responding`` rather
than sharing a set, so the ``auto_respond ⊆ participation`` invariant cannot be
broken from outside.

No ``discord`` import here: the manager works on plain thread ids so it is
unit-testable and usable from tool handlers.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from storage.conversations import ConversationStore

log = logging.getLogger(__name__)


class ThreadHandoffManager:
    def __init__(self, store: ConversationStore) -> None:
        self._store = store
        self._participation: set[int] = set()
        self._auto_respond: set[int] = set()

    async def load(self) -> None:
        """Seed both sets from SQLite so participation survives restarts."""
        for thread_id, auto_respond in await self._store.list_thread_conversations():
            try:
                parsed = int(thread_id)
            except ValueError:
                log.warning("Ignoring non-numeric thread id in thread_conversations: %r", thread_id)
                continue
            self._participation.add(parsed)
            if auto_respond:
                self._auto_respond.add(parsed)

    @property
    def managed_count(self) -> int:
        return len(self._participation)

    @property
    def auto_respond_count(self) -> int:
        return len(self._auto_respond)

    def is_managed(self, thread_id: int) -> bool:
        return thread_id in self._participation

    def is_auto_responding(self, thread_id: int) -> bool:
        """Whether this thread answers every message without needing a mention."""
        return thread_id in self._auto_respond

    async def enroll(
        self,
        thread_id: int,
        conversation_id: int,
        *,
        creator_user_id: str,
        auto_respond: bool = True,
    ) -> None:
        await self._store.map_thread_conversation(
            str(thread_id),
            conversation_id,
            creator_user_id=creator_user_id,
            auto_respond=auto_respond,
        )
        self._participation.add(thread_id)
        self._remember_mode(thread_id, auto_respond)

    async def is_creator(self, thread_id: int, user_id: str) -> bool:
        """Whether ``user_id`` is the recorded initiator of this thread.

        The database is authoritative instead of the current conversation
        owner: shared roots can be handed off by a later participant, and a
        restart must not broaden or lose that lifecycle permission.
        """
        if thread_id not in self._participation or not user_id:
            return False
        creator = await self._store.get_thread_creator_user_id(str(thread_id))
        return creator is not None and creator == user_id

    async def pause(self, thread_id: int) -> bool:
        """Stop answering unprompted here; the thread stays mapped to its root."""
        return await self._set_auto_respond(thread_id, False)

    async def resume(self, thread_id: int) -> bool:
        """Answer every message here again. True if the thread was managed."""
        return await self._set_auto_respond(thread_id, True)

    async def leave(self, thread_id: int) -> bool:
        """End participation; the thread reverts to mention-only. True if it was managed."""
        managed = thread_id in self._participation
        await self._store.delete_thread_conversation(str(thread_id))
        self.forget(thread_id)
        return managed

    def forget(self, thread_id: int) -> None:
        """Drop in-memory state for a thread whose row is already gone.

        The routing path uses this when a mapped row has been swept out from
        under a live id, so both sets stay consistent without a redundant write.
        """
        self._participation.discard(thread_id)
        self._auto_respond.discard(thread_id)

    async def prune(self, thread_id: int) -> None:
        """Drop a thread that turned out to be gone (deleted/locked)."""
        log.info("Pruning unreachable managed thread %s", thread_id)
        await self.leave(thread_id)

    async def _set_auto_respond(self, thread_id: int, auto_respond: bool) -> bool:
        if thread_id not in self._participation:
            return False
        if not await self._store.set_thread_auto_respond(str(thread_id), auto_respond):
            # The row went away underneath us (retention sweep, privacy wipe).
            # Drop the stale id instead of reporting a mode change that would not
            # survive a restart; the next message opens a fresh root.
            self.forget(thread_id)
            return False
        self._remember_mode(thread_id, auto_respond)
        return True

    def _remember_mode(self, thread_id: int, auto_respond: bool) -> None:
        if auto_respond:
            self._auto_respond.add(thread_id)
        else:
            self._auto_respond.discard(thread_id)
