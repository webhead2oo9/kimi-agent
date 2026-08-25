from __future__ import annotations

import pytest

from memory.banks import (
    SKILLS_BANK,
    community_bank_id,
    ensure_community_bank,
    ensure_global_banks,
    forget_initialized_bank,
)


class FakeMemoryClient:
    def __init__(self, results: dict[str, bool]) -> None:
        self.results = results
        self.created: list[str] = []

    async def create_bank(
        self,
        *,
        bank_id: str,
        name: str,
        reflect_mission: str = "",
        retain_mission: str = "",
        retain_extraction_mode: str = "",
        observations_mission: str = "",
        disposition: dict | None = None,
    ) -> bool:
        self.created.append(bank_id)
        return self.results.get(bank_id, True)


@pytest.mark.asyncio
async def test_ensure_global_banks_creates_only_skills_bank() -> None:
    forget_initialized_bank(SKILLS_BANK)
    client = FakeMemoryClient({SKILLS_BANK: True})

    assert await ensure_global_banks(client) is True  # type: ignore[arg-type]
    assert client.created == [SKILLS_BANK]

    forget_initialized_bank(SKILLS_BANK)


@pytest.mark.asyncio
async def test_ensure_global_banks_returns_false_on_create_failure() -> None:
    forget_initialized_bank(SKILLS_BANK)
    client = FakeMemoryClient({SKILLS_BANK: False})

    assert await ensure_global_banks(client) is False  # type: ignore[arg-type]
    assert client.created == [SKILLS_BANK]

    forget_initialized_bank(SKILLS_BANK)


def test_community_bank_id_resolution() -> None:
    assert community_bank_id("111") == "community:111"
    assert community_bank_id("222") == "community:222"
    assert community_bank_id(None) is None
    assert community_bank_id("") is None


@pytest.mark.asyncio
async def test_ensure_community_bank_lazy_create() -> None:
    forget_initialized_bank("community:777")
    client = FakeMemoryClient({"community:777": True})

    assert await ensure_community_bank(client, "777") == "community:777"  # type: ignore[arg-type]
    assert client.created == ["community:777"]
    # Cached on second call, so no re-creation.
    assert await ensure_community_bank(client, "777") == "community:777"  # type: ignore[arg-type]
    assert client.created == ["community:777"]

    forget_initialized_bank("community:777")


@pytest.mark.asyncio
async def test_ensure_community_bank_no_guild() -> None:
    client = FakeMemoryClient({})
    assert await ensure_community_bank(client, None) is None  # type: ignore[arg-type]
    assert client.created == []


@pytest.mark.asyncio
async def test_ensure_community_bank_create_failure() -> None:
    forget_initialized_bank("community:888")
    client = FakeMemoryClient({"community:888": False})

    assert await ensure_community_bank(client, "888") is None  # type: ignore[arg-type]

    forget_initialized_bank("community:888")
