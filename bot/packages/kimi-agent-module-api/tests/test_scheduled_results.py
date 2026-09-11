"""Exercise the public subscriber contract without importing a host."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace

import pytest

from kimi_agent_module_api import (
    ModulePermissions,
    ScheduledResult,
    ScheduledResultAccessError,
    ScheduledResultAttachment,
    ScheduledResultMessage,
)
from kimi_agent_module_api.contracts import MessageRef, ModuleContractError, validate_permissions
from kimi_agent_module_api.testing import FakeScheduledResults


def result():
    return ScheduledResult(
        notification_id="notification",
        subscription="reports",
        task_id="task",
        run_id="run",
        revision=3,
        guild_id=100,
        owner_id=10,
        published_at=1000,
        expires_at=2000,
        messages=(
            ScheduledResultMessage(
                MessageRef(100, 300, 900),
                "Published",
                (ScheduledResultAttachment("file", "result.txt", 3),),
            ),
        ),
    )


def test_result_is_immutable_and_permissions_must_be_named():
    with pytest.raises(FrozenInstanceError):
        result().owner_id = 99
    validate_permissions("reports", ModulePermissions(scheduled_results=("reports",)))
    for names in (("Upper Case",), ("reports", "reports"), ("",)):
        with pytest.raises(ModuleContractError):
            validate_permissions("reports", ModulePermissions(scheduled_results=names))


@pytest.mark.asyncio
async def test_fake_retry_acknowledgement_preview_and_file_lifetime():
    fake = FakeScheduledResults(("reports",))
    attempts, readers = [], []

    async def handler(notification, files):
        attempts.append(notification.notification_id)
        readers.append(files)
        assert await files.read("file") == b"abc"
        with pytest.raises(ScheduledResultAccessError):
            await files.read("not published")
        with pytest.raises(ScheduledResultAccessError):
            await files.read("file", max_bytes=2)
        if len(attempts) == 1:
            raise RuntimeError("retry me")

    await fake.subscribe("reports", guild_id=100, handler=handler)
    await fake.deliver(result(), preview=True)
    assert attempts == []
    files = {"file": b"abc", "not published": b"private"}
    with pytest.raises(RuntimeError, match="retry me"):
        await fake.deliver(result(), files=files)
    assert not fake.acknowledged
    await fake.deliver(result(), files=files)
    await fake.deliver(result(), files=files)
    assert attempts == ["notification", "notification"]
    assert fake.acknowledged == {"notification"}
    for reader in readers:
        with pytest.raises(ScheduledResultAccessError):
            await reader.read("file")


@pytest.mark.asyncio
async def test_fake_permission_and_guild_scope():
    fake = FakeScheduledResults(("reports",), is_guild_active=lambda guild: guild == 100)

    async def handler(_result, _files):
        pass

    with pytest.raises(ScheduledResultAccessError):
        await fake.subscribe("other", guild_id=100, handler=handler)
    with pytest.raises(ModuleContractError):
        await fake.subscribe("reports", guild_id=0, handler=handler)
    await fake.subscribe("reports", guild_id=200, handler=handler)
    with pytest.raises(ScheduledResultAccessError):
        await fake.deliver(replace(result(), guild_id=200))
    await fake.subscribe("reports", guild_id=100, handler=handler)
    await fake.unsubscribe("reports", guild_id=100)
    with pytest.raises(ScheduledResultAccessError):
        await fake.deliver(result())
