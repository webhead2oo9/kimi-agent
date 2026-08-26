from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any

import pytest

from app.proposals import ConfigProposalService, ProposalHost
from kimi_agent_module_api import ProposalActor, ProposalError
from kimi_agent_module_api.contracts import MessageRef, build_custom_id
from kimi_agent_module_api.testing import FakeInteraction, FakeInteractions
from storage.db import Database

GUILD = 123
OTHER_GUILD = 456
INVOKING_CHANNEL = 789
REVIEW_CHANNEL = 790
TARGET = f"guild:{GUILD}"
CONTENT = "---\nbot_active: true\n---\nGuild instructions.\n"


class _Poster:
    def __init__(self, guild_id: int = GUILD, *, fail: bool = False) -> None:
        self.guild_id = guild_id
        self.fail = fail
        self.calls: list[tuple[int, dict[str, Any]]] = []

    async def __call__(self, channel_id: int, **kwargs: Any) -> MessageRef:
        self.calls.append((channel_id, kwargs))
        if self.fail:
            raise RuntimeError("post failed")
        return MessageRef(self.guild_id, channel_id, 900 + len(self.calls))


class _HostState:
    def __init__(self, config_dir: Path, *, poster: _Poster | None = None) -> None:
        self.config_dir = config_dir
        self.poster = poster or _Poster()
        self.review_channel: str | None = None
        self.channels = {
            INVOKING_CHANNEL: GUILD,
            REVIEW_CHANNEL: GUILD,
            999: OTHER_GUILD,
        }
        self.refreshes: list[int] = []
        self.health = ""
        self.refresh_error: Exception | None = None

    async def channel_guild_id(self, channel_id: int) -> int | None:
        return self.channels.get(channel_id)

    async def refresh(self, guild_id: int) -> None:
        self.refreshes.append(guild_id)
        if self.refresh_error is not None:
            raise self.refresh_error

    def host(self) -> ProposalHost:
        return ProposalHost(
            config_dir=lambda: self.config_dir,
            review_channel_id=lambda _guild_id: self.review_channel,
            channel_guild_id=self.channel_guild_id,
            known_modules=lambda: ("config_admin", "moderation"),
            post_review=self.poster,
            on_applied=self.refresh,
            verify_guild=lambda _guild_id: self.health,
        )


def _actor(guild_id: int = GUILD, channel_id: int = INVOKING_CHANNEL) -> ProposalActor:
    return ProposalActor("42", "test", str(guild_id), str(channel_id))


async def _service(tmp_path: Path, state: _HostState | None = None):
    database = Database(tmp_path / "bot.db")
    await database.connect()
    host_state = state or _HostState(tmp_path / "config")
    service = ConfigProposalService(database, host_state.host())
    return database, host_state, service, service.view_for("config_admin")


@pytest.mark.asyncio
async def test_propose_persists_posts_and_uses_configured_channel(tmp_path: Path) -> None:
    database, state, service, view = await _service(tmp_path)
    try:
        state.review_channel = str(REVIEW_CHANNEL)
        ref = await view.propose(
            target=TARGET, content=CONTENT, summary="Enable this guild", actor=_actor()
        )

        assert ref.state == "pending"
        assert ref.message == MessageRef(GUILD, REVIEW_CHANNEL, 901)
        channel_id, kwargs = state.poster.calls[0]
        assert channel_id == REVIEW_CHANNEL
        assert [button.key for button in kwargs["components"]] == ["approve", "reject"]
        async with database.conn.execute(
            "SELECT module_name,base_exists,base_revision,content_revision "
            "FROM config_proposals WHERE proposal_id=?",
            (ref.proposal_id,),
        ) as cursor:
            row = await cursor.fetchone()
        assert tuple(row) == (
            "config_admin",
            0,
            hashlib.sha256(b"").hexdigest(),
            hashlib.sha256(CONTENT.encode()).hexdigest(),
        )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_snapshot_propose_and_get_are_guild_scoped(tmp_path: Path) -> None:
    database, _state, _service_impl, view = await _service(tmp_path)
    try:
        with pytest.raises(ProposalError, match="actor's guild"):
            await view.snapshot(TARGET, actor=_actor(OTHER_GUILD))
        with pytest.raises(ProposalError, match="actor's guild"):
            await view.propose(
                target=TARGET, content=CONTENT, summary="cross guild", actor=_actor(OTHER_GUILD)
            )
        ref = await view.propose(target=TARGET, content=CONTENT, summary="ok", actor=_actor())
        assert await view.get(ref.proposal_id, actor=_actor(OTHER_GUILD)) is None
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_review_channel_must_belong_to_actor_guild(tmp_path: Path) -> None:
    database, state, _service_impl, view = await _service(tmp_path)
    try:
        state.review_channel = "999"
        with pytest.raises(ProposalError, match="review channel"):
            await view.propose(target=TARGET, content=CONTENT, summary="bad route", actor=_actor())
        assert state.poster.calls == []
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_approve_writes_and_reject_leaves_file_absent(tmp_path: Path) -> None:
    database, _state, service, view = await _service(tmp_path)
    router = FakeInteractions("proposals")
    service.install(router)
    try:
        approved = await view.propose(
            target=TARGET, content=CONTENT, summary="apply", actor=_actor()
        )
        interaction = FakeInteraction(
            guild_id=GUILD,
            user_id=55,
            custom_id=build_custom_id("proposals", "approve", approved.proposal_id),
        )
        await router.components[("button", "approve")](interaction)
        path = tmp_path / "config" / "servers" / f"{GUILD}.md"
        assert path.read_text(encoding="utf-8") == CONTENT
        assert (await view.get(approved.proposal_id, actor=_actor())).state == "applied"  # type: ignore[union-attr]
        assert interaction.last.kind == "edit"
        assert interaction.last.components == ()

        path.unlink()
        rejected = await view.propose(
            target=TARGET, content=CONTENT, summary="reject", actor=_actor()
        )
        rejection = FakeInteraction(
            guild_id=GUILD,
            user_id=56,
            custom_id=build_custom_id("proposals", "reject", rejected.proposal_id),
        )
        await router.components[("button", "reject")](rejection)
        assert not path.exists()
        assert (await view.get(rejected.proposal_id, actor=_actor())).state == "rejected"  # type: ignore[union-attr]
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_operator_edit_after_proposal_is_not_clobbered_without_expected_revision(
    tmp_path: Path,
) -> None:
    database, _state, service, view = await _service(tmp_path)
    router = FakeInteractions("proposals")
    service.install(router)
    try:
        ref = await view.propose(target=TARGET, content=CONTENT, summary="stale", actor=_actor())
        path = tmp_path / "config" / "servers" / f"{GUILD}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        operator_content = "---\nbot_active: false\n---\nOperator edit.\n"
        path.write_text(operator_content, encoding="utf-8")
        interaction = FakeInteraction(
            guild_id=GUILD,
            custom_id=build_custom_id("proposals", "approve", ref.proposal_id),
        )
        await router.components[("button", "approve")](interaction)
        decided = await view.get(ref.proposal_id, actor=_actor())
        assert decided is not None and decided.state == "rejected"
        assert decided.decision_reason == "configuration changed since proposal"
        assert path.read_text(encoding="utf-8") == operator_content
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_invalid_health_rolls_back_and_leaves_pending(tmp_path: Path) -> None:
    database, state, service, view = await _service(tmp_path)
    router = FakeInteractions("proposals")
    service.install(router)
    try:
        path = tmp_path / "config" / "servers" / f"{GUILD}.md"
        path.parent.mkdir(parents=True)
        baseline = "---\nbot_active: true\n---\nBaseline.\n"
        path.write_text(baseline, encoding="utf-8")
        ref = await view.propose(
            target=TARGET, content=CONTENT, summary="bad health", actor=_actor()
        )
        state.health = "module settings invalid"
        interaction = FakeInteraction(
            guild_id=GUILD,
            custom_id=build_custom_id("proposals", "approve", ref.proposal_id),
        )
        await router.components[("button", "approve")](interaction)
        assert path.read_text(encoding="utf-8") == baseline
        pending = await view.get(ref.proposal_id, actor=_actor())
        assert pending is not None and pending.state == "pending"
        assert state.refreshes == [GUILD, GUILD]
        assert interaction.last.ephemeral
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_interrupted_write_is_recovered_by_second_service(tmp_path: Path) -> None:
    database, state, service, view = await _service(tmp_path)
    try:
        ref = await view.propose(target=TARGET, content=CONTENT, summary="recover", actor=_actor())
        path = tmp_path / "config" / "servers" / f"{GUILD}.md"
        path.parent.mkdir(parents=True)
        path.write_bytes(CONTENT.encode("utf-8"))

        restarted = ConfigProposalService(database, state.host())
        router = FakeInteractions("proposals")
        restarted.install(router)
        interaction = FakeInteraction(
            guild_id=GUILD,
            custom_id=build_custom_id("proposals", "approve", ref.proposal_id),
        )
        await router.components[("button", "approve")](interaction)
        recovered = await restarted.view_for("config_admin").get(ref.proposal_id, actor=_actor())
        assert recovered is not None and recovered.state == "applied"
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_approve_reject_race_keeps_file_and_state_consistent(tmp_path: Path) -> None:
    database, _state, service, view = await _service(tmp_path)
    router = FakeInteractions("proposals")
    service.install(router)
    try:
        ref = await view.propose(target=TARGET, content=CONTENT, summary="race", actor=_actor())
        approve = FakeInteraction(
            guild_id=GUILD,
            custom_id=build_custom_id("proposals", "approve", ref.proposal_id),
        )
        reject = FakeInteraction(
            guild_id=GUILD,
            custom_id=build_custom_id("proposals", "reject", ref.proposal_id),
        )
        await asyncio.gather(
            router.components[("button", "approve")](approve),
            router.components[("button", "reject")](reject),
        )
        decided = await view.get(ref.proposal_id, actor=_actor())
        path = tmp_path / "config" / "servers" / f"{GUILD}.md"
        assert decided is not None
        assert path.exists() is (decided.state == "applied")
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_validation_failed_post_and_staff_registration(tmp_path: Path) -> None:
    state = _HostState(tmp_path / "config", poster=_Poster(fail=True))
    database, _state, service, view = await _service(tmp_path, state)
    router = FakeInteractions("proposals")
    service.install(router)
    try:
        assert router.component_min_tiers == {
            ("button", "approve"): "staff",
            ("button", "reject"): "staff",
        }
        with pytest.raises(ProposalError, match="proposal target"):
            await view.propose(target="settings", content=CONTENT, summary="no", actor=_actor())
        with pytest.raises(ProposalError, match="bot_active"):
            await view.propose(
                target=TARGET, content="plain body", summary="invalid", actor=_actor()
            )
        with pytest.raises(ProposalError, match="posting"):
            await view.propose(target=TARGET, content=CONTENT, summary="post", actor=_actor())
        async with database.conn.execute("SELECT COUNT(*) FROM config_proposals") as cursor:
            assert (await cursor.fetchone())[0] == 0
    finally:
        await database.close()
