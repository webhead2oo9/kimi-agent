"""White-box state probes for application coordinator tests.

These snapshots intentionally live with tests: none is a runtime capability or
supported application API.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from app.admission import TurnAdmissionController
from app.command_sync import DiscordCommandSync
from app.guild_activation import GuildActivationService
from app.lifecycle import ApplicationLifecycle
from app.root_locks import RootLockPool


@dataclass(frozen=True, slots=True)
class AdmissionState:
    active_total: int
    active_by_user: dict[str, int]


async def admission_state(controller: TurnAdmissionController) -> AdmissionState:
    """Read admission counters under their production lock."""

    async with controller._lock:
        return AdmissionState(
            active_total=controller._active_total,
            active_by_user=dict(controller._active_by_user),
        )


@dataclass(frozen=True, slots=True)
class RootLockState:
    keys: tuple[str, ...]
    refcounts: Mapping[str, int]


def root_lock_state(pool: RootLockPool) -> RootLockState:
    return RootLockState(
        keys=tuple(sorted(pool._locks)),
        refcounts=MappingProxyType(dict(pool._refcounts)),
    )


@dataclass(frozen=True, slots=True)
class CommandSyncState:
    gateway_generation: int
    global_sync_task: asyncio.Task[None] | None
    global_sync_generation: int | None
    retired_global_sync_tasks: tuple[asyncio.Task[None], ...]
    ready_event_tasks: tuple[asyncio.Task[Any], ...]
    ready_event_generations: Mapping[asyncio.Task[Any], int]


def command_sync_state(command_sync: DiscordCommandSync) -> CommandSyncState:
    return CommandSyncState(
        gateway_generation=command_sync._gateway_generation,
        global_sync_task=command_sync._global_sync_task,
        global_sync_generation=command_sync._global_sync_generation,
        retired_global_sync_tasks=tuple(command_sync._retired_global_sync_tasks),
        ready_event_tasks=tuple(command_sync._ready_event_generations),
        ready_event_generations=MappingProxyType(dict(command_sync._ready_event_generations)),
    )


def guild_activation_task(
    service: GuildActivationService,
) -> asyncio.Task[None] | None:
    return service._refresh_task


@dataclass(frozen=True, slots=True)
class LifecycleState:
    closed: bool
    close_complete: bool
    startup_error: Exception | None
    db_initialized: bool
    gateway_ready: bool
    workspace_sweeper_started: bool
    auto_retain_sweeper_started: bool
    transcript_retention_sweeper_started: bool
    video_session_sweeper_started: bool
    guild_activation_refresh_task: asyncio.Task[None] | None
    auto_retain_task: asyncio.Task[Any] | None
    attachment_sweeper_task: asyncio.Task[Any] | None
    workspace_sweeper_task: asyncio.Task[Any] | None
    transcript_retention_task: asyncio.Task[Any] | None
    video_session_sweeper_task: asyncio.Task[Any] | None
    module_event_publisher: object | None
    module_interaction_runtime: object | None


def lifecycle_state(lifecycle: ApplicationLifecycle) -> LifecycleState:
    return LifecycleState(
        closed=lifecycle._closed,
        close_complete=lifecycle._close_complete.is_set(),
        startup_error=lifecycle._startup_error,
        db_initialized=lifecycle._db_initialized,
        gateway_ready=lifecycle._gateway_ready,
        workspace_sweeper_started=lifecycle._workspace_sweeper_started,
        auto_retain_sweeper_started=lifecycle._auto_retain_sweeper_started,
        transcript_retention_sweeper_started=lifecycle._transcript_retention_sweeper_started,
        video_session_sweeper_started=lifecycle._video_session_sweeper_started,
        guild_activation_refresh_task=guild_activation_task(lifecycle._resources.guild_activation),
        auto_retain_task=lifecycle._auto_retain_task,
        attachment_sweeper_task=lifecycle._attachment_sweeper_task,
        workspace_sweeper_task=lifecycle._workspace_sweeper_task,
        transcript_retention_task=lifecycle._transcript_retention_task,
        video_session_sweeper_task=lifecycle._video_session_sweeper_task,
        module_event_publisher=lifecycle._module_event_publisher,
        module_interaction_runtime=lifecycle._module_interaction_runtime,
    )
