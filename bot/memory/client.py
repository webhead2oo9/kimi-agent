from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

from hindsight_client import Hindsight
import contextlib

log = logging.getLogger(__name__)


class UserMemoryBankStateWriter(Protocol):
    async def mark_may_exist(self, user_id: str) -> None: ...


class MemoryBackendError(RuntimeError):
    """A Hindsight call failed. Distinguishes a backend outage from an empty bank."""


@dataclass(frozen=True)
class RecalledMemory:
    text: str
    type: str
    context: str | None = None
    tags: list[str] | None = None
    id: str | None = None
    document_id: str | None = None
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class MemoryRecord:
    id: str
    text: str
    type: str
    document_id: str | None = None
    context: str | None = None
    tags: list[str] | None = None
    metadata: dict[str, Any] | None = None
    created_at: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True)
class MemoryPage:
    items: list[MemoryRecord]
    total: int
    limit: int
    offset: int


@dataclass(frozen=True)
class DocumentRecord:
    id: str
    original_text: str
    memory_unit_count: int
    tags: list[str] | None = None
    metadata: dict[str, Any] | None = None
    created_at: str | None = None
    updated_at: str | None = None


class MemoryClient:
    """Async wrapper around the Hindsight SDK."""

    def __init__(
        self,
        url: str,
        api_key: str | None = None,
        *,
        user_bank_state_store: UserMemoryBankStateWriter | None = None,
    ) -> None:
        kwargs: dict = {"base_url": url, "timeout": 120.0}
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
        store = getattr(self, "_user_bank_state_store", None)
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

    async def recall_strict(
        self,
        bank_id: str,
        query: str,
        budget: str = "mid",
        max_tokens: int = 4096,
        types: list[str] | None = None,
        tags: list[str] | None = None,
        tags_match: str = "any",
    ) -> list[RecalledMemory]:
        """Like :meth:`recall`, but raises :class:`MemoryBackendError` on failure."""
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
                    text=r.text,
                    type=r.type or "memory",
                    context=getattr(r, "context", None),
                    tags=getattr(r, "tags", None),
                    id=getattr(r, "id", None),
                    document_id=getattr(r, "document_id", None),
                    metadata=_optional_dict(getattr(r, "metadata", None)),
                )
                for r in result.results
            ]
        except Exception as exc:
            log.exception("Failed to recall from bank %s", bank_id)
            raise MemoryBackendError(f"recall failed for bank {bank_id}") from exc

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
            return await self.recall_strict(
                bank_id=bank_id,
                query=query,
                budget=budget,
                max_tokens=max_tokens,
                types=types,
                tags=tags,
                tags_match=tags_match,
            )
        except MemoryBackendError:
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

    async def list_memories_strict(
        self,
        bank_id: str,
        query: str | None = None,
        memory_type: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> MemoryPage:
        """Like :meth:`list_memories`, but raises :class:`MemoryBackendError` on failure."""
        try:
            result = await self._client.memory.list_memories(
                bank_id=bank_id,
                q=query,
                type=memory_type,
                limit=limit,
                offset=offset,
            )
            return MemoryPage(
                items=[_memory_record(item) for item in result.items],
                total=int(result.total),
                limit=int(result.limit),
                offset=int(result.offset),
            )
        except Exception as exc:
            log.exception("Failed to list memories from bank %s", bank_id)
            raise MemoryBackendError(f"list_memories failed for bank {bank_id}") from exc

    async def list_memories(
        self,
        bank_id: str,
        query: str | None = None,
        memory_type: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> MemoryPage:
        try:
            return await self.list_memories_strict(
                bank_id=bank_id,
                query=query,
                memory_type=memory_type,
                limit=limit,
                offset=offset,
            )
        except MemoryBackendError:
            return MemoryPage(items=[], total=0, limit=limit, offset=offset)

    async def list_documents_strict(
        self,
        bank_id: str,
        query: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[DocumentRecord]:
        """Like :meth:`list_documents`, but raises :class:`MemoryBackendError` on failure."""
        try:
            result = await self._client.documents.list_documents(
                bank_id=bank_id,
                q=query,
                limit=limit,
                offset=offset,
            )
            return [_document_record(item) for item in result.items]
        except Exception as exc:
            log.exception("Failed to list documents from bank %s", bank_id)
            raise MemoryBackendError(f"list_documents failed for bank {bank_id}") from exc

    async def list_documents(
        self,
        bank_id: str,
        query: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[DocumentRecord]:
        try:
            return await self.list_documents_strict(
                bank_id=bank_id,
                query=query,
                limit=limit,
                offset=offset,
            )
        except MemoryBackendError:
            return []

    async def get_document_strict(
        self,
        bank_id: str,
        document_id: str,
    ) -> DocumentRecord | None:
        """Fetch one document, ``None`` when it genuinely does not exist (HTTP 404).

        Raises :class:`MemoryBackendError` on any other failure so callers can
        distinguish a backend outage from a missing document.
        """
        try:
            result = await self._client.documents.get_document(
                bank_id=bank_id,
                document_id=document_id,
            )
            return _document_record(result)
        except Exception as exc:
            if _exception_status(exc) == 404:
                return None
            log.exception("Failed to get document %s from bank %s", document_id, bank_id)
            raise MemoryBackendError(f"get_document failed for bank {bank_id}") from exc

    async def get_document(
        self,
        bank_id: str,
        document_id: str,
    ) -> DocumentRecord | None:
        try:
            return await self.get_document_strict(bank_id=bank_id, document_id=document_id)
        except MemoryBackendError:
            return None

    async def delete_document_strict(self, bank_id: str, document_id: str) -> bool:
        """Delete one document.

        Returns ``False`` only when the backend confirms the document is absent.
        Any other failure raises :class:`MemoryBackendError`, allowing destructive
        admin surfaces to avoid presenting an outage as a successful no-op.
        """
        try:
            result = await self._client.documents.delete_document(
                bank_id=bank_id,
                document_id=document_id,
            )
        except Exception as exc:
            if _exception_status(exc) == 404:
                return False
            log.exception("Failed to delete document %s from bank %s", document_id, bank_id)
            raise MemoryBackendError(f"delete_document failed for bank {bank_id}") from exc
        if getattr(result, "success", None) is not True:
            log.error(
                "Document delete was not confirmed for document %s in bank %s",
                document_id,
                bank_id,
            )
            raise MemoryBackendError(f"delete_document failed for bank {bank_id}")
        return True

    async def delete_document(self, bank_id: str, document_id: str) -> bool:
        """Best-effort compatibility wrapper around :meth:`delete_document_strict`."""

        try:
            return await self.delete_document_strict(
                bank_id=bank_id,
                document_id=document_id,
            )
        except MemoryBackendError:
            return False

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

    async def delete_bank(self, bank_id: str) -> bool:
        """Best-effort compatibility wrapper around :meth:`delete_bank_strict`."""

        try:
            return await self.delete_bank_strict(bank_id)
        except MemoryBackendError:
            return False

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
            # The public ``update_bank_config`` is sync (wraps its own event loop),
            # and the generated ``banks.*`` async API builds host-less URLs in this
            # SDK build. The hand-written private ``_aupdate_bank_config`` constructs
            # the full URL itself (like ``acreate_bank``), so it is the reliable async
            # path; revisit if the SDK is bumped.
            await self._client._aupdate_bank_config(bank_id, clean)
            return True
        except Exception:
            log.exception("Failed to update bank config for %s", bank_id)
            return False

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._client.close()


def _memory_record(item: object) -> MemoryRecord:
    return MemoryRecord(
        id=str(_value(item, "id", "memory_id") or ""),
        text=str(_value(item, "text", "content", "fact") or ""),
        type=str(_value(item, "type", "fact_type") or "memory"),
        document_id=_optional_str(_value(item, "document_id")),
        context=_optional_str(_value(item, "context")),
        tags=_optional_str_list(_value(item, "tags")),
        metadata=_optional_dict(_value(item, "metadata", "document_metadata")),
        created_at=_optional_str(_value(item, "created_at")),
        updated_at=_optional_str(_value(item, "updated_at")),
    )


def _document_record(item: object) -> DocumentRecord:
    return DocumentRecord(
        id=str(_value(item, "id", "document_id") or ""),
        original_text=str(_value(item, "original_text", "text", "content") or ""),
        memory_unit_count=_optional_int(_value(item, "memory_unit_count")),
        tags=_optional_str_list(_value(item, "tags")),
        metadata=_optional_dict(_value(item, "document_metadata", "metadata")),
        created_at=_optional_str(_value(item, "created_at")),
        updated_at=_optional_str(_value(item, "updated_at")),
    )


def _value(item: object, *names: str) -> object:
    if isinstance(item, dict):
        for name in names:
            if name in item:
                return item[name]
        return None
    for name in names:
        value = getattr(item, name, None)
        if value is not None:
            return value
    return None


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _optional_int(value: object) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value)
    return 0


def _optional_str_list(value: object) -> list[str] | None:
    if not isinstance(value, list):
        return None
    return [str(item) for item in value]


def _optional_dict(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return value


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
