"""Dependencies and per-run values shared by scheduled-task components."""

from __future__ import annotations
import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from discord.ext import commands
from app.providers import ProviderManager
from app.task_access import TaskAccess
from app.tools import RuntimeTools
from config.settings import Settings
from discord_adapter.gateway import DiscordGateway
from moderation.service import ModerationService
from storage.conversations import ConversationStore
from storage.scheduled_tasks import ScheduledTaskStore
from storage.usage import UsageStore
from tools.scheduled_tasks import TaskDefinition
from utils.privacy_barrier import UserPrivacyBarrier
from storage.task_types import TaskRecord

from dataclasses import field
from typing import Literal, TypedDict
from pydantic import BaseModel, Field


@dataclass(frozen=True)
class ScheduledTaskRuntime:
    bot: commands.Bot
    settings: Settings
    tools: RuntimeTools
    store: ScheduledTaskStore
    conversations: ConversationStore
    usage: UsageStore
    providers: ProviderManager
    gateway: DiscordGateway
    access: TaskAccess
    privacy: UserPrivacyBarrier
    user_blocked: Callable[[str], Awaitable[bool]]
    semaphore: asyncio.Semaphore
    moderation: ModerationService | None


class TaskCompletion(BaseModel):
    outcome: Literal["completed", "no_change", "needs_input"]
    state: dict[str, Any]
    detail: str = Field(default="", max_length=4000)
    content: str = Field(default="", max_length=60000)


class PreviewResult(TypedDict):
    task_name: str
    revision: int
    outcome: str
    detail: str
    posts: list[dict[str, Any]]
    files: list[tuple[str, str | None, bytes]]


@dataclass
class ActiveRun:
    task: TaskRecord
    definition: TaskDefinition
    posts: list[dict[str, Any]] = field(default_factory=list)


class ActiveRuns(dict[str, ActiveRun]):
    def register(self, run_id: str, task: TaskRecord) -> None:
        self[run_id] = ActiveRun(task, TaskDefinition.model_validate(task["definition"]))
