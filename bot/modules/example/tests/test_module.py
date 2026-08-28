"""Behavior of the started module through every entry point, using only the API fakes."""

from __future__ import annotations

import asyncio

import pytest
from conftest import ALICE, BOB, GUILD, STAFF, Harness, ToolContext
from kimi_agent_module_api import TrustTier
from kimi_agent_module_api.contracts import ButtonSpec, UndeclaredDiscordAction, parse_custom_id
from kimi_agent_module_api.events import TOPIC_MEMBER_REMOVE, MemberRemoveEvent
from kimi_agent_module_api.testing import FakeInteraction

from community_agent_reference_module.module import (
    BUTTON_THANK_BACK,
    DIGEST_HANDLER,
    DIGEST_JOB_KEY,
    MODULE_NAME,
    SERVICE_NAME,
    SERVICE_VERSION,
    TOOL_GIVE,
    TOOL_LEADERBOARD,
    TOPIC_GIVEN,
    KudosBoardService,
    KudosGivenEvent,
)

# ----------------------------------------------------------------------
# start() wires every surface
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_registers_every_surface(started: Harness) -> None:
    assert started.scheduler.jobs[DIGEST_JOB_KEY].handler == DIGEST_HANDLER
    assert started.events.subscriptions == (TOPIC_MEMBER_REMOVE,)
    assert set(started.interactions.commands) == {"kudos.give", "kudos.top", "kudos.setup"}
    assert started.interactions.commands["kudos.setup"][0].min_tier == "staff"
    assert ("button", BUTTON_THANK_BACK) in started.interactions.components
    # The registry hands out a proxy; the typed get checks the provided object.
    started.services.get(SERVICE_NAME, SERVICE_VERSION, KudosBoardService)
    assert started.health.current is not None
    assert started.health.current.state == "healthy"
    assert started.health.current.metrics["guilds"] == 1.0


@pytest.mark.asyncio
async def test_close_releases_registrations(started: Harness) -> None:
    await started.module.close()

    assert started.interactions.commands == {}
    assert started.interactions.components == {}
    assert started.events.subscriptions == ()
    assert started.services.provided == {}


# ----------------------------------------------------------------------
# The LLM tools
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_give_tool_records_and_publishes(started: Harness, member_ctx: ToolContext) -> None:
    reply = await started.tool(
        TOOL_GIVE, {"user": f"<@{BOB}>", "reason": "  fixed the   bot "}, member_ctx
    )

    assert reply == f"Kudos to <@{BOB}>: fixed the bot"
    [event] = started.events.published
    assert event.topic == TOPIC_GIVEN
    assert event.payload == KudosGivenEvent(GUILD, ALICE, BOB, "fixed the bot", 1)


@pytest.mark.asyncio
async def test_give_tool_validates_input(started: Harness, member_ctx: ToolContext) -> None:
    assert "numeric" in await started.tool(TOOL_GIVE, {"user": "bob", "reason": "x"}, member_ctx)
    assert "yourself" in await started.tool(
        TOOL_GIVE, {"user": str(ALICE), "reason": "x"}, member_ctx
    )
    assert "Say what" in await started.tool(
        TOOL_GIVE, {"user": str(BOB), "reason": "  "}, member_ctx
    )
    assert "under 200" in await started.tool(
        TOOL_GIVE, {"user": str(BOB), "reason": "x" * 201}, member_ctx
    )


@pytest.mark.asyncio
async def test_daily_limit_is_a_rolling_window(started: Harness, member_ctx: ToolContext) -> None:
    args = {"user": str(BOB), "reason": "great"}
    assert (await started.tool(TOOL_GIVE, args, member_ctx)).startswith("Kudos")
    assert (await started.tool(TOOL_GIVE, args, member_ctx)).startswith("Kudos")
    assert "last 24 hours" in await started.tool(TOOL_GIVE, args, member_ctx)

    started.clock.now += 24 * 3600 + 1
    assert (await started.tool(TOOL_GIVE, args, member_ctx)).startswith("Kudos")


@pytest.mark.asyncio
async def test_guild_minimum_tier_is_enforced(started: Harness, member_ctx: ToolContext) -> None:
    started.guild_settings.set(GUILD, giver_min_tier="regular")

    reply = await started.tool(TOOL_GIVE, {"user": str(BOB), "reason": "x"}, member_ctx)
    assert "regular members and above" in reply

    regular = ToolContext(user_id=ALICE, trust_tier=TrustTier.REGULAR)
    assert (await started.tool(TOOL_GIVE, {"user": str(BOB), "reason": "x"}, regular)).startswith(
        "Kudos"
    )


@pytest.mark.asyncio
async def test_daily_limit_holds_under_concurrent_gives(
    started: Harness, member_ctx: ToolContext
) -> None:
    args = {"user": str(BOB), "reason": "race"}

    replies = await asyncio.gather(*(started.tool(TOOL_GIVE, args, member_ctx) for _ in range(6)))

    assert sum(reply.startswith("Kudos") for reply in replies) == 2


@pytest.mark.asyncio
async def test_leaderboard_tool_ranks_receivers(started: Harness, member_ctx: ToolContext) -> None:
    await started.tool(TOOL_GIVE, {"user": str(BOB), "reason": "a"}, member_ctx)
    await started.tool(TOOL_GIVE, {"user": str(STAFF), "reason": "b"}, member_ctx)
    bob = ToolContext(user_id=BOB)
    await started.tool(TOOL_GIVE, {"user": str(STAFF), "reason": "c"}, bob)

    board = await started.tool(TOOL_LEADERBOARD, {"days": 7}, member_ctx)

    assert board.splitlines()[1:] == [f"1. <@{STAFF}> — 2", f"2. <@{BOB}> — 1"]
    assert "Nobody" in await started.tool(
        TOOL_LEADERBOARD, {}, ToolContext(user_id=1, guild_id=999)
    )


# ----------------------------------------------------------------------
# Slash commands and the persistent button
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_give_command_replies_with_thank_back_button(started: Harness) -> None:
    _spec, handler = started.interactions.commands["kudos.give"]
    interaction = FakeInteraction(
        guild_id=GUILD, channel_id=555, user_id=ALICE, options={"member": BOB, "reason": "helped"}
    )

    await handler(interaction)

    response = interaction.last
    assert response.content == f"Kudos to <@{BOB}>: helped"
    [button] = response.components
    assert isinstance(button, ButtonSpec)
    assert button.key == BUTTON_THANK_BACK and button.parts == ("1",)


@pytest.mark.asyncio
async def test_thank_back_button_only_works_for_the_receiver(started: Harness) -> None:
    _spec, give = started.interactions.commands["kudos.give"]
    await give(
        FakeInteraction(
            guild_id=GUILD,
            channel_id=555,
            user_id=ALICE,
            options={"member": BOB, "reason": "helped"},
        )
    )
    custom_id = started.interactions.custom_id(BUTTON_THANK_BACK, "1")
    assert parse_custom_id(custom_id) == (MODULE_NAME, BUTTON_THANK_BACK, ("1",))
    button = started.interactions.components[("button", BUTTON_THANK_BACK)]

    stranger = FakeInteraction(guild_id=GUILD, channel_id=555, user_id=STAFF, custom_id=custom_id)
    await button(stranger)
    assert stranger.last.ephemeral and "Only the person thanked" in str(stranger.last.content)

    receiver = FakeInteraction(guild_id=GUILD, channel_id=555, user_id=BOB, custom_id=custom_id)
    await button(receiver)
    assert receiver.last.content == f"Kudos to <@{ALICE}>: thanks for the kudos!"

    started.guild_settings.set(GUILD, allow_thank_back=False)
    disabled = FakeInteraction(guild_id=GUILD, channel_id=555, user_id=BOB, custom_id=custom_id)
    await button(disabled)
    assert "turned off" in str(disabled.last.content)
    started.guild_settings.set(GUILD, allow_thank_back=True)

    unknown = FakeInteraction(
        guild_id=GUILD,
        channel_id=555,
        user_id=BOB,
        custom_id=started.interactions.custom_id(BUTTON_THANK_BACK, "404"),
    )
    await button(unknown)
    assert "no longer available" in str(unknown.last.content)


@pytest.mark.asyncio
async def test_thank_back_button_rejects_malformed_record_ids(started: Harness) -> None:
    button = started.interactions.components[("button", BUTTON_THANK_BACK)]

    for malformed_part in ("not-a-number", str(1 << 63)):
        interaction = FakeInteraction(
            guild_id=GUILD,
            channel_id=555,
            user_id=BOB,
            custom_id=started.interactions.custom_id(BUTTON_THANK_BACK, malformed_part),
        )
        await button(interaction)
        assert interaction.last.ephemeral
        assert "no longer available" in str(interaction.last.content)


@pytest.mark.asyncio
async def test_top_command_renders_an_embed(started: Harness, member_ctx: ToolContext) -> None:
    await started.tool(TOOL_GIVE, {"user": str(BOB), "reason": "a"}, member_ctx)
    _spec, top = started.interactions.commands["kudos.top"]
    interaction = FakeInteraction(
        guild_id=GUILD, channel_id=555, user_id=ALICE, options={"days": 7}
    )

    await top(interaction)

    embed = interaction.last.embed
    assert embed is not None and embed.title == "Kudos, last 7 day(s)"
    assert f"<@{BOB}> — 1" in str(embed.description)
    assert interaction.last.ephemeral is False


@pytest.mark.asyncio
async def test_setup_command_proposes_a_guild_document(started: Harness) -> None:
    _spec, setup = started.interactions.commands["kudos.setup"]
    interaction = FakeInteraction(
        guild_id=GUILD, channel_id=555, user_id=STAFF, options={"channel": 777}
    )

    await setup(interaction)

    [change] = started.proposals.changes
    assert change.target == f"guild:{GUILD}:{MODULE_NAME}"
    assert change.content == "---\ndigest_channel_id: 777\n---\n"
    assert change.actor.user_id == str(STAFF) and change.actor.guild_id == str(GUILD)
    assert change.expected_revision is not None
    assert interaction.last.ephemeral and change.proposal_id in str(interaction.last.content)


# ----------------------------------------------------------------------
# Scheduler, events, service
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_digest_posts_to_configured_channels(
    started: Harness, member_ctx: ToolContext
) -> None:
    await started.tool(TOOL_GIVE, {"user": str(BOB), "reason": "a"}, member_ctx)
    # A second guild without a digest channel is skipped silently.
    started.guild_settings.set(200)

    ran = await started.scheduler.run_due(now=1e12)

    assert ran == 1
    [call] = started.discord.calls_for("send_message")
    assert call.args[0] == 900
    assert f"<@{BOB}> — 1" in str(call.kwargs["embed"].description)
    assert started.health.keyed["digest"].state == "healthy"
    assert started.health.keyed["digest"].metrics["digest_posted"] == 1.0


@pytest.mark.asyncio
async def test_digest_survives_one_guild_failing(started: Harness, member_ctx: ToolContext) -> None:
    await started.tool(TOOL_GIVE, {"user": str(BOB), "reason": "a"}, member_ctx)
    # Undeclared actions raise; simulate a per-guild failure by narrowing the fake.
    started.discord.declared = frozenset()

    await started.scheduler.run_due(now=1e12)

    job = started.scheduler.jobs[DIGEST_JOB_KEY]
    assert job.last_error is None, "a guild failure must not fail the job"
    # The digest concern is keyed, so the module's own unkeyed report stays healthy
    # while the host shows the worst of the two.
    assert started.health.current is not None and started.health.current.state == "healthy"
    assert started.health.keyed["digest"].state == "degraded"
    assert started.health.keyed["digest"].metrics["digest_failures"] == 1.0


@pytest.mark.asyncio
async def test_member_remove_forgets_their_kudos(started: Harness, member_ctx: ToolContext) -> None:
    await started.tool(TOOL_GIVE, {"user": str(BOB), "reason": "a"}, member_ctx)

    handled = await started.events.deliver(
        TOPIC_MEMBER_REMOVE, MemberRemoveEvent(GUILD, BOB, roles_at_removal=())
    )

    assert handled == 1
    assert "Nobody" in await started.tool(TOOL_LEADERBOARD, {}, member_ctx)


@pytest.mark.asyncio
async def test_provided_service_reads_the_same_ledger(
    started: Harness, member_ctx: ToolContext
) -> None:
    await started.tool(TOOL_GIVE, {"user": str(BOB), "reason": "a"}, member_ctx)
    service = started.services.get(SERVICE_NAME, SERVICE_VERSION, KudosBoardService)

    board = await service.leaderboard(GUILD, days=1, limit=5)

    assert [(entry.user_id, entry.count) for entry in board] == [(BOB, 1)]


@pytest.mark.asyncio
async def test_undeclared_discord_action_is_caught_by_the_fake(started: Harness) -> None:
    with pytest.raises(UndeclaredDiscordAction):
        await started.discord.send_dm(ALICE, "hi")
