"""The SDK's protocol fakes work without importing host implementations."""

from __future__ import annotations

import pytest

from kimi_agent_module_api.contracts import (
    Backoff,
    Event,
    JobRun,
    MessageRef,
    MigrationContext,
    ServiceUnavailable,
    UndeclaredDiscordAction,
)
from kimi_agent_module_api.testing import (
    FakeDiscordActions,
    FakeEvents,
    FakeInteraction,
    FakeScheduler,
    FakeServiceRegistry,
    MemoryStorage,
)


@pytest.mark.asyncio
async def test_fake_events_deliver_only_matching_subscriptions() -> None:
    events = FakeEvents("demo")
    seen: list[str] = []

    async def handler(event: Event) -> None:
        seen.append(event.topic)

    registration = events.subscribe("discord.*", handler)
    assert await events.deliver("discord.message", {"id": 1}) == 1
    registration.close()
    assert await events.deliver("discord.message_edit", {"id": 1}) == 0
    assert seen == ["discord.message"]


@pytest.mark.asyncio
async def test_fake_scheduler_retries_then_settles_jobs() -> None:
    scheduler = FakeScheduler()
    attempts: list[int] = []

    async def flaky(run: JobRun) -> None:
        attempts.append(run.attempt)
        if run.attempt == 1:
            raise RuntimeError("retry")

    scheduler.register("work", flaky)
    await scheduler.run_every(
        "job",
        60,
        "work",
        backoff=Backoff(base_seconds=5, max_seconds=10, multiplier=2),
    )

    assert await scheduler.run_due(0) == 1
    assert scheduler.jobs["job"].run_at == 5
    assert await scheduler.run_due(5) == 1
    assert scheduler.jobs["job"].run_at == 65
    assert attempts == [1, 2]


@pytest.mark.asyncio
async def test_fake_discord_actions_enforce_declared_permissions() -> None:
    actions = FakeDiscordActions("demo", frozenset({"send_message"}))
    ref = await actions.send_message(42, "hello")

    assert isinstance(ref, MessageRef)
    assert actions.calls_for("send_message")[0].args[:2] == (42, "hello")
    with pytest.raises(UndeclaredDiscordAction):
        await actions.fetch_message(ref)


@pytest.mark.asyncio
async def test_fake_interaction_records_responses() -> None:
    interaction = FakeInteraction(module_name="demo")
    await interaction.respond("done", ephemeral=True)
    await interaction.follow_up("more")

    assert interaction.responses[0].content == "done"
    assert interaction.responses[0].ephemeral is True
    assert interaction.last.content == "more"


def test_fake_service_proxy_closes_with_its_registration() -> None:
    class Board:
        def answer(self) -> int:
            return 42

    registry = FakeServiceRegistry()
    registration = registry.provide("records.board", 1, Board())
    proxy = registry.get("records.board", 1, Board)

    assert proxy.answer() == 42
    registration.close()
    with pytest.raises(ServiceUnavailable):
        proxy.answer()


@pytest.mark.asyncio
async def test_memory_storage_runs_scoped_migrations_and_transactions() -> None:
    async def create(ctx: MigrationContext) -> None:
        await ctx.connection.execute(f"CREATE TABLE {ctx.table('rows')} (value INTEGER)")

    async with MemoryStorage.open("my-module") as storage:
        assert storage.table("rows") == '"my_module_rows"'
        await storage.migrate((("001", create),))
        async with storage.write_transaction() as connection:
            await connection.execute('INSERT INTO "my_module_rows" (value) VALUES (7)')
        cursor = await storage.connection.execute('SELECT value FROM "my_module_rows"')
        assert await cursor.fetchone() == (7,)
