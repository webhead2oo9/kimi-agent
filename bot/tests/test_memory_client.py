import asyncio
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from memory import banks
from memory.client import MemoryBackendError, MemoryClient


class FakeHindsight:
    def __init__(self) -> None:
        self.create_bank_calls: list[dict] = []
        self.config_calls: list[tuple[str, dict, float]] = []
        self.banks = _FakeBanks(self.config_calls)

    def create_bank(self, **kwargs: object) -> None:
        raise AssertionError("sync create_bank should not be used in async code")

    async def acreate_bank(self, **kwargs: object) -> None:
        self.create_bank_calls.append(kwargs)


class _FakeBanks:
    def __init__(self, calls: list[tuple[str, dict, float]]) -> None:
        self._calls = calls

    async def update_bank_config(
        self,
        *,
        bank_id: str,
        bank_config_update: Any,
        _request_timeout: float,
    ) -> dict:
        self._calls.append((bank_id, bank_config_update.updates, _request_timeout))
        return {"bank_id": bank_id}


class FakeRecallHindsight:
    def __init__(self) -> None:
        self.recall_calls: list[dict] = []

    async def arecall(self, **kwargs: object) -> object:
        self.recall_calls.append(kwargs)
        result = type(
            "RecallResult",
            (),
            {
                "results": [
                    type(
                        "Memory",
                        (),
                        {
                            "text": "webhead uses a Quest 3.",
                            "type": "world",
                            "tags": ["scope:global"],
                        },
                    )()
                ]
            },
        )
        return result()


class FakeRetainHindsight:
    def __init__(self, *, success: bool = True, var_async: bool = False) -> None:
        self.retain_calls: list[dict] = []
        self.success = success
        self.var_async = var_async

    async def aretain(self, **kwargs: object) -> object:
        self.retain_calls.append(kwargs)
        return SimpleNamespace(success=self.success, var_async=self.var_async)


class RecordingUserBankState:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.marked: list[str] = []

    async def mark_may_exist(self, user_id: str) -> None:
        if self.fail:
            raise RuntimeError("database unavailable")
        self.marked.append(user_id)


class FakeMemoryClient:
    def __init__(self, created: bool) -> None:
        self.created = created
        self.calls: list[dict] = []

    async def create_bank(self, **kwargs: object) -> bool:
        self.calls.append(kwargs)
        return self.created


def _memory_client(
    hindsight: object,
    *,
    user_bank_state_store: RecordingUserBankState | None = None,
) -> MemoryClient:
    """Build a fully initialized wrapper without constructing the real SDK client."""

    client = object.__new__(MemoryClient)
    client._client = cast(Any, hindsight)
    client._user_bank_state_store = user_bank_state_store
    return client


def test_create_bank_creates_shell_then_configures_via_config_patch() -> None:
    fake_hindsight = FakeHindsight()
    client = _memory_client(fake_hindsight)

    created = asyncio.run(
        client.create_bank(
            bank_id="bot-skills",
            name="Bot Skills",
            reflect_mission="Store procedural knowledge.",
            retain_mission="Retain skill definitions.",
            retain_extraction_mode="concise",
            disposition={"skepticism": 4, "literalism": 5, "empathy": 1},
        )
    )

    assert created is True
    # The create PUT only registers the shell; mission/disposition land via /config.
    assert fake_hindsight.create_bank_calls == [{"bank_id": "bot-skills", "name": "Bot Skills"}]
    assert fake_hindsight.config_calls == [
        (
            "bot-skills",
            {
                "reflect_mission": "Store procedural knowledge.",
                "retain_mission": "Retain skill definitions.",
                "retain_extraction_mode": "concise",
                "disposition_skepticism": 4,
                "disposition_literalism": 5,
                "disposition_empathy": 1,
            },
            120.0,
        )
    ]


def test_public_banks_api_preserves_host_auth_body_and_timeout(monkeypatch) -> None:
    client = MemoryClient("https://memory.example/base/", api_key="test-key")
    api_client = client._client.banks.api_client

    class _Response:
        async def read(self) -> None:
            return None

    call_api = AsyncMock(return_value=_Response())
    monkeypatch.setattr(api_client, "call_api", call_api)
    monkeypatch.setattr(
        api_client,
        "response_deserialize",
        lambda **_kwargs: SimpleNamespace(data={"bank_id": "bot-skills"}),
    )

    try:
        updated = asyncio.run(
            client.update_bank_config(
                "bot-skills",
                {"reflect_mission": "Store procedural knowledge."},
            )
        )
    finally:
        client.close()

    assert updated is True
    call_api.assert_awaited_once()
    awaited = call_api.await_args
    assert awaited is not None
    args = awaited.args
    assert args[0] == "PATCH"
    assert args[1] == "https://memory.example/base/v1/default/banks/bot-skills/config"
    assert args[2]["Authorization"] == "Bearer test-key"
    assert args[3] == {"updates": {"reflect_mission": "Store procedural knowledge."}}
    assert awaited.kwargs == {"_request_timeout": 120.0}


def test_create_user_bank_persists_may_exist_marker() -> None:
    fake_hindsight = FakeHindsight()
    state = RecordingUserBankState()
    client = _memory_client(fake_hindsight, user_bank_state_store=state)

    created = asyncio.run(
        client.create_bank(
            bank_id="user:123",
            name="webhead's Memory",
        )
    )

    assert created is True
    assert state.marked == ["123"]
    assert fake_hindsight.create_bank_calls == [{"bank_id": "user:123", "name": "webhead's Memory"}]


def test_recall_forwards_fact_types_to_hindsight_async_api() -> None:
    fake_hindsight = FakeRecallHindsight()
    client = _memory_client(fake_hindsight)

    memories = asyncio.run(
        client.recall(
            bank_id="user:123",
            query="Quest setup",
            budget="mid",
            max_tokens=1200,
            types=["world", "experience"],
        )
    )

    assert [m.text for m in memories] == ["webhead uses a Quest 3."]
    assert memories[0].tags == ["scope:global"]
    assert fake_hindsight.recall_calls == [
        {
            "bank_id": "user:123",
            "query": "Quest setup",
            "budget": "mid",
            "max_tokens": 1200,
            "types": ["world", "experience"],
        }
    ]


def test_retain_forces_completed_write_and_forwards_timestamp() -> None:
    fake_hindsight = FakeRetainHindsight()
    client = _memory_client(fake_hindsight)

    retained = asyncio.run(
        client.retain(
            bank_id="user:123",
            content="webhead (2026-06-03T12:00:00Z): I use a Quest 3.",
            context="Discord current-user memory for webhead.",
            document_id="user-memory:123:111:abcd",
            metadata={"source_kind": "discord_user_memory"},
            timestamp="2026-06-03T12:00:00Z",
            retain_async=True,
        )
    )

    assert retained is True
    assert fake_hindsight.retain_calls == [
        {
            "bank_id": "user:123",
            "content": "webhead (2026-06-03T12:00:00Z): I use a Quest 3.",
            "retain_async": False,
            "context": "Discord current-user memory for webhead.",
            "document_id": "user-memory:123:111:abcd",
            "metadata": {"source_kind": "discord_user_memory"},
            "timestamp": "2026-06-03T12:00:00Z",
        }
    ]


def test_user_bank_retain_persists_may_exist_marker() -> None:
    fake_hindsight = FakeRetainHindsight()
    state = RecordingUserBankState()
    client = _memory_client(fake_hindsight, user_bank_state_store=state)

    retained = asyncio.run(
        client.retain(
            bank_id="user:123",
            content="webhead uses a Quest 3.",
        )
    )

    assert retained is True
    assert state.marked == ["123"]
    assert len(fake_hindsight.retain_calls) == 1


def test_user_bank_retain_is_refused_when_marker_cannot_be_persisted() -> None:
    fake_hindsight = FakeRetainHindsight()
    client = _memory_client(
        fake_hindsight,
        user_bank_state_store=RecordingUserBankState(fail=True),
    )

    retained = asyncio.run(
        client.retain(
            bank_id="user:123",
            content="webhead uses a Quest 3.",
        )
    )

    assert retained is False
    assert fake_hindsight.retain_calls == []


@pytest.mark.parametrize(
    ("success", "var_async"),
    [(False, False), (True, True)],
)
def test_retain_rejects_uncompleted_backend_response(
    success: bool,
    var_async: bool,
) -> None:
    fake_hindsight = FakeRetainHindsight(success=success, var_async=var_async)
    client = _memory_client(fake_hindsight)

    retained = asyncio.run(
        client.retain(
            bank_id="user:123",
            content="webhead uses a Quest 3.",
        )
    )

    assert retained is False


def test_delete_bank_uses_async_hindsight_api() -> None:
    class _RecordingHindsight:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def adelete_bank(self, bank_id: str) -> object:
            self.calls.append(bank_id)
            return SimpleNamespace(success=True)

    hindsight = _RecordingHindsight()
    client = _memory_client(hindsight)

    assert asyncio.run(client.delete_bank_strict(bank_id="user:123")) is True
    assert hindsight.calls == ["user:123"]


def test_delete_bank_treats_backend_404_as_idempotent_success() -> None:
    class _NotFound(Exception):
        def __init__(self) -> None:
            super().__init__("bank already absent")
            self.status = 404

    class _MissingBankHindsight:
        async def adelete_bank(self, bank_id: str) -> None:
            raise _NotFound

    client = _memory_client(_MissingBankHindsight())

    assert asyncio.run(client.delete_bank_strict(bank_id="user:123")) is True


def test_strict_bank_delete_raises_when_backend_does_not_confirm_deletion() -> None:
    class _DownHindsight:
        async def adelete_bank(self, bank_id: str) -> None:
            raise RuntimeError("backend down")

    client = _memory_client(_DownHindsight())

    with pytest.raises(MemoryBackendError):
        asyncio.run(client.delete_bank_strict(bank_id="user:123"))


def test_strict_bank_delete_rejects_negative_backend_acknowledgement() -> None:
    class _NegativeHindsight:
        async def adelete_bank(self, bank_id: str) -> object:
            return SimpleNamespace(success=False)

    client = _memory_client(_NegativeHindsight())

    with pytest.raises(MemoryBackendError):
        asyncio.run(client.delete_bank_strict(bank_id="user:123"))


class FakeDownHindsight:
    async def arecall(self, **kwargs: object) -> object:
        raise RuntimeError("backend down")


def _down_client() -> MemoryClient:
    return _memory_client(FakeDownHindsight())


def test_recall_swallows_backend_failure_into_safe_default() -> None:
    client = _down_client()

    assert asyncio.run(client.recall(bank_id="user:123", query="q")) == []


def test_ensure_user_bank_retries_after_failed_creation() -> None:
    banks._initialized_banks.clear()
    failing_client = FakeMemoryClient(created=False)

    bank_id = asyncio.run(
        banks.ensure_user_bank(
            client=cast(MemoryClient, failing_client),
            discord_id="123",
            display_name="webhead",
        )
    )

    assert bank_id is None
    assert "user:123" not in banks._initialized_banks

    succeeding_client = FakeMemoryClient(created=True)
    bank_id = asyncio.run(
        banks.ensure_user_bank(
            client=cast(MemoryClient, succeeding_client),
            discord_id="123",
            display_name="webhead",
        )
    )

    assert bank_id == "user:123"
    assert "user:123" in banks._initialized_banks
