"""Shared test stubs for the test suites.

Plain importable classes (not fixtures) so tests can parameterize, subclass,
or wrap them per scenario: ``from tests.helpers import StubProvider``. Stubs
that genuinely diverge per suite (in-memory conversation stores, fake aiohttp
sessions) intentionally stay local to their test files.
"""

from __future__ import annotations

import asyncio
import base64
import re
import tempfile
import zlib
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from dataclasses import replace
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from agent.attachments import TurnImages
from agent.context import ConversationContext
from agent.core import ConversationRunResult
from agent.turn import TurnDependencies
from app.foreground_turn import HandleTurn
from app.command_sync import CommandSyncSnapshot
from app.lifecycle import AppRepositories, LifecycleSnapshot
from app.message_runtime import remove_processing_reaction as remove_message_processing_reaction
from app.root_locks import RootLockSnapshot
from app.user_app_chat import UserAppChatRequest
from config.model_config import parse_model_config_text
from config.settings import Settings
from providers.assets import write_generated_assets
from providers.types import ProviderCapability
from storage.blocked_users import BlockedUserStore
from storage.coding_tasks import CodingTaskStore
from storage.conversations import ConversationStore
from storage.db import Database
from storage.image_distillations import ImageDistillationStore
from storage.memory_banks import UserMemoryBankStateStore
from storage.model_selection import ModelSelectionStore
from storage.preferences import PreferenceStore
from storage.privacy import PrivacyDeletionRequestStore
from storage.usage import UsageStore
from storage.video_sessions import VideoSessionStore
from tools.registry import MessageContext
from tools.workspace.common import UserLocks
from trust.tiers import TrustTier
from utils.privacy_barrier import UserPrivacyBarrier

from workspace.manager import WorkspaceManager


# Real 1x1 images that survive full decoding. Provider-output validation
# rejects bare signatures, so tests that need "an image" must use these.
VALID_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAACXBIWXMAAA7EAAAOxAGVKw4b"
    "AAAADElEQVR4nGP4//8/AAX+Av4N70a4AAAAAElFTkSuQmCC"
)
VALID_JPEG_BYTES = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAIBAQEBAQIBAQECAgICAgQDAgICAgUEBAMEBgUG"
    "BgYFBgYGBwkIBgcJBwYGCAsICQoKCgoKBggLDAsKDAkKCgr/2wBDAQICAgICAgUDAwUKBwYH"
    "CgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgr/wgAR"
    "CAABAAEDAREAAhEBAxEB/8QAFAABAAAAAAAAAAAAAAAAAAAACP/EABQBAQAAAAAAAAAAAAAAAA"
    "AAAAD/2gAMAwEAAhADEAAAAX8f/8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABBQJ//8QA"
    "FBEBAAAAAAAAAAAAAAAAAAAAAP/aAAgBAwEBPwF//8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAA"
    "gBAgEBPwF//8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQAGPwJ//8QAFBABAAAAAAAAAAAA"
    "AAAAAAAAAP/aAAgBAQABPyF//9oADAMBAAIAAwAAABAf/8QAFBEBAAAAAAAAAAAAAAAAAAAAAP"
    "/aAAgBAwEBPxB//8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAgBAgEBPxB//8QAFBABAAAAAAAA"
    "AAAAAAAAAAAAAP/aAAgBAQABPxB//9k="
)


class NobodyBlocked:
    """Blocked-user store that blocks nobody, for tests that bypass init."""

    async def is_blocked(self, user_id: str) -> bool:
        return False


class LifecycleProbe:
    """Test seam for the extracted ``app.lifecycle`` module."""

    def __init__(self, app: Any) -> None:
        self._app = app

    def snapshot(self) -> LifecycleSnapshot:
        return self._app.lifecycle.snapshot()

    async def first_init_core(self) -> None:
        await self._app.lifecycle.initialize()

    async def close_resources(self) -> None:
        await self._app.lifecycle.close()

    async def resume_pending_privacy_deletions(self, *, auto_retain_watermarks: Any) -> None:
        await self._app.lifecycle.resume_pending_privacy_deletions(
            auto_retain_watermarks=auto_retain_watermarks
        )

    # These reach into ApplicationLifecycle's private state on purpose: the
    # states are unreachable without starting Discord or blocking shutdown, and
    # keeping the pokes here keeps test-only mutators off the production class.
    @property
    def _lifecycle(self) -> Any:
        return self._app.lifecycle

    def set_closed(self, value: bool = True) -> None:
        self._lifecycle._closed = value

    def set_startup_error(self, error: Exception | None) -> None:
        self._lifecycle._startup_error = error

    def set_db_initialized(self, value: bool = True) -> None:
        self._lifecycle._db_initialized = value

    def set_gateway_ready(self, value: bool = True) -> None:
        self._lifecycle._gateway_ready = value

    def set_workspace_sweeper_started(self, value: bool = True) -> None:
        self._lifecycle._workspace_sweeper_started = value

    def set_video_session_sweeper_started(self, value: bool = True) -> None:
        self._lifecycle._video_session_sweeper_started = value

    def set_guild_activation_refresh_task(self, task: Any | None) -> None:
        self._lifecycle.resources.guild_activation._refresh_task = task

    def set_video_session_sweeper_task(self, task: Any | None) -> None:
        self._lifecycle._video_session_sweeper_task = task

    def set_module_event_publisher(self, publisher: Any | None) -> None:
        self._lifecycle._module_event_publisher = publisher

    def set_module_interaction_runtime(self, runtime: Any | None) -> None:
        self._lifecycle._module_interaction_runtime = runtime

    def set_thread_handoff(self, handoff: Any | None) -> None:
        self._lifecycle._thread_handoff = handoff


def set_command_sync_retired_tasks(command_sync: Any, tasks: Iterable[Any]) -> None:
    """Replace retired tasks for the stable-snapshot concurrency invariant test."""

    command_sync._retired_global_sync_tasks.clear()
    command_sync._retired_global_sync_tasks.update(tasks)


def replace_app_repositories(app: Any, **changes: Any) -> None:
    """Substitute stores in the frozen ``app.lifecycle.AppRepositories`` bundle."""

    replace_lifecycle_resources(app, repositories=replace(app.repositories, **changes))


def replace_app_database(app: Any, database: Database) -> None:
    """Rebuild the complete ``app.lifecycle.AppRepositories`` bundle for one test DB."""

    repositories = AppRepositories(
        conversation_store=ConversationStore(database),
        preference_store=PreferenceStore(database),
        blocked_user_store=BlockedUserStore(database),
        model_selection_store=ModelSelectionStore(database),
        image_distillation_store=ImageDistillationStore(database),
        usage_store=UsageStore(database),
        video_session_store=VideoSessionStore(database),
        coding_task_store=CodingTaskStore(database),
        privacy_deletion_store=PrivacyDeletionRequestStore(database),
        user_memory_bank_state_store=UserMemoryBankStateStore(database),
    )
    replace_lifecycle_resources(app, database=database, repositories=repositories)


def replace_lifecycle_resources(app: Any, **changes: Any) -> None:
    """Substitute collaborators in the frozen ``app.lifecycle.LifecycleResources`` bundle."""

    lifecycle = app.lifecycle
    lifecycle._resources = replace(lifecycle.resources, **changes)


class CommandSyncProbe:
    """Test seam for the extracted ``app.command_sync`` module."""

    def __init__(self, app: Any) -> None:
        self._app = app

    def snapshot(self) -> CommandSyncSnapshot:
        return self._app.command_sync.snapshot()


class PersonalChatDriver:
    """Test driver for the extracted ``app.user_app_chat`` controller."""

    def __init__(self, app: Any) -> None:
        self._app = app

    async def run_chat(self, *args: Any, **kwargs: Any) -> Any:
        request_generation = kwargs.pop("request_generation")
        interaction = args[0] if args else kwargs.get("interaction")
        assert interaction is not None
        request = UserAppChatRequest(
            user_id=str(interaction.user.id),
            generation=request_generation,
        )
        return await self._app.user_app_chat.run(*args, request=request, **kwargs)

    async def reset(self, *args: Any, **kwargs: Any) -> Any:
        return await self._app.user_app_chat.reset(*args, **kwargs)

    def generation(self, user_id: str) -> int:
        return self._app.user_app_chat.generation(user_id)

    def dm_tier(self, *args: Any, **kwargs: Any) -> Any:
        return self._app.user_app_chat.classify_dm(*args, **kwargs)

    async def resolve_dm_conversation(self, *args: Any, **kwargs: Any) -> Any:
        return await self._app.user_app_chat.resolve_dm_conversation(*args, **kwargs)


class RootLockProbe:
    """Test probe for the public ``app.root_locks`` pool."""

    def __init__(self, app: Any) -> None:
        self._app = app

    def snapshot(self) -> RootLockSnapshot:
        return self._app.root_locks.snapshot()

    @asynccontextmanager
    async def hold(self, key: str) -> AsyncIterator[None]:
        async with self._app.root_locks.hold(key):
            yield

    @asynccontextmanager
    async def hold_user_conversations(self, user_id: str) -> AsyncIterator[None]:
        async with self._app.root_locks.hold_user_conversations(
            user_id,
            self._app.conversation_store,
        ):
            yield


async def remove_processing_reaction(
    app: Any,
    message: Any,
    *,
    timeout: float = 2.0,
) -> None:
    """Exercise the public message-runtime reaction cleanup seam."""

    await remove_message_processing_reaction(app.discord_gateway, message, timeout)


def install_foreground_turn_handler(app: Any, handle_turn_hook: HandleTurn) -> None:
    """Replace one application's foreground provider seam for a focused test."""

    runner = app.message_controller.make_foreground_turn_runner(handle_turn_hook=handle_turn_hook)
    replace_lifecycle_resources(app, turn_runner=runner)
    app.user_app_chat._turn_runner = runner


PNG_SIGNATURE_ONLY = b"\x89PNG\r\n\x1a\n"
VALID_PNG_BASE64 = base64.b64encode(VALID_PNG_BYTES).decode("ascii")


def make_settings(**overrides: Any) -> Settings:
    """Build test settings without consulting the checkout's dotenv file."""

    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


def corrupt_png_idat_stream(png: bytes) -> bytes:
    """Valid chunk framing and CRCs around a garbage DEFLATE payload.

    The container walk passes (every CRC checks out); only an actual decode
    notices the stream is not zlib data. This is the fixture that proves the
    Pillow layer runs at all.
    """

    idat = png.index(b"IDAT")
    length = int.from_bytes(png[idat - 4 : idat], "big")
    data_start = idat + 4
    data_end = data_start + length
    junk = bytes((i * 37 + 11) % 251 for i in range(length))
    crc = zlib.crc32(junk, zlib.crc32(b"IDAT")).to_bytes(4, "big")
    return png[:data_start] + junk + crc + png[data_end + 4 :]


def corrupt_png_crc(png: bytes) -> bytes:
    """Flip one byte inside the first IDAT payload without updating its CRC.

    The result still starts with a PNG signature and a well-formed IHDR, so a
    signature-only check accepts it; a chunk walk that verifies CRCs does not.
    """

    data_start = png.index(b"IDAT") + 4
    corrupted = bytearray(png)
    corrupted[data_start + 2] ^= 0xFF
    return bytes(corrupted)


PROJECT_ROOT = Path(__file__).resolve().parents[1]

# `.env.example` declares a key either actively (`KEY=value`) or as a commented
# default (`# KEY=value`). Both forms count as "documented", so the one parser
# has to see both: reading only active lines is what let three settings look
# absent from the template when they were merely commented.
_ENV_DECLARATION_RE = re.compile(r"(?m)^(#?)\s*([A-Z][A-Z0-9_]*)\s*=(.*)$")


def env_example_declarations() -> dict[str, str]:
    """Every key declared in `.env.example`, mapped to its literal value.

    Commented defaults map to their value with the `# ` stripped; an active
    `KEY=` with no value maps to the empty string. Use `env_example_active()`
    when a test cares that a key ships *set* rather than merely documented.
    """

    text = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    return {key: value.strip() for _, key, value in _ENV_DECLARATION_RE.findall(text)}


def env_example_active() -> dict[str, str]:
    """Only the uncommented `KEY=value` declarations in `.env.example`."""

    text = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    return {
        key: value.strip()
        for comment, key, value in _ENV_DECLARATION_RE.findall(text)
        if not comment
    }


class StubProvider:
    """Attribute-only LLMProvider stand-in for wiring tests that never run a turn."""

    def __init__(
        self,
        provider_key: str = "dummy",
        model: str = "dummy",
        capabilities: set[ProviderCapability] | None = None,
    ) -> None:
        self.provider_key = provider_key
        self.model = model
        self.capabilities = capabilities if capabilities is not None else set()


# Routing for the stub manager: one ordinary key-based profile, nothing on an
# OAuth backend. Without a model_config here, Application.run's Codex startup
# check falls back to reading the operator's real config/models.yaml off disk,
# so a unit test about run() delegating to bot.run() would consult live routing
# and live credentials, and fail on a machine whose token had expired or a
# checkout with no models.yaml at all (it is untracked instance state).
_STUB_MODEL_CONFIG_YAML = """
providers:
  stub:
    type: openai_compat
    base_url: https://stub.invalid/v1
    api_key_env: MODEL_API_KEY
models:
  stub-model:
    provider: stub
    model: stub-model
    context_window: 200000
    capabilities: [text, tool_calling]
roles:
  chat: stub-model
  compaction: stub-model
selectable_chat_models: [stub-model]
"""


class StubProviderManager:
    """ProviderManager stand-in; records close() via both flag and counter."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.main = StubProvider()
        self.closed = False
        self.close_count = 0
        self.model_config = parse_model_config_text(_STUB_MODEL_CONFIG_YAML)
        self.active_chat_model: str | None = None

    @property
    def selectable_chat_models(self) -> tuple[str, ...]:
        return tuple(self.model_config.selectable_chat_models)

    async def refresh_selectable_chat_models(self) -> None:
        return None

    async def initialize_circuits(self, store: Any) -> None:
        return None

    async def circuit_snapshots(self) -> tuple[Any, ...]:
        return ()

    async def reset_all_circuits(self) -> None:
        return None

    def validate_active_chat_model(self, model_name: str | None) -> None:
        if model_name is not None and model_name not in self.selectable_chat_models:
            raise ValueError(model_name)

    def set_active_chat_model(self, model_name: str | None) -> None:
        self.validate_active_chat_model(model_name)
        self.active_chat_model = model_name

    def ensure_research(self) -> StubProvider:
        return self.main

    def ensure_research_synth(self) -> StubProvider:
        return self.main

    def ensure_compaction(self) -> StubProvider:
        return self.main

    def build_compactor(self, llm_semaphore: Any = None) -> None:
        return None

    def has_active_llm_credentials(self) -> bool:
        return True

    async def close(self) -> None:
        self.closed = True
        self.close_count += 1


class StubContextManager:
    """TurnContextManager stand-in: fresh empty context, records tool activations."""

    def __init__(self) -> None:
        self.added_activated_tools: list[tuple[Any, set[str]]] = []

    async def build_turn_context(
        self,
        key: str,
        channel_name: str = "",
        before_discord_message_id: str | None = None,
        **_access: Any,
    ) -> ConversationContext:
        return ConversationContext(key=key)

    async def add_activated_tools(
        self,
        context: ConversationContext,
        names: set[str],
    ) -> None:
        self.added_activated_tools.append((context.db_conversation_id, set(names)))

    async def has_loaded_message(
        self,
        context: ConversationContext,
        discord_message_id: str,
    ) -> bool:
        _ = (context, discord_message_id)
        return False


class RecordingEnsureUserBank:
    """ensure_user_bank stand-in; records (memory_client, user_id, user_name) calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, str, str]] = []

    async def __call__(self, memory_client: Any, user_id: str, user_name: str) -> None:
        self.calls.append((memory_client, user_id, user_name))


class RecordingRecall:
    """recall_current_user_context stand-in; records kwargs, returns a canned result."""

    def __init__(self, result: str = "") -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return self.result


class FakeResponses:
    """OpenAI Responses SDK stand-in; records create() kwargs, returns a canned response."""

    def __init__(self, response: SimpleNamespace) -> None:
        self.calls: list[dict[str, Any]] = []
        self._response = response

    async def create(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        return self._response


@lru_cache(maxsize=1)
def _default_workspace_dir() -> Path:
    """One throwaway workspace root shared by every default-path dependency set.

    Per-call `mkdtemp()` left ~50 empty directories behind per test run, which
    nothing cleans up on Windows. Tests that inspect what was written pass their
    own `tmp_path` instead.
    """

    return Path(tempfile.mkdtemp(prefix="kimibot-tests-"))


def make_turn_dependencies(
    *,
    workspace_dir: Path | None = None,
    **overrides: Any,
) -> TurnDependencies:
    """A *complete* `TurnDependencies`, with a working default for every field.

    `TurnDependencies` has no optional fields: production wires all of them, so
    a partially-built one is not a state the turn code should have to reason
    about. Tests that care about one dependency override just that one and
    inherit inert defaults for the rest::

        deps = make_turn_dependencies(blocked_tools=lambda: frozenset({"x"}))

    `workspace_dir` defaults to a throwaway temp directory so the real
    `WorkspaceManager` can be constructed without a test needing `tmp_path`.
    Pass `tmp_path` explicitly whenever the test inspects what was written.
    """

    base = workspace_dir if workspace_dir is not None else _default_workspace_dir()

    async def _collect_reply_context(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def _run_conversation(_request: Any) -> ConversationRunResult:
        return ConversationRunResult(text="")

    async def _collect_turn_images(*_args: Any, **_kwargs: Any) -> TurnImages:
        return TurnImages(vision_parts=[], edit_target=None)

    async def _resolve_discord_references(*_args: Any, **_kwargs: Any) -> tuple[()]:
        return ()

    async def _persist_prepared_user_message(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def _user_persona_loader(_user_id: str) -> str:
        return ""

    async def _ensure_user_bank(*_args: Any, **_kwargs: Any) -> None:
        return None

    # A provider switch must not silently blank the model name a test set. The
    # default resolver echoes resolved_model_name rather than returning "".
    resolved_model_name = overrides.get("resolved_model_name", "")

    def _chat_model_name_resolver(**_kwargs: Any) -> str:
        return str(resolved_model_name)

    defaults: dict[str, Any] = {
        "context_manager": StubContextManager(),
        "provider": StubProvider(),
        "registry": object(),
        "attachment_store": object(),
        "workspace_dir": base,
        "workspace_manager": WorkspaceManager(base_dir=base),
        "workspace_locks": UserLocks(),
        "llm_semaphore": asyncio.Semaphore(1),
        "memory_client": None,
        "preference_store": None,
        "ensure_user_bank": _ensure_user_bank,
        "recall_current_user_context": RecordingRecall(),
        "skills_index_builder": lambda: "",
        "personal_skills_index_builder": lambda: "",
        "user_persona_loader": _user_persona_loader,
        "count_user_prior_messages": None,
        "channel_pinned_tools": frozenset,
        "blocked_tools": frozenset,
        "tool_configs": dict,
        "resolve_discord_references": _resolve_discord_references,
        "collect_turn_images": _collect_turn_images,
        "collect_reply_context": _collect_reply_context,
        "collect_turn_attachments": lambda _message: [],
        "strip_mention": lambda content, **_kwargs: content,
        "run_conversation": _run_conversation,
        "chat_provider_resolver": lambda **_kwargs: StubProvider(),
        "chat_model_name_resolver": _chat_model_name_resolver,
        "persist_prepared_user_message": _persist_prepared_user_message,
        "write_generated_assets": write_generated_assets,
        "compactor": None,
        "activity_reporter": None,
        "moderation_service": None,
        "usage_store": None,
        "image_distillation_store": None,
        "model_config": None,
        "resolved_model_name": "",
        "user_activity": UserPrivacyBarrier().activity,
    }
    defaults.update(overrides)
    return TurnDependencies(**defaults)


def make_message_context(
    activated: set[str] | None = None,
    *,
    user_id: str = "123",
    user_name: str = "Gamer",
    guild_id: str = "999",
    channel_id: str = "100",
    context_key: str = "guild:100:main",
    trigger_discord_message_id: str = "555",
    trust_tier: TrustTier = TrustTier.MEMBER,
) -> MessageContext:
    """MessageContext for source-tool dispatch tests."""

    return MessageContext(
        user_id=user_id,
        user_name=user_name,
        guild_id=guild_id,
        channel_id=channel_id,
        thread_id=None,
        trust_tier=trust_tier,
        context_key=context_key,
        trigger_discord_message_id=trigger_discord_message_id,
        activated_tools=activated or set(),
    )
