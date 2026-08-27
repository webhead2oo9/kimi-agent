from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from config.settings import Settings
from memory.banks import ensure_global_banks
from memory.client import MemoryClient
from tools.community import init_community_tools
from tools.learn import LearnHook
from tools.registry import ToolRegistry
from tools.user_memory import init_user_memory_tools, init_user_memory_write_tools

if TYPE_CHECKING:
    from storage.conversations import ConversationStore
    from storage.preferences import PreferenceStore

log = logging.getLogger(__name__)

MEMORY_TOOL_NAMES = frozenset(
    {
        "recall_community",
        "reflect_community",
        "teach",
        "recall_user",
        "reflect_user",
        "remember_user_memory",
    }
)


@dataclass
class MemoryManager:
    settings: Settings
    registry: ToolRegistry
    client: Any | None = None
    ready: bool = False
    tools_registered: bool = False
    # Audit sink for staff-taught community knowledge; see app/learn_log.py.
    on_learn: LearnHook | None = None

    def __post_init__(self) -> None:
        if self.client is None and self.settings.hindsight_url:
            self.client = MemoryClient(
                url=self.settings.hindsight_url,
                api_key=self.settings.hindsight_api_key.get_secret_value() or None,
            )

    def active_client(self) -> Any | None:
        return self.client if self.ready else None

    async def ensure_ready(
        self,
        conversation_store: ConversationStore,
        preference_store: PreferenceStore,
    ) -> None:
        if self.client is None:
            log.warning("No Hindsight URL configured - running without memory")
            self.ready = False
            return

        self.ready = await ensure_global_banks(self.client)
        if not self.ready:
            if self.tools_registered:
                self.unregister_tools()
                self.tools_registered = False
            log.warning(
                "Hindsight memory unavailable at %s - running without memory tools",
                self.settings.hindsight_url,
            )
            return

        if not self.tools_registered:
            init_community_tools(self.registry, self.client, on_learn=self.on_learn)
            init_user_memory_tools(
                self.registry,
                self.client,
                recall_types=self.settings.user_memory_recall_types,
            )
            self.tools_registered = True

        init_user_memory_write_tools(
            self.registry,
            self.client,
            conversation_store,
            preference_store,
            max_writes_per_turn=self.settings.memory_max_writes_per_turn,
        )
        log.info("Hindsight memory connected at %s", self.settings.hindsight_url)

    def unregister_tools(self) -> None:
        self.registry.remove_tools(set(MEMORY_TOOL_NAMES))

    async def close(self) -> None:
        client = self.client
        if client is None:
            return

        if self.tools_registered:
            self.unregister_tools()
        self.client = None
        self.ready = False
        self.tools_registered = False

        close = getattr(client, "close", None)
        if close is None:
            return
        try:
            result = close()
            if inspect.isawaitable(result):
                await result
        except Exception:
            log.exception("Failed to close Hindsight memory client")
