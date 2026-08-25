from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

from commands.moderation_cmd import ModerationGroup, format_block_status, normalize_user_id
from storage.blocked_users import BlockedUserRecord
from trust.resolver import TrustResolver


class _Response:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def is_done(self) -> bool:
        return False

    async def send_message(self, content: str, **kwargs: Any) -> None:
        self.sent.append(content)


class _Interaction:
    def __init__(self, user_id: int, guild_id: int | None = None) -> None:
        self.user = SimpleNamespace(id=user_id)
        self.guild_id = guild_id
        self.response = _Response()


class _RecordingStore:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    async def block_user(self, user_id: str, *, blocked_by: str, reason: str = "") -> bool:
        self.calls.append((user_id, blocked_by, reason))
        return True


def _group(store: _RecordingStore, staff_ids: set[str]) -> ModerationGroup:
    resolver = TrustResolver(staff_role_ids=set(), regular_role_ids=set(), staff_ids=staff_ids)
    return ModerationGroup(cast(Any, store), resolver)


def _run_block(group: ModerationGroup, interaction: Any, user: Any, reason: str | None) -> None:
    asyncio.run(cast(Any, group).block.callback(group, interaction, user, reason))


def test_block_accepts_user_who_already_left_the_guild() -> None:
    # The target is a bare user (no guild Member object), e.g. a harasser who
    # left the server; the block must still be recordable.
    store = _RecordingStore()
    group = _group(store, staff_ids={"999"})
    interaction = _Interaction(user_id=999)

    _run_block(group, interaction, SimpleNamespace(id=123), "harassing in DMs")

    assert store.calls == [("123", "999", "harassing in DMs")]
    assert interaction.response.sent == ["Blocked `123`."]


def test_block_refuses_staff_id_target_even_without_member_object() -> None:
    # Staff-ID allowlist protection holds with no Member (roles unavailable).
    store = _RecordingStore()
    group = _group(store, staff_ids={"42"})
    interaction = _Interaction(user_id=999)

    _run_block(group, interaction, SimpleNamespace(id=42), None)

    assert store.calls == []
    assert interaction.response.sent == ["Staff users cannot be blocked."]


def test_block_refuses_self_block() -> None:
    store = _RecordingStore()
    group = _group(store, staff_ids=set())
    interaction = _Interaction(user_id=999)

    _run_block(group, interaction, SimpleNamespace(id=999), None)

    assert store.calls == []
    assert interaction.response.sent == ["You cannot block yourself."]


def test_normalize_user_id_accepts_mentions_and_plain_ids() -> None:
    assert normalize_user_id("123") == "123"
    assert normalize_user_id("<@123>") == "123"
    assert normalize_user_id("<@!123>") == "123"


def test_format_block_status_for_unblocked_user() -> None:
    assert format_block_status("123", None) == "`123` is not blocked."


def test_format_block_status_for_blocked_user() -> None:
    record = BlockedUserRecord(
        user_id="123",
        blocked_by="999",
        reason="spam",
        created_at=10.0,
        updated_at=20.0,
    )

    assert format_block_status("123", record) == (
        "`123` is blocked.\nBlocked by: `999`\nReason: spam"
    )
