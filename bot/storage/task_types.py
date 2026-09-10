"""Typed records at the scheduled-task persistence boundary."""

from __future__ import annotations

from typing import Any, Literal, NotRequired, TypedDict

TaskStatus = Literal["draft", "active", "paused", "attention", "completed", "rejected"]
RunStatus = Literal[
    "running",
    "delivery",
    "completed",
    "no_change",
    "needs_input",
    "failed",
    "read_failed",
    "cancelled",
    "interrupted",
    "delivery_failed",
]
ApprovalStatus = Literal["pending", "approved", "rejected"]
DeliveryStatus = Literal["pending", "sending", "sent", "failed", "uncertain", "cancelled"]


class TaskRecord(TypedDict):
    id: str
    guild_id: str
    owner_id: str
    channel_id: str
    status: TaskStatus
    revision: int
    active_revision: int | None
    state_generation: int
    next_run: float | None
    created_at: float
    updated_at: float
    proposer_id: str
    approval_status: ApprovalStatus
    definition: dict[str, Any]
    state: dict[str, Any]
    read_failure_streak: NotRequired[int]


class DueTask(TypedDict):
    id: str
    owner_id: str
    next_run: float
    execution: Literal["llm", "python_gate", "python_only"]


class RunRecord(TypedDict):
    id: str
    revision: int
    scheduled_for: float
    status: RunStatus
    detail: str
    created_at: float
    finished_at: float | None


class DeliveryRecord(TypedDict):
    id: int
    run_id: str
    channel_id: str
    payload_json: str
    is_log: int
    status: DeliveryStatus
    message_id: str | None
    attempts: int
    retry_at: float
    error: str
    task_id: str
    revision: int
    run_status: RunStatus
    run_detail: str


class DeliverySummary(TypedDict):
    id: int
    run_id: str
    channel_id: str
    status: DeliveryStatus
    message_id: str | None
    error: str


class TaskHistory(TypedDict):
    runs: list[RunRecord]
    deliveries: list[DeliverySummary]


class SavedTaskFile(TypedDict):
    filename: str
    description: str | None
    data: bytes
