from __future__ import annotations

from dataclasses import dataclass, field
from typing import cast

import discord

from branding import DEFAULT_BOT_NAME
from discord_adapter.io import (
    can_send_reply,
    should_respond,
    strip_mention,
)


@dataclass
class _Author:
    id: int
    bot: bool = False


@dataclass
class _Channel:
    id: int = 100


@dataclass
class _Message:
    author: _Author
    channel: object = field(default_factory=_Channel)
    content: str = ""
    mentions: list[_Author] = field(default_factory=list)
    type: discord.MessageType = discord.MessageType.default
    mention_everyone: bool = False


class _BotUser(_Author):
    def mentioned_in(self, message: _Message) -> bool:
        # Mirror real discord.py semantics: ClientUser.mentioned_in short-circuits
        # True on a mass-ping. should_respond must NOT rely on this.
        if message.mention_everyone:
            return True
        return any(user.id == self.id for user in message.mentions)


def _should(
    message: _Message,
    *,
    bot_user: _BotUser | None = None,
    allowed_channels: set[int] | None = None,
    thread_participation: set[int] | None = None,
) -> bool:
    """``thread_participation`` here means "threads currently auto-responding".

    In production that predicate is answered by ``ThreadHandoffManager``, which
    reports False for an unmanaged thread *and* for a managed one someone paused.
    """
    bot = bot_user or _BotUser(id=999, bot=True)
    auto_responding = thread_participation or set()
    return should_respond(
        message,  # type: ignore[arg-type]
        bot_user=bot,  # type: ignore[arg-type]
        bot_name=DEFAULT_BOT_NAME,
        responds_without_mention=lambda thread_id: thread_id in auto_responding,
        allowed_guilds=set(),
        allowed_channels=allowed_channels,
    )


def test_dm_message_is_ignored_even_from_human(monkeypatch):
    dm_channel = type("_DMChannel", (), {})
    monkeypatch.setattr(discord, "DMChannel", dm_channel)
    message = _Message(author=_Author(id=123), channel=dm_channel())

    assert _should(message) is False


def test_normal_channel_message_with_bot_mention_responds():
    bot_user = _BotUser(id=999, bot=True)
    message = _Message(author=_Author(id=123), mentions=[bot_user])

    assert _should(message, bot_user=bot_user) is True


def test_normal_channel_reply_with_bot_mention_responds():
    bot_user = _BotUser(id=999, bot=True)
    message = _Message(
        author=_Author(id=123),
        mentions=[bot_user],
        type=discord.MessageType.reply,
    )

    assert _should(message, bot_user=bot_user) is True


def test_normal_channel_reply_without_bot_mention_is_ignored():
    message = _Message(author=_Author(id=123), type=discord.MessageType.reply)

    assert _should(message) is False


def test_hey_bot_name_text_invocation_responds_without_mention():
    message = _Message(author=_Author(id=123), content="hey kimi can you help?")

    assert _should(message) is True


def test_hey_bot_name_text_invocation_is_case_insensitive():
    message = _Message(author=_Author(id=123), content="Hey Kimi")

    assert _should(message) is True


def test_hi_bot_name_text_invocation_accepts_punctuation():
    message = _Message(author=_Author(id=123), content="Hi, Kimi! ping?")

    assert _should(message) is True


def test_bot_name_help_text_invocation_responds_without_mention():
    message = _Message(author=_Author(id=123), content="kimi help")

    assert _should(message) is True


def test_text_invocation_must_start_the_message():
    message = _Message(author=_Author(id=123), content="Alice said hey kimi")

    assert _should(message) is False


def test_text_invocation_requires_the_command_phrase():
    message = _Message(author=_Author(id=123), content="kimi what can you do?")

    assert _should(message) is False


def test_managed_thread_responds_without_mention(monkeypatch):
    thread_channel = type("_ThreadChannel", (_Channel,), {})
    monkeypatch.setattr(discord, "Thread", thread_channel)
    message = _Message(author=_Author(id=123), channel=thread_channel(id=321))

    assert _should(message, thread_participation={321}) is True


def test_unmanaged_thread_still_requires_mention(monkeypatch):
    thread_channel = type("_ThreadChannel", (_Channel,), {})
    monkeypatch.setattr(discord, "Thread", thread_channel)
    message = _Message(author=_Author(id=123), channel=thread_channel(id=321))

    assert _should(message, thread_participation={654}) is False
    assert _should(message, thread_participation=set()) is False


def test_paused_thread_falls_back_to_the_ordinary_channel_gates(monkeypatch):
    """A paused managed thread behaves exactly like any other channel.

    The predicate reports False for it, so nothing is answered unprompted, but
    every normal trigger still works, which is the whole way back in.
    """
    thread_channel = type("_ThreadChannel", (_Channel,), {})
    monkeypatch.setattr(discord, "Thread", thread_channel)
    bot = _BotUser(id=999, bot=True)

    plain = _Message(author=_Author(id=123), channel=thread_channel(id=321), content="hi all")
    assert _should(plain, bot_user=bot, thread_participation=set()) is False

    mentioned = _Message(
        author=_Author(id=123),
        channel=thread_channel(id=321),
        mentions=[bot],
    )
    assert _should(mentioned, bot_user=bot, thread_participation=set()) is True

    greeted = _Message(
        author=_Author(id=123),
        channel=thread_channel(id=321),
        content="hey kimi start responding again",
    )
    assert _should(greeted, bot_user=bot, thread_participation=set()) is True


def test_managed_thread_does_not_override_author_gates(monkeypatch):
    thread_channel = type("_ThreadChannel", (_Channel,), {})
    monkeypatch.setattr(discord, "Thread", thread_channel)
    message = _Message(
        author=_Author(id=555, bot=True),
        channel=thread_channel(id=321),
    )

    assert _should(message, thread_participation={321}) is False


def test_disallowed_channel_blocks_even_with_mention():
    bot_user = _BotUser(id=999, bot=True)
    message = _Message(
        author=_Author(id=123),
        channel=_Channel(id=555),
        mentions=[bot_user],
    )

    assert _should(message, bot_user=bot_user, allowed_channels={100}) is False


def test_bot_authored_messages_are_ignored_even_with_mention():
    bot_user = _BotUser(id=999, bot=True)
    message = _Message(author=_Author(id=456, bot=True), mentions=[bot_user])

    assert _should(message, bot_user=bot_user) is False


@dataclass
class _Perms:
    send_messages: bool = True
    send_messages_in_threads: bool = True


@dataclass
class _PermChannel:
    perms: _Perms = field(default_factory=_Perms)
    raises: bool = False

    def permissions_for(self, member: object) -> _Perms:
        if self.raises:
            raise RuntimeError("permission resolution failed")
        return self.perms


def test_can_send_reply_allows_when_send_permission_present():
    channel = _PermChannel(perms=_Perms(send_messages=True))
    assert can_send_reply(channel, bot_member=object()) is True


def test_can_send_reply_blocks_when_send_permission_missing():
    channel = _PermChannel(perms=_Perms(send_messages=False))
    assert can_send_reply(channel, bot_member=object()) is False


def test_can_send_reply_uses_thread_permission_for_threads(monkeypatch):
    thread_type = type("_ThreadChannel", (_PermChannel,), {})
    monkeypatch.setattr(discord, "Thread", thread_type)
    # In a thread, send_messages alone does not grant posting; the thread perm does.
    blocked = thread_type(perms=_Perms(send_messages=True, send_messages_in_threads=False))
    assert can_send_reply(blocked, bot_member=object()) is False
    allowed = thread_type(perms=_Perms(send_messages=False, send_messages_in_threads=True))
    assert can_send_reply(allowed, bot_member=object()) is True


def test_can_send_reply_fails_open_without_member_or_on_error():
    # No resolved bot member -> cannot evaluate -> fail open (never suppress a reply).
    assert can_send_reply(_PermChannel(perms=_Perms(send_messages=False)), bot_member=None) is True
    # permissions_for raising -> fail open.
    assert can_send_reply(_PermChannel(raises=True), bot_member=object()) is True


def test_bot_authored_text_invocation_is_ignored():
    message = _Message(author=_Author(id=456, bot=True), content="hey kimi help")

    assert _should(message) is False


def test_self_authored_messages_are_ignored_even_with_mention():
    bot_user = _BotUser(id=999, bot=True)
    message = _Message(author=bot_user, mentions=[bot_user])

    assert _should(message, bot_user=bot_user) is False


def test_at_everyone_mass_ping_without_bot_mention_is_ignored():
    # @everyone/@here must not trigger a turn unless the bot is actually in
    # message.mentions. discord.py's mentioned_in short-circuits on
    # mention_everyone; should_respond must not.
    bot_user = _BotUser(id=999, bot=True)
    message = _Message(
        author=_Author(id=123),
        mentions=[],
        mention_everyone=True,
    )

    assert _should(message, bot_user=bot_user) is False


def test_at_everyone_with_explicit_bot_mention_still_responds():
    bot_user = _BotUser(id=999, bot=True)
    message = _Message(
        author=_Author(id=123),
        mentions=[bot_user],
        mention_everyone=True,
    )

    assert _should(message, bot_user=bot_user) is True


def test_strip_mention_removes_text_invocation_prefix_with_prompt():
    bot_user = cast(discord.ClientUser, _BotUser(id=999, bot=True))

    assert (
        strip_mention(
            "hey kimi, troubleshoot the build",
            bot_user=bot_user,
            bot_name=DEFAULT_BOT_NAME,
        )
        == "troubleshoot the build"
    )
    assert (
        strip_mention(
            "Hi, Kimi! ping?",
            bot_user=bot_user,
            bot_name=DEFAULT_BOT_NAME,
        )
        == "ping?"
    )
    assert (
        strip_mention(
            "kimi help",
            bot_user=bot_user,
            bot_name=DEFAULT_BOT_NAME,
        )
        == "help"
    )


def test_strip_mention_keeps_bare_greeting_non_empty():
    bot_user = cast(discord.ClientUser, _BotUser(id=999, bot=True))

    assert (
        strip_mention(
            "hey kimi",
            bot_user=bot_user,
            bot_name=DEFAULT_BOT_NAME,
        )
        == "hey kimi"
    )
