import asyncio

import pytest

from memory.client import MemoryBackendError
from memory.privacy import forget_user_memory


class RecordingMemoryClient:
    def __init__(self, deleted: bool = True, *, error: Exception | None = None) -> None:
        self.deleted = deleted
        self.error = error
        self.delete_bank_calls: list[str] = []

    async def delete_bank_strict(self, bank_id: str) -> bool:
        self.delete_bank_calls.append(bank_id)
        if self.error is not None:
            raise self.error
        return self.deleted


class RecordingPreferenceStore:
    def __init__(self) -> None:
        self.set_calls: list[tuple[str, bool]] = []
        self.clear_persona_calls: list[str] = []

    async def set_memory_enabled(self, user_id: str, enabled: bool) -> bool:
        self.set_calls.append((user_id, enabled))
        return True

    async def clear_persona(self, user_id: str) -> bool:
        self.clear_persona_calls.append(user_id)
        return True


class RecordingBankState:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.absent: list[str] = []

    async def mark_absent(self, user_id: str) -> None:
        if self.fail:
            raise RuntimeError("database unavailable")
        self.absent.append(user_id)


def test_forget_user_memory_deletes_bank_and_disables_future_memory() -> None:
    memory = RecordingMemoryClient()
    preferences = RecordingPreferenceStore()
    bank_state = RecordingBankState()

    result = asyncio.run(
        forget_user_memory(
            memory_client=memory,
            preference_store=preferences,
            user_id="123",
            bank_state_store=bank_state,
        )
    )

    assert result.bank_deleted is True
    assert memory.delete_bank_calls == ["user:123"]
    assert preferences.set_calls == [("123", False)]
    assert preferences.clear_persona_calls == ["123"]
    assert bank_state.absent == ["123"]


def test_forget_user_memory_fails_if_deleted_bank_state_cannot_be_finalized() -> None:
    memory = RecordingMemoryClient()
    preferences = RecordingPreferenceStore()

    with pytest.raises(RuntimeError, match="database unavailable"):
        asyncio.run(
            forget_user_memory(
                memory_client=memory,
                preference_store=preferences,
                user_id="123",
                bank_state_store=RecordingBankState(fail=True),
            )
        )

    assert memory.delete_bank_calls == ["user:123"]
    assert preferences.set_calls == [("123", False)]
    assert preferences.clear_persona_calls == ["123"]


def test_forget_user_memory_disables_future_memory_without_hindsight_client() -> None:
    preferences = RecordingPreferenceStore()

    result = asyncio.run(
        forget_user_memory(
            memory_client=None,
            preference_store=preferences,
            user_id="123",
        )
    )

    assert result.bank_deleted is False
    assert preferences.set_calls == [("123", False)]
    assert preferences.clear_persona_calls == ["123"]


def test_forget_user_memory_propagates_unconfirmed_backend_failure() -> None:
    memory = RecordingMemoryClient(error=MemoryBackendError("down"))
    preferences = RecordingPreferenceStore()
    bank_state = RecordingBankState()

    with pytest.raises(MemoryBackendError):
        asyncio.run(
            forget_user_memory(
                memory_client=memory,
                preference_store=preferences,
                user_id="123",
                bank_state_store=bank_state,
            )
        )

    assert memory.delete_bank_calls == ["user:123"]
    assert preferences.set_calls == [("123", False)]
    assert preferences.clear_persona_calls == ["123"]
    assert bank_state.absent == []
