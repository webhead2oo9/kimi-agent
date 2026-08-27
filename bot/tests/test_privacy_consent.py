from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import discord
import pytest

from app.consent import PrivacyConsentGate, PrivacyConsentView
from storage.db import Database
from storage.preferences import PreferenceStore


# --- PreferenceStore consent column ------------------------------------------


@pytest.mark.asyncio
async def test_memory_defaults_enabled_then_can_opt_out_and_reenable(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    store = PreferenceStore(db)
    try:
        assert await store.is_memory_enabled("123") is True

        assert await store.set_memory_enabled("123", False) is True
        assert await store.is_memory_enabled("123") is False

        assert await store.set_memory_enabled("123", True) is True
        assert await store.is_memory_enabled("123") is True
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_consent_defaults_false_and_toggles(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    store = PreferenceStore(db)
    try:
        assert await store.has_consented("123") is False

        assert await store.set_consent("123", True) is True
        assert await store.has_consented("123") is True
        # Idempotent: re-setting the same value reports no change.
        assert await store.set_consent("123", True) is False

        assert await store.set_consent("123", False) is True
        assert await store.has_consented("123") is False
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_consent_and_memory_coexist_on_same_row(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    store = PreferenceStore(db)
    try:
        # Set each flag in turn; neither UPSERT may clobber the other's column.
        await store.set_memory_enabled("123", False)
        await store.set_consent("123", True)

        assert await store.is_memory_enabled("123") is False
        assert await store.has_consented("123") is True

        # Single row, not two competing states.
        async with db.conn.execute(
            "SELECT COUNT(*) FROM user_preferences WHERE user_id = ?", ("123",)
        ) as cur:
            row = await cur.fetchone()
        assert row is not None and row[0] == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_persona_round_trips_and_coexists_with_preferences(tmp_path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    store = PreferenceStore(db)
    try:
        assert await store.get_persona("123") == ""

        await store.set_memory_enabled("123", False)
        await store.set_consent("123", True)
        assert await store.set_persona("123", "Roleplay as a friendly space mechanic.") is True
        assert await store.get_persona("123") == "Roleplay as a friendly space mechanic."
        assert await store.set_persona("123", "Roleplay as a friendly space mechanic.") is False

        assert await store.is_memory_enabled("123") is False
        assert await store.has_consented("123") is True
        assert await store.clear_persona("123") is True
        assert await store.clear_persona("123") is False
        assert await store.get_persona("123") == ""
    finally:
        await db.close()


# --- Gate decision logic (no live Discord) -----------------------------------


class FakeStore:
    def __init__(self, *, consented: bool = False) -> None:
        self._consented = consented
        self.set_calls: list[tuple[str, bool]] = []

    async def has_consented(self, user_id: str) -> bool:
        return self._consented

    async def set_consent(self, user_id: str, granted: bool) -> bool:
        self.set_calls.append((user_id, granted))
        self._consented = granted
        return True


class FakeResponse:
    def __init__(self) -> None:
        self.edited: dict | None = None
        self.sent: list[tuple] = []

    async def edit_message(self, **kwargs) -> None:
        self.edited = kwargs

    async def send_message(self, content=None, **kwargs) -> None:
        self.sent.append((content, kwargs))


class FakeInteraction:
    def __init__(self, user_id: int) -> None:
        self.user = SimpleNamespace(id=user_id)
        self.response = FakeResponse()


class FakeMessage:
    def __init__(self, author_id: int) -> None:
        self.author = SimpleNamespace(id=author_id)
        self.reply_calls: list[dict] = []

    async def reply(self, **kwargs):
        self.reply_calls.append(kwargs)

        async def _edit(**_kw):
            return None

        return SimpleNamespace(edit=_edit)


def _make_gate(store, redispatch, *, enabled: bool = True) -> PrivacyConsentGate:
    return PrivacyConsentGate(
        enabled=enabled,
        title="Title",
        text="Body",
        timeout=60.0,
        preference_store=store,
        redispatch=redispatch,
    )


async def _noop_redispatch(message) -> None:  # pragma: no cover - default unused arg
    return None


@pytest.mark.asyncio
async def test_maybe_prompt_skips_when_disabled() -> None:
    msg = FakeMessage(123)
    gate = _make_gate(FakeStore(), _noop_redispatch, enabled=False)

    assert await gate.maybe_prompt(cast(discord.Message, msg)) is False
    assert msg.reply_calls == []


@pytest.mark.asyncio
async def test_maybe_prompt_skips_when_already_consented() -> None:
    msg = FakeMessage(123)
    gate = _make_gate(FakeStore(consented=True), _noop_redispatch)

    assert await gate.maybe_prompt(cast(discord.Message, msg)) is False
    assert msg.reply_calls == []


@pytest.mark.asyncio
async def test_maybe_prompt_posts_once_and_dedupes_while_pending() -> None:
    msg = FakeMessage(123)
    gate = _make_gate(FakeStore(consented=False), _noop_redispatch)

    assert await gate.maybe_prompt(cast(discord.Message, msg)) is True
    assert len(msg.reply_calls) == 1
    assert "123" in gate._pending
    # An embed + interactive view rode the reply.
    assert msg.reply_calls[0].get("embed") is not None
    assert msg.reply_calls[0].get("view") is not None

    # A second mention while the prompt is open must not post another notice.
    assert await gate.maybe_prompt(cast(discord.Message, msg)) is True
    assert len(msg.reply_calls) == 1


@pytest.mark.asyncio
async def test_accept_records_consent_and_redispatches() -> None:
    msg = FakeMessage(123)
    store = FakeStore(consented=False)
    redispatched: list = []

    async def redispatch(message) -> None:
        redispatched.append(message)

    gate = _make_gate(store, redispatch)
    gate._pending.add("123")
    interaction = FakeInteraction(123)

    await gate._accept(cast(discord.Message, msg), "123", cast(discord.Interaction, interaction))

    assert store.set_calls == [("123", True)]
    assert redispatched == [msg]
    assert interaction.response.edited is not None
    assert interaction.response.edited.get("view") is None
    assert "123" not in gate._pending


@pytest.mark.asyncio
async def test_accept_releases_pending_when_consent_write_fails() -> None:
    """A failed consent write must not leave the user stuck in _pending forever."""

    class FailingStore(FakeStore):
        async def set_consent(self, user_id: str, granted: bool) -> bool:
            raise RuntimeError("db down")

    msg = FakeMessage(123)
    redispatched: list = []

    async def redispatch(message) -> None:  # pragma: no cover - must not run
        redispatched.append(message)

    gate = _make_gate(FailingStore(consented=False), redispatch)
    gate._pending.add("123")
    interaction = FakeInteraction(123)

    with pytest.raises(RuntimeError):
        await gate._accept(
            cast(discord.Message, msg), "123", cast(discord.Interaction, interaction)
        )

    # The reservation is released, so the gate reappears on the next mention
    # instead of silently dropping every future message from this user.
    assert "123" not in gate._pending
    assert redispatched == []


@pytest.mark.asyncio
async def test_accept_redispatches_even_when_prompt_edit_fails() -> None:
    """An expired interaction token must not swallow the original message."""

    class ExpiredResponse(FakeResponse):
        async def edit_message(self, **kwargs) -> None:
            fake_http_response = SimpleNamespace(status=404, reason="Not Found")
            raise discord.NotFound(fake_http_response, "unknown interaction")  # type: ignore[arg-type]

    msg = FakeMessage(123)
    store = FakeStore(consented=False)
    redispatched: list = []

    async def redispatch(message) -> None:
        redispatched.append(message)

    gate = _make_gate(store, redispatch)
    gate._pending.add("123")
    interaction = FakeInteraction(123)
    interaction.response = ExpiredResponse()

    await gate._accept(cast(discord.Message, msg), "123", cast(discord.Interaction, interaction))

    assert store.set_calls == [("123", True)]
    assert redispatched == [msg]
    assert "123" not in gate._pending


@pytest.mark.asyncio
async def test_decline_does_not_consent_or_redispatch() -> None:
    store = FakeStore(consented=False)
    redispatched: list = []

    async def redispatch(message) -> None:  # pragma: no cover - must not run
        redispatched.append(message)

    gate = _make_gate(store, redispatch)
    gate._pending.add("123")
    interaction = FakeInteraction(123)

    await gate._decline("123", cast(discord.Interaction, interaction))

    assert store.set_calls == []
    assert redispatched == []
    assert interaction.response.edited is not None
    assert "123" not in gate._pending


@pytest.mark.asyncio
async def test_view_rejects_non_author_click() -> None:
    async def _noop(*_args) -> None:
        return None

    view = PrivacyConsentView(
        author_id=123,
        on_accept=_noop,
        on_decline=_noop,
        on_close=_noop,
        timeout=60.0,
    )

    stranger = FakeInteraction(999)
    assert await view._is_author(cast(discord.Interaction, stranger)) is False
    assert stranger.response.sent  # ephemeral rejection was sent

    owner = FakeInteraction(123)
    assert await view._is_author(cast(discord.Interaction, owner)) is True
    assert owner.response.sent == []


@pytest.mark.asyncio
async def test_view_atomically_claims_one_concurrent_decision() -> None:
    accept_started = asyncio.Event()
    release_accept = asyncio.Event()
    decisions: list[str] = []

    async def on_accept(_interaction: discord.Interaction) -> None:
        decisions.append("accept")
        accept_started.set()
        await release_accept.wait()

    async def on_decline(_interaction: discord.Interaction) -> None:
        decisions.append("decline")

    async def on_close() -> None:
        decisions.append("timeout")

    view = PrivacyConsentView(
        author_id=123,
        on_accept=on_accept,
        on_decline=on_decline,
        on_close=on_close,
        timeout=60.0,
    )
    accept_button = cast(
        Any,
        next(child for child in view.children if getattr(child, "label", None) == "Accept"),
    )
    decline_button = cast(
        Any,
        next(child for child in view.children if getattr(child, "label", None) == "Decline"),
    )
    accept_interaction = FakeInteraction(123)
    decline_interaction = FakeInteraction(123)

    accepting = asyncio.create_task(
        accept_button.callback(cast(discord.Interaction, accept_interaction))
    )
    await accept_started.wait()
    await decline_button.callback(cast(discord.Interaction, decline_interaction))
    release_accept.set()
    await accepting

    assert decisions == ["accept"]
    assert decline_interaction.response.sent == [
        ("This privacy choice has already been handled.", {"ephemeral": True})
    ]


@pytest.mark.asyncio
async def test_view_rechecks_readiness_when_click_waits_to_claim() -> None:
    available = True
    decisions: list[str] = []

    async def on_accept(_interaction: discord.Interaction) -> None:
        decisions.append("accept")

    async def on_decline(_interaction: discord.Interaction) -> None:
        decisions.append("decline")

    async def on_close() -> None:
        decisions.append("timeout")

    view = PrivacyConsentView(
        author_id=123,
        on_accept=on_accept,
        on_decline=on_decline,
        on_close=on_close,
        timeout=60.0,
        is_available=lambda: available,
    )
    accept_button = cast(
        Any,
        next(child for child in view.children if getattr(child, "label", None) == "Accept"),
    )
    interaction = FakeInteraction(123)

    await view._decision_lock.acquire()
    clicking = asyncio.create_task(accept_button.callback(cast(discord.Interaction, interaction)))
    await asyncio.sleep(0)
    available = False
    view._decision_lock.release()
    await clicking

    assert decisions == []
    assert view._resolved is False
    assert interaction.response.sent == [
        ("This privacy prompt is no longer available.", {"ephemeral": True})
    ]


@pytest.mark.asyncio
async def test_view_timeout_still_cleans_up_while_interactions_unavailable() -> None:
    closed = 0

    async def noop_interaction(_interaction: discord.Interaction) -> None:
        return None

    async def on_close() -> None:
        nonlocal closed
        closed += 1

    view = PrivacyConsentView(
        author_id=123,
        on_accept=noop_interaction,
        on_decline=noop_interaction,
        on_close=on_close,
        timeout=60.0,
        is_available=lambda: False,
    )

    await view.on_timeout()

    assert closed == 1
    assert view._resolved is True
