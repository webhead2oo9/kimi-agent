from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from memory.banks import forget_initialized_bank, user_bank_id
from memory.mutations import user_memory_mutation


class MemoryDeletionClient(Protocol):
    async def delete_bank(self, bank_id: str) -> bool: ...


class MemoryPreferenceStore(Protocol):
    async def set_memory_enabled(self, user_id: str, enabled: bool) -> bool: ...


class AutoRetainWatermarks(Protocol):
    async def fast_forward_user(self, user_id: str) -> int: ...


class UserMemoryBankState(Protocol):
    async def mark_absent(self, user_id: str) -> None: ...


@dataclass(frozen=True)
class ForgetMemoryResult:
    user_id: str
    bank_id: str
    bank_deleted: bool
    memory_disabled: bool


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
        memory_disabled = await preference_store.set_memory_enabled(user_id, False)
        clear_persona = getattr(preference_store, "clear_persona", None)
        if clear_persona is not None:
            await clear_persona(user_id)
        if auto_retain_watermarks is not None:
            await auto_retain_watermarks.fast_forward_user(user_id)

        bank_deleted = False
        if memory_client is not None:
            # Production clients expose a strict delete that raises when the
            # backend did not confirm the wipe. Lightweight adapters may expose
            # only delete_bank, but its ``False`` must never become success.
            strict_delete = getattr(memory_client, "delete_bank_strict", None)
            if strict_delete is not None:
                bank_deleted = await strict_delete(bank_id)
            else:
                bank_deleted = await memory_client.delete_bank(bank_id)
            if bank_deleted:
                # This local commit is part of successful deletion semantics.
                # If it fails, propagate so durable /privacy remains pending;
                # retrying the already-deleted remote bank is idempotent.
                if bank_state_store is not None:
                    await bank_state_store.mark_absent(user_id)
                forget_initialized_bank(bank_id)

    return ForgetMemoryResult(
        user_id=user_id,
        bank_id=bank_id,
        bank_deleted=bank_deleted,
        memory_disabled=memory_disabled,
    )
