"""The opt-in result subscriber uses only public SDK ports and idempotent SQL."""

from __future__ import annotations

from dataclasses import replace

import pytest
from conftest import GUILD, Harness
from kimi_agent_module_api import ModuleCapabilities, ScheduledResult
from kimi_agent_module_api.testing import FakeScheduledResults


@pytest.mark.asyncio
async def test_result_subscriber_is_opt_in_and_repeated_id_is_counted_once(
    started: Harness,
) -> None:
    fake = started.ctx.scheduled_results
    assert isinstance(fake, FakeScheduledResults)
    assert not fake.handlers
    await started.module.close()
    started.module._settings.result_guild_id = GUILD
    ctx = replace(
        started.ctx,
        capabilities=ModuleCapabilities(
            started.ctx.capabilities.available | {"scheduled_results.v1"},
            False,
            False,
        ),
    )
    await started.module.start(ctx)
    notification = ScheduledResult(
        notification_id="stable-id",
        subscription="published_tasks",
        task_id="task",
        run_id="run",
        revision=1,
        guild_id=GUILD,
        owner_id=2,
        published_at=started.clock(),
        expires_at=started.clock() + 86400,
        messages=(),
    )
    await fake.deliver(notification)
    # Model losing the host ACK after the module's transaction committed.
    fake.acknowledged.clear()
    await fake.deliver(notification)
    assert started.health.current is not None
    assert started.health.keyed["published_tasks"].metrics["published_task_results"] == 1
    table = ctx.storage.table("result_receipts")
    async with ctx.storage.connection.execute(f"SELECT * FROM {table}") as cursor:
        rows = await cursor.fetchall()
        assert len(rows) == 1
        assert tuple(rows[0]) == ("stable-id", notification.expires_at)
    started.clock.now += 2 * 86400
    await fake.deliver(
        replace(notification, notification_id="next-id", expires_at=started.clock() + 86400)
    )
    assert started.health.keyed["published_tasks"].metrics["published_task_results"] == 1
