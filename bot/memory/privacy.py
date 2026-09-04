from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from memory.banks import forget_initialized_bank, user_bank_id
from memory.mutations import user_memory_mutation


class MemoryDeletionClient(Protocol):
    async def delete_bank_strict(self, bank_id: str) -> bool: ...


class MemoryPreferenceStore(Protocol):
    async def set_memory_enabled(self, user_id: str, enabled: bool) -> bool: ...

    async def clear_persona(self, user_id: str) -> bool: ...


class AutoRetainWatermarks(Protocol):
    async def fast_forward_user(self, user_id: str) -> int: ...


class UserMemoryBankState(Protocol):
    async def mark_absent(self, user_id: str) -> None: ...


@dataclass(frozen=True)
class ForgetMemoryResult:
    bank_deleted: bool


async def forget_user_memory(
    *,
    memory_client: MemoryDeletionClient | None,
    preference_store: MemoryPreferenceStore,
    user_id: str,
    auto_retain_watermarks: AutoRetainWatermarks | None = None,
    bank_state_store: UserMemoryBankState | None = None,
) -> ForgetMemoryResult:
    bank_id = user_bank_id(user_id)

    # Keep the complete state transition under the same per-user boundary used
    # by bank setup and retain calls. Anything that entered first finishes before
    # deletion; anything that enters afterward observes memory disabled.
    async with user_memory_mutation(user_id):
        await preference_store.set_memory_enabled(user_id, False)
        await preference_store.clear_persona(user_id)
        if auto_retain_watermarks is not None:
            await auto_retain_watermarks.fast_forward_user(user_id)

        bank_deleted = False
        if memory_client is not None:
            bank_deleted = await memory_client.delete_bank_strict(bank_id)
            if bank_deleted:
                # This local commit is part of successful deletion semantics.
                # If it fails, propagate so durable /privacy remains pending;
                # retrying the already-deleted remote bank is idempotent.
                if bank_state_store is not None:
                    await bank_state_store.mark_absent(user_id)
                forget_initialized_bank(bank_id)

    return ForgetMemoryResult(bank_deleted=bank_deleted)
