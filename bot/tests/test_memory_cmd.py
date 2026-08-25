from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest

from utils.privacy_barrier import UserPrivacyBarrier
from commands.memory_cmd import MemoryGroup
from memory.mutations import user_memory_mutation
from storage.preferences import PreferenceStore


class _Response:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.deferred = False

    async def defer(self, **kwargs: Any) -> None:
        assert kwargs == {"ephemeral": True, "thinking": True}
        self.deferred = True

    async def send_message(self, content: str, **kwargs: Any) -> None:
        assert kwargs["ephemeral"] is True
        self.sent.append(content)


class _Interaction:
    def __init__(self) -> None:
        self.user = SimpleNamespace(id=42)
        self.response = _Response()
        self.edited: list[str] = []

    async def edit_original_response(self, *, content: str) -> None:
        self.edited.append(content)


class _Preferences:
    def __init__(self) -> None:
        self.enabled = True
        self.changes: list[bool] = []

    async def is_memory_enabled(self, user_id: str) -> bool:
        assert user_id == "42"
        return self.enabled

    async def set_memory_enabled(self, user_id: str, enabled: bool) -> bool:
        assert user_id == "42"
        changed = self.enabled != enabled
        self.enabled = enabled
        self.changes.append(enabled)
        return changed


@pytest.mark.asyncio
async def test_opt_in_waits_until_exclusive_privacy_deletion_finishes() -> None:
    """A queued opt-in cannot overtake the tail of a confirmed data wipe."""

    barrier = UserPrivacyBarrier()
    preferences = _Preferences()
    deletion_disabled_memory = asyncio.Event()
    release_deletion = asyncio.Event()

    async def delete() -> None:
        async with barrier.deletion("42"):
            # Model forget_user_memory's complete preference/bank transition.
            async with user_memory_mutation("42"):
                await preferences.set_memory_enabled("42", False)
            deletion_disabled_memory.set()
            # The mutation lock is now free, but the complete privacy deletion
            # still owns its exclusive lease while other stores finish.
            await release_deletion.wait()

    deletion = asyncio.create_task(delete())
    await deletion_disabled_memory.wait()

    group = MemoryGroup(
        cast(PreferenceStore, preferences),
        privacy_barrier=barrier,
    )
    interaction = _Interaction()
    opt_in = asyncio.create_task(cast(Any, group).opt_in.callback(group, interaction))
    await asyncio.sleep(0)

    assert preferences.changes == [False]
    assert interaction.response.deferred is True
    assert interaction.response.sent == []
    assert interaction.edited == []

    release_deletion.set()
    await asyncio.gather(deletion, opt_in)

    # A genuinely later interaction may re-enable memory, but only after the
    # confirmed wipe and its user-facing result have completed.
    assert preferences.changes == [False, True]
    assert interaction.response.sent == []
    assert interaction.edited == [
        "Memory **enabled**. I can use and retain long-term Hindsight memories for you."
    ]


def test_memory_group_exposes_only_self_service_commands() -> None:
    # Staff memory administration is not exposed on /memory; the command keeps
    # only the end-user toggles + status (forget-me lives on /privacy).
    preferences = cast(PreferenceStore, object())

    group = MemoryGroup(preferences)

    assert {command.name for command in group.commands} == {
        "opt-in",
        "opt-out",
        "status",
    }
