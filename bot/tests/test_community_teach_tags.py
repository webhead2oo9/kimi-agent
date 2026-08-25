from __future__ import annotations

import json

import pytest

import tools.community as community
from tools.registry import MessageContext
from trust.tiers import TrustTier


class _CaptureClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def create_bank(self, *, bank_id, name, **kwargs):
        return True

    async def retain(self, *, bank_id, content, context="", tags=None, retain_async=True, **kwargs):
        self.calls.append(
            {"bank_id": bank_id, "content": content, "context": context, "tags": tags, **kwargs}
        )
        return True


def _ctx() -> MessageContext:
    return MessageContext(
        user_id="42",
        user_name="StaffPerson",
        guild_id="777",
        channel_id="c1",
        thread_id=None,
        trust_tier=TrustTier.STAFF,
    )


@pytest.mark.asyncio
async def test_teach_tags_are_taught_not_verified_external() -> None:
    client = _CaptureClient()
    community._memory = client  # type: ignore[assignment]
    out = await community._teach(
        {"content": "The build server lives on port 8080.", "topic": "how-to"},
        _ctx(),
    )
    parsed = json.loads(out)
    assert "Learned and stored" in parsed.get("result", "")
    assert len(client.calls) == 1
    assert client.calls[0]["bank_id"] == "community:777"
    tags = client.calls[0]["tags"]
    assert "source:taught" in tags
    assert "confidence:high" in tags
    assert "scope:public" in tags
    assert "topic:how-to" in tags
    assert "taught_by:42" in tags
    assert not any(t.startswith("verified:") for t in tags)
