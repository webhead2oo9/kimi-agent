"""Durable owner-approved change proposals for trusted modules."""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from collections.abc import Mapping
from typing import Any, cast

from kimi_agent_module_api import (
    ProposalActionHandler,
    ProposalActor,
    ProposalDraft,
    ProposalError,
    ProposalNotFound,
    ProposalNotPending,
    ProposalPreview,
    ProposalRecord,
    ProposalState,
    ProposalStale,
)
from storage.db import Database

_SENSITIVE_SEGMENT = re.compile(
    r"(?:^|_)(?:api_key|authorization|credential|password|private_key|secret|token)(?:_|$)",
    re.IGNORECASE,
)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _assert_no_plaintext_secrets(value: Any, *, key: str = "") -> None:
    if (
        key
        and _SENSITIVE_SEGMENT.search(key)
        and (not isinstance(value, str) or not value.startswith("secret://"))
    ):
        raise ValueError(f"secret-bearing field {key!r} must contain an opaque secret reference")
    if isinstance(value, Mapping):
        for child_key, child in value.items():
            _assert_no_plaintext_secrets(child, key=str(child_key))
    elif isinstance(value, list | tuple):
        for child in value:
            _assert_no_plaintext_secrets(child)


class ProposalStore:
    def __init__(self, database: Database) -> None:
        self._db = database

    async def create(
        self, module_name: str, draft: ProposalDraft, preview: ProposalPreview
    ) -> ProposalRecord:
        now = time.time()
        record = ProposalRecord(
            proposal_id=uuid.uuid4().hex,
            module_name=module_name,
            action=draft.action,
            target=draft.target,
            summary=draft.summary.strip(),
            changes=dict(draft.changes),
            actor=draft.actor,
            expected_revision=draft.expected_revision,
            preview=preview,
            state="pending",
            created_at=now,
            updated_at=now,
        )
        async with self._db.write_transaction() as conn:
            await conn.execute(
                "INSERT INTO control_proposals ("
                "proposal_id, module_name, action, target, summary, changes_json, actor_json, "
                "expected_revision, preview_json, state, created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
                (
                    record.proposal_id,
                    module_name,
                    draft.action,
                    draft.target,
                    record.summary,
                    _json(record.changes),
                    _json(record.actor.__dict__),
                    draft.expected_revision,
                    _json(preview.__dict__),
                    now,
                    now,
                ),
            )
            await self._event(conn, record.proposal_id, "created", {}, now)
        return record

    async def get(self, proposal_id: str) -> ProposalRecord | None:
        async with self._db.conn.execute(
            "SELECT * FROM control_proposals WHERE proposal_id = ?", (proposal_id,)
        ) as cur:
            row = await cur.fetchone()
        return None if row is None else _record(row)

    async def list(self, state: ProposalState | None = None) -> list[ProposalRecord]:
        if state is None:
            query = "SELECT * FROM control_proposals ORDER BY created_at DESC LIMIT 100"
            params: tuple[object, ...] = ()
        else:
            query = (
                "SELECT * FROM control_proposals WHERE state = ? ORDER BY created_at DESC LIMIT 100"
            )
            params = (state,)
        async with self._db.conn.execute(query, params) as cur:
            rows = await cur.fetchall()
        return [_record(row) for row in rows]

    async def transition(
        self,
        proposal_id: str,
        *,
        from_state: ProposalState,
        to_state: ProposalState,
        decided_by: str | None = None,
        reason: str = "",
        result_message: str = "",
        event_payload: Mapping[str, Any] | None = None,
    ) -> ProposalRecord:
        now = time.time()
        async with self._db.write_transaction() as conn:
            cur = await conn.execute(
                "UPDATE control_proposals SET state = ?, decided_by = COALESCE(?, decided_by), "
                "decision_reason = CASE WHEN ? = '' THEN decision_reason ELSE ? END, "
                "result_message = CASE WHEN ? = '' THEN result_message ELSE ? END, "
                "updated_at = ? WHERE proposal_id = ? AND state = ?",
                (
                    to_state,
                    decided_by,
                    reason,
                    reason,
                    result_message,
                    result_message,
                    now,
                    proposal_id,
                    from_state,
                ),
            )
            if cur.rowcount != 1:
                raise ProposalNotPending(f"proposal {proposal_id} is no longer {from_state}")
            await self._event(conn, proposal_id, to_state, event_payload or {}, now)
        record = await self.get(proposal_id)
        if record is None:  # pragma: no cover - protected by the update above
            raise ProposalNotFound(proposal_id)
        return record

    @staticmethod
    async def _event(
        conn: Any, proposal_id: str, kind: str, payload: Mapping[str, Any], now: float
    ) -> None:
        await conn.execute(
            "INSERT INTO control_proposal_events (proposal_id, kind, payload_json, created_at) "
            "VALUES (?, ?, ?, ?)",
            (proposal_id, kind, _json(payload), now),
        )


def _record(row: Any) -> ProposalRecord:
    actor = ProposalActor(**json.loads(str(row["actor_json"])))
    preview_data = json.loads(str(row["preview_json"]))
    preview = ProposalPreview(
        revision=str(preview_data["revision"]),
        redacted_changes=cast(dict[str, Any], preview_data["redacted_changes"]),
        activation=preview_data.get("activation", "live"),
        warnings=tuple(preview_data.get("warnings", ())),
    )
    return ProposalRecord(
        proposal_id=str(row["proposal_id"]),
        module_name=str(row["module_name"]),
        action=str(row["action"]),
        target=str(row["target"]),
        summary=str(row["summary"]),
        changes=cast(dict[str, Any], json.loads(str(row["changes_json"]))),
        actor=actor,
        expected_revision=(
            None if row["expected_revision"] is None else str(row["expected_revision"])
        ),
        preview=preview,
        state=cast(ProposalState, str(row["state"])),
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
        decided_by=None if row["decided_by"] is None else str(row["decided_by"]),
        decision_reason=str(row["decision_reason"]),
        result_message=str(row["result_message"]),
    )


class DurableProposalService:
    def __init__(self, database: Database, *, owner_user_id: str) -> None:
        self._store = ProposalStore(database)
        self._owner_user_id = owner_user_id
        self._handlers: dict[str, tuple[str, ProposalActionHandler]] = {}
        self._apply_lock = asyncio.Lock()

    def register_handler(
        self, module_name: str, action: str, handler: ProposalActionHandler
    ) -> None:
        if module_name != "core" and not action.startswith(f"{module_name}."):
            raise ValueError(f"module action {action!r} must use the {module_name!r} namespace")
        if not action or action in self._handlers:
            raise RuntimeError(f"duplicate proposal action {action!r}")
        self._handlers[action] = (module_name, handler)

    def unregister_module(self, module_name: str) -> None:
        self._handlers = {
            action: registered
            for action, registered in self._handlers.items()
            if registered[0] != module_name
        }

    async def create(self, module_name: str, draft: ProposalDraft) -> ProposalRecord:
        registered_module, handler = self._handler(draft.action)
        if module_name != registered_module and registered_module != "core":
            raise ProposalError(
                f"module {module_name!r} cannot create action owned by {registered_module!r}"
            )
        if not draft.summary.strip():
            raise ValueError("proposal summary must not be empty")
        _assert_no_plaintext_secrets(draft.changes)
        preview = await handler.preview(draft)
        if draft.expected_revision is not None and draft.expected_revision != preview.revision:
            raise ProposalStale("proposal was based on a stale configuration revision")
        return await self._store.create(module_name, draft, preview)

    async def get(self, proposal_id: str) -> ProposalRecord | None:
        return await self._store.get(proposal_id)

    async def list(self, *, state: ProposalState | None = None) -> list[ProposalRecord]:
        return await self._store.list(state)

    async def approve(self, proposal_id: str, *, owner_user_id: str) -> ProposalRecord:
        self._require_owner(owner_user_id)
        async with self._apply_lock:
            record = await self._required(proposal_id)
            if record.state != "pending":
                raise ProposalNotPending(f"proposal {proposal_id} is {record.state}")
            _registered_module, handler = self._handler(record.action)
            draft = ProposalDraft(
                action=record.action,
                target=record.target,
                summary=record.summary,
                changes=record.changes,
                actor=record.actor,
                expected_revision=record.expected_revision,
            )
            fresh = await handler.preview(draft)
            if fresh.revision != record.preview.revision:
                return await self._store.transition(
                    proposal_id,
                    from_state="pending",
                    to_state="stale",
                    decided_by=owner_user_id,
                    reason="target revision changed before approval",
                )
            try:
                await self._store.transition(
                    proposal_id,
                    from_state="pending",
                    to_state="applying",
                    decided_by=owner_user_id,
                )
                applying = await self._required(proposal_id)
                try:
                    result = await handler.apply(applying)
                except Exception as exc:
                    return await self._store.transition(
                        proposal_id,
                        from_state="applying",
                        to_state="failed",
                        result_message=str(exc),
                    )
                target_state: ProposalState = (
                    "restart_pending" if result.activation != "live" else "applied"
                )
                return await self._store.transition(
                    proposal_id,
                    from_state="applying",
                    to_state=target_state,
                    result_message=result.message,
                    event_payload={"revision": result.revision, "activation": result.activation},
                )
            except asyncio.CancelledError as cancellation:
                # The transition to ``applying`` is durable, so its compensation
                # must outlive repeated shutdown cancellation too.  The helper is
                # idempotent when cancellation landed before that transition or
                # after a terminal transition committed.
                cleanup = asyncio.create_task(
                    self._fail_interrupted_application(proposal_id),
                    name=f"proposal_cancel:{proposal_id}",
                )
                try:
                    while not cleanup.done():
                        try:
                            await asyncio.shield(cleanup)
                        except asyncio.CancelledError:
                            continue
                    await cleanup
                except Exception as cleanup_error:
                    raise cancellation from cleanup_error
                raise cancellation

    async def reject(
        self, proposal_id: str, *, owner_user_id: str, reason: str = ""
    ) -> ProposalRecord:
        self._require_owner(owner_user_id)
        return await self._store.transition(
            proposal_id,
            from_state="pending",
            to_state="rejected",
            decided_by=owner_user_id,
            reason=reason,
        )

    async def reconcile_control_state(self, state: Mapping[str, Any]) -> None:
        """Finish a restart proposal from the filesystem bootstrap journal."""
        proposal_id = str(state.get("proposal_id") or "")
        if not proposal_id:
            return
        record = await self._store.get(proposal_id)
        if record is None or record.state != "restart_pending":
            return
        if state.get("healthy"):
            await self._store.transition(
                proposal_id,
                from_state="restart_pending",
                to_state="applied",
                result_message="Restarted successfully with the managed revision.",
            )
        elif state.get("rollback_reason"):
            await self._store.transition(
                proposal_id,
                from_state="restart_pending",
                to_state="rolled_back",
                result_message=str(state["rollback_reason"]),
            )

    async def reconcile_interrupted_applications(self) -> None:
        """Fail proposals left mid-application by a previous process."""
        while records := await self._store.list(state="applying"):
            for record in records:
                await self._fail_interrupted_application(record.proposal_id)

    async def _fail_interrupted_application(self, proposal_id: str) -> None:
        try:
            await self._store.transition(
                proposal_id,
                from_state="applying",
                to_state="failed",
                result_message=(
                    "Application was interrupted before completion; review the target state "
                    "before creating a replacement proposal."
                ),
            )
        except ProposalNotPending:
            # Cancellation may have arrived just before ``applying`` was
            # committed or just after a terminal transition committed.
            return

    def _handler(self, action: str) -> tuple[str, ProposalActionHandler]:
        try:
            return self._handlers[action]
        except KeyError as exc:
            raise ProposalError(f"unknown proposal action {action!r}") from exc

    async def _required(self, proposal_id: str) -> ProposalRecord:
        record = await self._store.get(proposal_id)
        if record is None:
            raise ProposalNotFound(proposal_id)
        return record

    def _require_owner(self, user_id: str) -> None:
        if not self._owner_user_id or user_id != self._owner_user_id:
            raise PermissionError("bot owner approval is required")


__all__ = [
    "DurableProposalService",
    "ProposalError",
    "ProposalNotFound",
    "ProposalNotPending",
    "ProposalStale",
    "ProposalStore",
]
