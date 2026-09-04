from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass
from typing import Any, Protocol

from hindsight_client import Hindsight
from hindsight_client_api.models import BankConfigUpdate

log = logging.getLogger(__name__)
_HINDSIGHT_TIMEOUT_SECONDS = 120.0


class UserMemoryBankStateWriter(Protocol):
    async def mark_may_exist(self, user_id: str) -> None: ...


class MemoryBackendError(RuntimeError):
    """A Hindsight call failed. Distinguishes a backend outage from an empty bank."""


@dataclass(frozen=True)
class RecalledMemory:
    text: str
    type: str
    tags: list[str] | None = None


class MemoryClient:
    """Async wrapper around the Hindsight SDK."""

    def __init__(
        self,
        url: str,
        api_key: str | None = None,
        *,
        user_bank_state_store: UserMemoryBankStateWriter | None = None,
    ) -> None:
        kwargs: dict = {
            "base_url": url.rstrip("/"),
            "timeout": _HINDSIGHT_TIMEOUT_SECONDS,
        }
        if api_key:
            kwargs["api_key"] = api_key
        self._client = Hindsight(**kwargs)
        self._user_bank_state_store = user_bank_state_store

    def set_user_bank_state_store(
        self,
        store: UserMemoryBankStateWriter | None,
    ) -> None:
        """Attach durable bank-state tracking after SQLite is initialized."""

        self._user_bank_state_store = store

    async def _mark_user_bank_may_exist(self, bank_id: str) -> bool:
        prefix, separator, user_id = bank_id.partition(":")
        if prefix != "user" or separator != ":" or not user_id:
            return True
        store = self._user_bank_state_store
        if store is None:
            return True
        try:
            # Persist before the remote mutation: if Hindsight succeeds and the
            # process dies immediately afterward, deletion still knows the bank
            # may exist.
            await store.mark_may_exist(user_id)
        except Exception:
            log.exception(
                "Refusing untracked Hindsight mutation for user bank %s",
                bank_id,
            )
            return False
        return True

    async def retain(
        self,
        bank_id: str,
        content: str,
        context: str = "",
        tags: list[str] | None = None,
        document_id: str = "",
        metadata: dict[str, str] | None = None,
        timestamp: str | None = None,
        update_mode: str | None = None,
        retain_async: bool = False,
    ) -> bool:
        """Retain content and return only after Hindsight completed the write.

        Hindsight's asynchronous mode reports successful queue admission, not
        successful extraction.  This wrapper's boolean is consumed as a durable
        completion signal by user-memory tools and auto-retain watermarks, so force
        synchronous processing even when a caller requests async mode.
        """
        if not await self._mark_user_bank_may_exist(bank_id):
            return False
        try:
            kwargs: dict = {
                "bank_id": bank_id,
                "content": content,
                "retain_async": False,
            }
            if retain_async:
                log.debug(
                    "Forcing synchronous Hindsight retain for completion-aware write to bank %s",
                    bank_id,
                )
            if context:
                kwargs["context"] = context
            if tags:
                kwargs["tags"] = tags
            if document_id:
                kwargs["document_id"] = document_id
            if metadata:
                kwargs["metadata"] = metadata
            if timestamp:
                kwargs["timestamp"] = timestamp
            if update_mode:
                kwargs["update_mode"] = update_mode
            result = await self._client.aretain(**kwargs)
            if not result.success or result.var_async:
                log.error(
                    "Hindsight retain did not complete for bank %s (success=%s, async=%s)",
                    bank_id,
                    result.success,
                    result.var_async,
                )
                return False
            return True
        except Exception:
            log.exception("Failed to retain to bank %s", bank_id)
            return False

    async def recall(
        self,
        bank_id: str,
        query: str,
        budget: str = "mid",
        max_tokens: int = 4096,
        types: list[str] | None = None,
        tags: list[str] | None = None,
        tags_match: str = "any",
    ) -> list[RecalledMemory]:
        try:
            kwargs: dict = {
                "bank_id": bank_id,
                "query": query,
                "budget": budget,
                "max_tokens": max_tokens,
            }
            if types:
                kwargs["types"] = types
            if tags:
                kwargs["tags"] = tags
                kwargs["tags_match"] = tags_match
            result = await self._client.arecall(**kwargs)
            return [
                RecalledMemory(
                    text=item.text,
                    type=item.type or "memory",
                    tags=getattr(item, "tags", None),
                )
                for item in result.results
            ]
        except Exception:
            log.exception("Failed to recall from bank %s", bank_id)
            return []

    async def reflect(
        self,
        bank_id: str,
        query: str,
        budget: str = "mid",
        tags: list[str] | None = None,
        tags_match: str = "any",
    ) -> str:
        try:
            kwargs: dict = {
                "bank_id": bank_id,
                "query": query,
                "budget": budget,
            }
            if tags:
                kwargs["tags"] = tags
                kwargs["tags_match"] = tags_match
            result = await self._client.areflect(**kwargs)
            return result.text
        except Exception:
            log.exception("Failed to reflect from bank %s", bank_id)
            return ""

    async def delete_bank_strict(self, bank_id: str) -> bool:
        """Delete a bank, treating an already-absent bank as success.

        Bank deletion is idempotent for privacy retries, but an unconfirmed
        backend failure is raised so the durable deletion request stays pending.
        """
        try:
            result = await self._client.adelete_bank(bank_id)
        except Exception as exc:
            # Retried privacy deletion may have removed the bank immediately
            # before a crash prevented the durable SQLite request from being
            # finalized. "Already absent" is the desired idempotent end state.
            if _exception_status(exc) == 404:
                return True
            log.exception("Failed to delete bank %s", bank_id)
            raise MemoryBackendError(f"delete_bank failed for bank {bank_id}") from exc
        if getattr(result, "success", None) is not True:
            log.error("Bank delete was not confirmed for bank %s", bank_id)
            raise MemoryBackendError(f"delete_bank failed for bank {bank_id}")
        return True

    async def create_bank(
        self,
        bank_id: str,
        name: str,
        *,
        reflect_mission: str = "",
        retain_mission: str = "",
        retain_extraction_mode: str = "",
        observations_mission: str = "",
        disposition: dict | None = None,
    ) -> bool:
        """Create a bank and apply its retain/reflect/observation configuration.

        The bank-create PUT only registers the bank shell: the current Hindsight
        backend ignores ``mission``/``disposition`` on that call and requires the
        ``/config`` PATCH (see :meth:`update_bank_config`) for the retain/reflect/
        observation missions and disposition that actually steer extraction. We
        therefore create, then configure, and treat a failed config as a failed
        create so the caller does not cache an un-personalised bank.
        """
        if not await self._mark_user_bank_may_exist(bank_id):
            return False
        try:
            await self._client.acreate_bank(bank_id=bank_id, name=name)
        except Exception:
            log.exception("Failed to create bank %s", bank_id)
            return False
        updates: dict[str, Any] = {
            "reflect_mission": reflect_mission or None,
            "retain_mission": retain_mission or None,
            "retain_extraction_mode": retain_extraction_mode or None,
            "observations_mission": observations_mission or None,
        }
        if disposition:
            updates["disposition_skepticism"] = disposition.get("skepticism")
            updates["disposition_literalism"] = disposition.get("literalism")
            updates["disposition_empathy"] = disposition.get("empathy")
        return await self.update_bank_config(bank_id, updates)

    async def update_bank_config(self, bank_id: str, updates: dict[str, Any]) -> bool:
        """Apply config overrides (retain/reflect/observation missions, disposition)
        through the backend ``/config`` PATCH. ``None`` values are dropped; an empty
        update is a no-op success. Returns ``False`` on backend failure.
        """
        clean = {key: value for key, value in updates.items() if value is not None}
        if not clean:
            return True
        try:
            await self._client.banks.update_bank_config(
                bank_id=bank_id,
                bank_config_update=BankConfigUpdate(updates=clean),
                _request_timeout=_HINDSIGHT_TIMEOUT_SECONDS,
            )
            return True
        except Exception:
            log.exception("Failed to update bank config for %s", bank_id)
            return False

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._client.close()


def _exception_status(exc: Exception) -> int | None:
    """Return a common HTTP status attribute exposed by SDK exception variants."""

    candidates = (
        getattr(exc, "status", None),
        getattr(exc, "status_code", None),
        getattr(getattr(exc, "response", None), "status", None),
        getattr(getattr(exc, "response", None), "status_code", None),
    )
    for value in candidates:
        if value is None:
            continue
        try:
            return int(value)
        except TypeError, ValueError:
            continue
    return None
