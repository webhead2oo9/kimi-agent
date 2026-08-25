import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest

from memory import banks
from memory.client import MemoryBackendError, MemoryClient, MemoryRecord


class FakeHindsight:
    def __init__(self) -> None:
        self.create_bank_calls: list[dict] = []
        self.config_calls: list[tuple[str, dict]] = []

    def create_bank(self, **kwargs: object) -> None:
        raise AssertionError("sync create_bank should not be used in async code")

    async def acreate_bank(self, **kwargs: object) -> None:
        self.create_bank_calls.append(kwargs)

    async def _aupdate_bank_config(self, bank_id: str, updates: dict) -> dict:
        self.config_calls.append((bank_id, updates))
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
                            "context": None,
                            "tags": None,
                            "document_id": "user-memory:123:111:abcd",
                            "metadata": {
                                "source_kind": "discord_user_memory",
                                "subject_user_id": "123",
                            },
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


class FakeLowLevelMemoryApi:
    def __init__(self) -> None:
        self.list_calls: list[dict[str, object]] = []
        self.clear_calls: list[dict[str, object]] = []

    async def list_memories(self, **kwargs: object) -> object:
        self.list_calls.append(kwargs)
        return type(
            "ListMemoryResult",
            (),
            {
                "items": [
                    {
                        "id": "mem-1",
                        "text": "webhead prefers seated VR.",
                        "type": "world",
                        "document_id": "doc-1",
                        "tags": ["source:admin_manual"],
                        "metadata": {"source": "admin_manual"},
                    }
                ],
                "total": 1,
                "limit": 20,
                "offset": 0,
            },
        )()

    async def clear_bank_memories(self, **kwargs: object) -> object:
        self.clear_calls.append(kwargs)
        return object()


class FakeLowLevelDocumentsApi:
    def __init__(self) -> None:
        self.delete_calls: list[dict[str, object]] = []

    async def delete_document(self, **kwargs: object) -> object:
        self.delete_calls.append(kwargs)
        return SimpleNamespace(success=True)


class FakeAdminHindsight:
    def __init__(self) -> None:
        self.memory = FakeLowLevelMemoryApi()
        self.documents = FakeLowLevelDocumentsApi()
        self.delete_bank_calls: list[str] = []

    async def adelete_bank(self, bank_id: str) -> object:
        self.delete_bank_calls.append(bank_id)
        return SimpleNamespace(success=True)


class FakeMemoryClient:
    def __init__(self, created: bool) -> None:
        self.created = created
        self.calls: list[dict] = []

    async def create_bank(self, **kwargs: object) -> bool:
        self.calls.append(kwargs)
        return self.created


def test_create_bank_creates_shell_then_configures_via_config_patch() -> None:
    fake_hindsight = FakeHindsight()
    client = object.__new__(MemoryClient)
    client._client = cast(Any, fake_hindsight)

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
        )
    ]


def test_create_user_bank_persists_may_exist_marker() -> None:
    fake_hindsight = FakeHindsight()
    state = RecordingUserBankState()
    client = object.__new__(MemoryClient)
    client._client = cast(Any, fake_hindsight)
    client._user_bank_state_store = state

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
    client = object.__new__(MemoryClient)
    client._client = cast(Any, fake_hindsight)

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
    assert memories[0].document_id == "user-memory:123:111:abcd"
    assert memories[0].metadata == {
        "source_kind": "discord_user_memory",
        "subject_user_id": "123",
    }
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
    client = object.__new__(MemoryClient)
    client._client = cast(Any, fake_hindsight)

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
    client = object.__new__(MemoryClient)
    client._client = cast(Any, fake_hindsight)
    client._user_bank_state_store = state

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
    client = object.__new__(MemoryClient)
    client._client = cast(Any, fake_hindsight)
    client._user_bank_state_store = RecordingUserBankState(fail=True)

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
    client = object.__new__(MemoryClient)
    client._client = cast(Any, fake_hindsight)

    retained = asyncio.run(
        client.retain(
            bank_id="user:123",
            content="webhead uses a Quest 3.",
        )
    )

    assert retained is False


def test_list_memories_uses_low_level_async_api_and_normalizes_records() -> None:
    fake_hindsight = FakeAdminHindsight()
    client = object.__new__(MemoryClient)
    client._client = cast(Any, fake_hindsight)

    page = asyncio.run(
        client.list_memories(
            bank_id="user:123",
            query="seated",
            memory_type="world",
            limit=20,
            offset=0,
        )
    )

    assert page.total == 1
    assert page.items == [
        MemoryRecord(
            id="mem-1",
            text="webhead prefers seated VR.",
            type="world",
            document_id="doc-1",
            tags=["source:admin_manual"],
            metadata={"source": "admin_manual"},
        )
    ]
    assert fake_hindsight.memory.list_calls == [
        {
            "bank_id": "user:123",
            "q": "seated",
            "type": "world",
            "limit": 20,
            "offset": 0,
        }
    ]


def test_destructive_admin_operations_use_async_hindsight_apis() -> None:
    fake_hindsight = FakeAdminHindsight()
    client = object.__new__(MemoryClient)
    client._client = cast(Any, fake_hindsight)

    deleted_doc = asyncio.run(client.delete_document(bank_id="user:123", document_id="doc-1"))
    deleted_bank = asyncio.run(client.delete_bank(bank_id="user:123"))

    assert deleted_doc is True
    assert deleted_bank is True
    assert fake_hindsight.documents.delete_calls == [
        {"bank_id": "user:123", "document_id": "doc-1"}
    ]
    assert fake_hindsight.delete_bank_calls == ["user:123"]


def test_delete_bank_treats_backend_404_as_idempotent_success() -> None:
    class _NotFound(Exception):
        def __init__(self) -> None:
            super().__init__("bank already absent")
            self.status = 404

    class _MissingBankHindsight:
        async def adelete_bank(self, bank_id: str) -> None:
            raise _NotFound

    client = object.__new__(MemoryClient)
    client._client = cast(Any, _MissingBankHindsight())

    assert asyncio.run(client.delete_bank(bank_id="user:123")) is True


def test_strict_document_delete_distinguishes_missing_from_backend_failure() -> None:
    class _DeleteFailure(Exception):
        def __init__(self, status: int) -> None:
            super().__init__(f"delete failed: {status}")
            self.status_code = status

    class _DocumentsApi:
        def __init__(self, status: int) -> None:
            self.status = status

        async def delete_document(self, **kwargs: object) -> None:
            raise _DeleteFailure(self.status)

    client = object.__new__(MemoryClient)
    client._client = cast(Any, SimpleNamespace(documents=_DocumentsApi(404)))
    assert (
        asyncio.run(client.delete_document_strict(bank_id="user:123", document_id="doc-1")) is False
    )

    client._client = cast(Any, SimpleNamespace(documents=_DocumentsApi(503)))
    with pytest.raises(MemoryBackendError):
        asyncio.run(client.delete_document_strict(bank_id="user:123", document_id="doc-1"))


def test_strict_bank_delete_raises_when_backend_does_not_confirm_deletion() -> None:
    class _DownHindsight:
        async def adelete_bank(self, bank_id: str) -> None:
            raise RuntimeError("backend down")

    client = object.__new__(MemoryClient)
    client._client = cast(Any, _DownHindsight())

    with pytest.raises(MemoryBackendError):
        asyncio.run(client.delete_bank_strict(bank_id="user:123"))
    assert asyncio.run(client.delete_bank(bank_id="user:123")) is False


def test_strict_document_delete_rejects_negative_backend_acknowledgement() -> None:
    class _DocumentsApi:
        async def delete_document(self, **kwargs: object) -> object:
            return SimpleNamespace(success=False)

    client = object.__new__(MemoryClient)
    client._client = cast(Any, SimpleNamespace(documents=_DocumentsApi()))

    with pytest.raises(MemoryBackendError):
        asyncio.run(client.delete_document_strict(bank_id="user:123", document_id="doc-1"))
    assert asyncio.run(client.delete_document(bank_id="user:123", document_id="doc-1")) is False


def test_strict_bank_delete_rejects_negative_backend_acknowledgement() -> None:
    class _NegativeHindsight:
        async def adelete_bank(self, bank_id: str) -> object:
            return SimpleNamespace(success=False)

    client = object.__new__(MemoryClient)
    client._client = cast(Any, _NegativeHindsight())

    with pytest.raises(MemoryBackendError):
        asyncio.run(client.delete_bank_strict(bank_id="user:123"))
    assert asyncio.run(client.delete_bank(bank_id="user:123")) is False


class _DownLowLevelMemoryApi:
    async def list_memories(self, **kwargs: object) -> object:
        raise RuntimeError("backend down")


class _DownLowLevelDocumentsApi:
    async def list_documents(self, **kwargs: object) -> object:
        raise RuntimeError("backend down")

    async def get_document(self, **kwargs: object) -> object:
        raise RuntimeError("backend down")


class FakeDownHindsight:
    def __init__(self) -> None:
        self.memory = _DownLowLevelMemoryApi()
        self.documents = _DownLowLevelDocumentsApi()

    async def arecall(self, **kwargs: object) -> object:
        raise RuntimeError("backend down")


def _down_client() -> MemoryClient:
    client = object.__new__(MemoryClient)
    client._client = cast(Any, FakeDownHindsight())
    return client


def test_lenient_reads_swallow_backend_failure_into_safe_defaults() -> None:
    client = _down_client()

    assert asyncio.run(client.recall(bank_id="user:123", query="q")) == []
    page = asyncio.run(client.list_memories(bank_id="user:123"))
    assert page.items == [] and page.total == 0
    assert asyncio.run(client.list_documents(bank_id="user:123")) == []
    assert asyncio.run(client.get_document(bank_id="user:123", document_id="doc-1")) is None


def test_strict_reads_raise_memory_backend_error_on_failure() -> None:
    client = _down_client()

    with pytest.raises(MemoryBackendError):
        asyncio.run(client.recall_strict(bank_id="user:123", query="q"))
    with pytest.raises(MemoryBackendError):
        asyncio.run(client.list_memories_strict(bank_id="user:123"))
    with pytest.raises(MemoryBackendError):
        asyncio.run(client.list_documents_strict(bank_id="user:123"))
    with pytest.raises(MemoryBackendError):
        asyncio.run(client.get_document_strict(bank_id="user:123", document_id="doc-1"))


def test_get_document_strict_treats_404_as_genuinely_missing() -> None:
    class _NotFound(Exception):
        def __init__(self) -> None:
            super().__init__("not found")
            self.status = 404

    class _NotFoundDocumentsApi:
        async def get_document(self, **kwargs: object) -> object:
            raise _NotFound

    client = object.__new__(MemoryClient)
    client._client = cast(Any, SimpleNamespace(documents=_NotFoundDocumentsApi()))

    document = asyncio.run(client.get_document_strict(bank_id="user:123", document_id="doc-1"))
    assert document is None


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
