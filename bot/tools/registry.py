from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from agent.activity import ActivityReporter
from workspace import WorkspaceKey, workspace_owner_key
from providers.types import ContentPart
from tools._common import tool_error
from tools.config_spec import ToolConfigField, validate_config_spec
from trust.tiers import TrustTier

if TYPE_CHECKING:
    from agent.attachments import AttachmentRef
    from storage.usage import PaidUsageCall, UsageStore
    from usage.normalization import LLMUsageCall
    from tools.embeds import EmbedAttachment, EmbedSpec
    from tools.threads import ThreadCloseRequest, ThreadRequest

log = logging.getLogger(__name__)


# Personal chat is not in any Discord channel: `/chat` is a slash interaction and
# a DM channel id is per-recipient. Conversation scope, cancellation scope, and
# the usage ledger all key on this sentinel instead, so the two entry points land
# in one place. Never a real Discord snowflake, which are numeric.
USER_APP_SCOPE_CHANNEL_ID = "userapp"


@dataclass(frozen=True)
class TurnHandoff:
    """A tool-owned successful boundary that ends the foreground ReAct turn."""

    response_text: str
    reason: str
    task_id: str | None = None
    allowed_followup_tools: frozenset[str] = frozenset()


@dataclass
class MessageContext:
    user_id: str
    user_name: str
    # Logical data scope, NOT where the interaction physically happened. This is
    # the value every trust, policy, and data-scope decision must use: tool
    # dispatch scoping, community banks, skill scoping, catalogs. Personal chat
    # (`/chat`) is a guild-less surface, so this is None there even when the
    # slash command was invoked from inside a server; see platform_guild_id for
    # the physical location.
    guild_id: str | None
    channel_id: str
    thread_id: str | None
    trust_tier: TrustTier
    conversation_id: int | None = None
    channel_name: str = ""
    # Opaque platform actor for permission-sensitive tools. Discord entry paths
    # carry the triggering Member here so a tool does not have to rely on the
    # optional guild member cache; non-Discord/direct callers leave it unset.
    platform_member: Any | None = None
    trigger_discord_message_id: str = ""
    context_key: str = ""
    tool_event_turn_id: str = ""
    memory_writes_this_turn: int = 0
    # Logical provider operations (not model tool invocations or internal HTTP
    # retries) spent by internet_search in this user turn. Blend consumes one
    # per configured provider; a new MessageContext resets the allowance.
    internet_search_backend_calls_this_turn: int = 0
    wolfram_alpha_calls_this_turn: int = 0
    video_calls_this_turn: int = 0
    browser_calls_this_turn: int = 0
    browser_screenshots_this_turn: int = 0
    visual_renders_this_turn: int = 0
    image_gen_calls_this_turn: int = 0
    # In netns mode both surfaces draw on one physical VPN namespace lease, and
    # a rooted browser call holds it until the turn's finalizer runs. Without
    # these markers a turn that mixed browser and networked code calls would sit
    # waiting on a lease it already owns; each side checks the other and refuses
    # instead.
    browser_netns_claimed: bool = False
    networked_exec_inflight: bool = False
    # Exact managed jobs that may still own or be waiting for the shared netns.
    # The Boolean above remains the browser-facing fast path, while this set
    # prevents one completed job from clearing the conflict raised by another.
    networked_exec_job_ids: set[str] = field(default_factory=set)
    # Mirrors ConversationContext.background_task: a long-lived worker context
    # where the browser releases its turn lease after every call so managed
    # jobs can take the namespace between calls.
    background_task: bool = False
    # In-turn checklist set by the `plan` tool. Per-turn scratch that lives on
    # MessageContext (like the embed/thread/output_files rails), dies when the
    # turn returns, and is structurally outside the SQLite-persisted transcript.
    # agent/core.py reads it to paint the live activity surface and to re-append
    # the checklist to compaction notes; the tool rebinds it wholesale per call.
    plan: list[dict[str, str]] = field(default_factory=list)
    # Whether the active provider accepts image input. Set by the runtime so the
    # view_image tool can refuse cleanly instead of aborting the turn.
    images_supported: bool = False
    # Workspace images the view_image tool queued this iteration; agent/core.py
    # drains them into one synthetic untrusted user message after tool dispatch,
    # then clears the rail. In-turn only, never persisted.
    pending_view_images: list[ContentPart] = field(default_factory=list)
    view_images_this_turn: int = 0
    activated_tools: set[str] = field(default_factory=set)
    # Searchable tools the model explicitly browse_tools-loaded this turn; kept
    # separate from activated_tools so loads of channel-pinned names persist.
    explicitly_loaded_tools: set[str] = field(default_factory=set)
    # Operator denylist (guild ∪ channel blocked_tools) for this turn, sourced
    # from the on-disk fragments. dispatch() masks these as "Unknown tool" and
    # the visibility methods filter them out. Empty = nothing blocked.
    blocked_tools: frozenset[str] = frozenset()
    # Fully resolved operator config for every tool that declared a config_spec,
    # keyed by tool name (config/fragments/tool_config.py reads config/tools/<name>.md fresh
    # each turn). Defaults are already merged in, so a handler reads
    # `ctx.tool_configs.get("my_tool") or {}` and never merges anything itself;
    # the `or {}` covers bare test contexts and tools with no spec. Never
    # persisted; the fragment stays the source of truth.
    tool_configs: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    output_files: list[str] = field(default_factory=list)
    # Optional Discord attachment descriptions keyed by the exact queued path.
    # Kept parallel to output_files so ordinary files pay no metadata cost.
    output_file_descriptions: dict[str, str] = field(default_factory=dict)
    # Opaque, per-turn selectors for queued outputs whose safe display paths may
    # collide (notably script-backed skill files from separate job directories).
    # Values remain absolute internally; only the short keys are exposed to the
    # model. The monotonic counter prevents a stale selector from being reused
    # for a later attachment after removal.
    output_file_remove_ids: dict[str, str] = field(default_factory=dict)
    output_file_remove_id_counter: int = 0
    allowed_file_roots: list[str] = field(default_factory=list)
    input_parts: list[ContentPart] = field(default_factory=list)
    # Images from the message this turn replies to, as filtered upstream: an image
    # already carried by the rooted transcript is deduped away, and the reply's
    # share of the per-turn image budget can be zero, so this is "the reply images
    # that were newly attached to this turn", not every image on that message.
    # They already reach the model as their own untrusted user message
    # (agent/reply_context.py), so they are kept out of input_parts to avoid
    # sending the same bytes twice; a tool that wants to *operate* on a visible
    # image reads both rails in order.
    reply_image_parts: list[ContentPart] = field(default_factory=list)
    edit_target_image: ContentPart | None = None
    attachments: list[AttachmentRef] = field(default_factory=list)
    # Bounded, text-only copy of the context visible to the foreground model
    # immediately before it delegates to a coding task. The core rebuilds this
    # only for start_coding_task; handlers may persist it, but it contains no
    # system prompt, tool schemas, provider payloads, call arguments, or images.
    handoff_context_messages: list[dict[str, str]] = field(default_factory=list)
    activity_reporter: ActivityReporter | None = None
    # Dedicated coding workers hold the shared workspace lock for their whole
    # lifetime. Their internal workspace tools must not reacquire the non-
    # reentrant lock; ordinary turns always leave this false.
    workspace_lock_held: bool = False
    # Shared by a foreground response and its detached children. STOP sets this
    # before cancelling the root so admission-style tools can undo a boundary
    # commit instead of creating work after the cancellation sweep.
    stop_event: asyncio.Event | None = None
    # A successful durable delegation can finish the foreground turn without
    # paying for another model call. The core completes the current provider
    # tool-call envelope, persists this deterministic acknowledgement, and exits.
    terminal_handoff: TurnHandoff | None = None
    usage_store: UsageStore | None = None
    # Shared with the core ReAct accounting state. Direct model-backed tools
    # append through the awaited recorder when present so detached calls are
    # durable before their privacy lease ends; direct core callers fall back to
    # the plain sink.
    usage_sink: list[LLMUsageCall] | None = None
    record_usage_call: Callable[[LLMUsageCall], Awaitable[None]] | None = None
    embed: EmbedSpec | None = None
    embed_attachment: EmbedAttachment | None = None
    thread_request: ThreadRequest | None = None
    thread_close_request: ThreadCloseRequest | None = None
    workspace_key_override: WorkspaceKey | None = None
    personal_chat: bool = False
    # Where the interaction physically happened, independent of the logical
    # scope above. Only genuinely location-bound Discord work may read this
    # (a guild member lookup, a jump URL); it confers no authority, and a
    # boundary test keeps it out of tools/. Equal to guild_id off personal chat.
    platform_guild_id: str | None = None
    # Resource leases that must span the complete outer ReAct turn (rather than
    # one tool dispatch) register here. The core drains them exactly once in a
    # finally block, including provider errors, timeouts, max-iteration exits,
    # and task cancellation. Keys make repeated calls by one tool idempotent.
    turn_finalizers: list[Callable[[], Awaitable[None]]] = field(default_factory=list)
    turn_finalizer_keys: set[str] = field(default_factory=set)
    _turn_finalization_started: bool = field(default=False, init=False, repr=False)
    _turn_finalization_event: asyncio.Event = field(
        default_factory=asyncio.Event, init=False, repr=False
    )

    def add_turn_finalizer(
        self,
        key: str,
        callback: Callable[[], Awaitable[None]],
    ) -> bool:
        if self._turn_finalization_started:
            return False
        if key in self.turn_finalizer_keys:
            return False
        self.turn_finalizer_keys.add(key)
        self.turn_finalizers.append(callback)
        return True

    def begin_turn_finalization(self) -> list[Callable[[], Awaitable[None]]]:
        """Close finalizer registration and return callbacks to drain once."""

        if self._turn_finalization_started:
            return []
        self._turn_finalization_started = True
        self._turn_finalization_event.set()
        callbacks = list(reversed(self.turn_finalizers))
        self.turn_finalizers.clear()
        self.turn_finalizer_keys.clear()
        return callbacks

    @property
    def turn_finalization_started(self) -> bool:
        return self._turn_finalization_started

    async def wait_for_turn_finalization(self) -> None:
        await self._turn_finalization_event.wait()

    async def record_paid_usage(self, call: PaidUsageCall) -> None:
        """Durably attribute a non-LLM provider charge to this turn.

        Accounting must never take down the user-visible tool call. The vendor
        remains the authority if a local ledger write fails.
        """
        if self.usage_store is None:
            return
        try:
            await self.usage_store.record_paid_usage(
                user_id=self.user_id,
                user_name=self.user_name,
                channel_id=self.conversation_channel_id,
                guild_id=self.guild_id,
                calls=[call],
                turn_id=self.tool_event_turn_id or None,
            )
        except Exception:
            log.warning("paid usage ledger write failed", exc_info=True)

    @property
    def workspace_key(self) -> WorkspaceKey:
        """Owner key for the per-(user, guild) file workspace.

        Distinct from ``user_id`` (which stays the real Discord id used for
        memory banks, blocking, owner_only, and usage): only workspace-bound
        reads use this composite so files are isolated per community.
        """
        return self.workspace_key_override or workspace_owner_key(self.user_id, self.guild_id)

    @property
    def conversation_channel_id(self) -> str:
        return USER_APP_SCOPE_CHANNEL_ID if self.personal_chat else self.channel_id


@dataclass
class ToolEntry:
    name: str
    description: str
    parameters: dict
    handler: Callable[..., Coroutine[Any, Any, str]]
    min_tier: TrustTier
    searchable: bool = False
    skill_name: str = ""
    category: str = ""
    parameters_builder: Callable[[TrustTier], dict] | None = None
    # When True the tool is callable and visible ONLY to the configured bot
    # owner (by Discord user id), independent of trust tier. Gated at dispatch
    # time and hidden from every other caller's tool list/catalog.
    owner_only: bool = False
    # When set, the tool is callable and visible ONLY in these Discord guild ids
    # (a tool scoped to one community). None means every guild. Like
    # owner_only it is masked at dispatch (existence never leaks to other guilds)
    # and filtered from tool lists/catalog; a None ctx.guild_id (DMs) never
    # matches a guild-scoped tool. AND-ed with min_tier and owner_only.
    # Semantics are fail-closed by design: None = everywhere, a non-empty set =
    # only those guilds, and the empty set = NOWHERE (callable in no guild). The
    # skill-authoring path normalizes "no ids" to None (everywhere); the empty
    # set reaches here only from a Python caller or a parse that failed closed.
    guild_ids: frozenset[str] | None = None
    # Operator-tunable knobs this tool exposes (tools/config_spec.py). Empty (the
    # default) means the tool takes no operator config. Validated at register()
    # time and immutable, so clone_without/replace_skill_tools carry it without
    # special handling.
    config_spec: tuple[ToolConfigField, ...] = ()


class ToolRegistry:
    def __init__(self, owner_user_id: str = "") -> None:
        self._core_tools: dict[str, ToolEntry] = {}
        self._search_tools: dict[str, ToolEntry] = {}
        self._owner_user_id = owner_user_id
        self._event_loop: asyncio.AbstractEventLoop | None = None
        self._event_loop_thread_id: int | None = None

    def bind_event_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Keep live registry replacement serialized with Discord turns.

        Skill discovery and handler construction may run in a worker thread, but
        the final swap touches dictionaries read by turns on ``loop``. Binding
        lets ``replace_skill_tools`` synchronously marshal only that swap back to
        the loop while preserving the skill admin service's rollback contract.
        """

        if loop.is_closed():
            raise RuntimeError("Cannot bind the tool registry to a closed event loop")
        self._event_loop = loop
        self._event_loop_thread_id = threading.get_ident()

    def _is_owner(self, user_id: str) -> bool:
        # An empty owner id matches nobody, so owner_only tools fail closed.
        return bool(self._owner_user_id) and user_id == self._owner_user_id

    @staticmethod
    def _allowed_in_guild(entry: ToolEntry, guild_id: str | None) -> bool:
        # Fail closed: a guild-scoped tool needs a concrete guild_id that is in
        # its allowlist. A global tool (guild_ids is None) is allowed anywhere,
        # including DMs where guild_id is None.
        if entry.guild_ids is None:
            return True
        return guild_id is not None and guild_id in entry.guild_ids

    def _entry_visible(
        self,
        entry: ToolEntry,
        *,
        tier: TrustTier | None,
        user_id: str | None,
        guild_id: str | None,
        blocked: frozenset[str] | None,
        check_owner: bool = True,
    ) -> bool:
        """The single privilege predicate behind every visibility surface and dispatch.

        Skip semantics are explicit, because the public surfaces differ on
        purpose: a None ``tier`` skips the tier gate and ``check_owner=False``
        skips the owner gate (has_tool/get_searchable_entry's optional checks),
        while a None ``user_id`` with ``check_owner=True`` means "not the owner"
        (owner_only tools hidden, which is the listing surfaces' behavior). A
        None ``guild_id`` is a real value (DMs) and is always evaluated; a None
        or empty ``blocked`` means nothing is blocked.
        """
        if tier is not None and tier < entry.min_tier:
            return False
        if check_owner and entry.owner_only and not self._is_owner(user_id or ""):
            return False
        if not self._allowed_in_guild(entry, guild_id):
            return False
        return not blocked or entry.name not in blocked

    def set_owner_user_id(self, owner_user_id: str) -> None:
        """Set the owner gate after construction.

        The composition root (app/tools.py:build_runtime_tools) calls this so the
        owner id is applied even when a registry was constructed elsewhere and
        passed in. Otherwise owner_only tools would be masked for everyone.
        """
        self._owner_user_id = owner_user_id

    def register(
        self,
        name: str,
        description: str,
        parameters: dict,
        handler: Callable[..., Coroutine[Any, Any, str]],
        min_tier: TrustTier = TrustTier.MEMBER,
        searchable: bool = False,
        skill_name: str = "",
        category: str = "",
        parameters_builder: Callable[[TrustTier], dict] | None = None,
        owner_only: bool = False,
        guild_ids: frozenset[str] | None = None,
        config_spec: Sequence[ToolConfigField] = (),
    ) -> None:
        if name in self._core_tools or name in self._search_tools:
            raise ValueError(f"Tool {name!r} is already registered")

        # Validate at registration so a malformed declaration fails at boot,
        # where its author sees it, rather than in a turn.
        validated_config_spec = validate_config_spec(name, config_spec)

        entry = ToolEntry(
            name=name,
            description=description,
            parameters=parameters,
            handler=handler,
            min_tier=min_tier,
            searchable=searchable,
            skill_name=skill_name,
            category=category,
            parameters_builder=parameters_builder,
            owner_only=owner_only,
            guild_ids=guild_ids,
            config_spec=validated_config_spec,
        )
        if searchable:
            self._search_tools[name] = entry
        else:
            self._core_tools[name] = entry

    def is_registered(self, name: str) -> bool:
        """Pure name-existence check, ignoring every gate (owner/guild/tier/
        activation). For registration-time collision detection and post-reload
        verification, NOT for deciding caller visibility (use has_tool for that).
        A guild-scoped tool exists everywhere; only its dispatch/visibility is
        guild-bound, so existence checks must not be guild-filtered."""
        return name in self._core_tools or name in self._search_tools

    def registered_names(self) -> frozenset[str]:
        """Snapshot of every registered tool name, ignoring every gate.

        For the plugin loader's rollback delta (app/plugins.py): the names that
        appear between two snapshots are exactly what a failed plugin managed to
        register before raising.
        """
        return frozenset(self._core_tools) | frozenset(self._search_tools)

    def config_specs(self) -> dict[str, tuple[ToolConfigField, ...]]:
        """Every registered tool that declares operator config, name -> spec.

        Deliberately ignores tier/guild/owner/denylist gates, exactly like
        ``is_registered``: this answers "which fragments exist to read", and the
        per-turn loader would otherwise produce a different snapshot per caller.
        Dispatch-time gating is unaffected: a blocked or out-of-guild tool never
        runs, so the config it never reads costs one small file read.
        """
        return {
            name: entry.config_spec
            for pool in (self._core_tools, self._search_tools)
            for name, entry in pool.items()
            if entry.config_spec
        }

    def remove_tools(self, names: set[str]) -> None:
        for name in names:
            self._core_tools.pop(name, None)
            self._search_tools.pop(name, None)

    def clone_without(self, names: set[str]) -> ToolRegistry:
        """Return an independent registry view sharing entries except ``names``."""
        clone = ToolRegistry(owner_user_id=self._owner_user_id)
        clone._core_tools = {
            name: entry for name, entry in self._core_tools.items() if name not in names
        }
        clone._search_tools = {
            name: entry for name, entry in self._search_tools.items() if name not in names
        }
        return clone

    def clone_only(self, names: set[str]) -> ToolRegistry:
        """Return an independent least-privilege view containing only ``names``."""

        clone = ToolRegistry(owner_user_id=self._owner_user_id)
        clone._core_tools = {
            name: entry for name, entry in self._core_tools.items() if name in names
        }
        clone._search_tools = {
            name: entry for name, entry in self._search_tools.items() if name in names
        }
        return clone

    def promote_searchable(self, names: set[str]) -> None:
        """Make selected searchable entries always visible in this registry view."""

        for name in names:
            entry = self._search_tools.pop(name, None)
            if entry is not None:
                # Entries are otherwise shared between registry views; copy so
                # the source catalog retains its searchable classification.
                self._core_tools[name] = replace(entry, searchable=False)

    def get_tools_for_tier(
        self,
        tier: TrustTier,
        activated: set[str] | None = None,
        user_id: str | None = None,
        guild_id: str | None = None,
        blocked: frozenset[str] | None = None,
    ) -> list[ToolEntry]:
        active = activated or set()

        def visible(entry: ToolEntry) -> bool:
            return self._entry_visible(
                entry, tier=tier, user_id=user_id, guild_id=guild_id, blocked=blocked
            )

        core = [t for t in self._core_tools.values() if visible(t)]
        search = [t for name, t in self._search_tools.items() if name in active and visible(t)]
        return core + search

    def get_all_tools(self) -> list[ToolEntry]:
        return list(self._core_tools.values()) + list(self._search_tools.values())

    def replace_skill_tools_threadsafe(self, entries: list[ToolEntry]) -> None:
        """Run ``replace_skill_tools`` on the bound loop and wait for completion."""

        loop = self._event_loop
        if (
            loop is None
            or self._event_loop_thread_id is None
            or threading.get_ident() == self._event_loop_thread_id
        ):
            self.replace_skill_tools(entries)
            return
        if loop.is_closed() or not loop.is_running():
            raise RuntimeError("The tool registry event loop is not running")

        completed: concurrent.futures.Future[None] = concurrent.futures.Future()

        def replace_on_loop() -> None:
            try:
                self.replace_skill_tools(entries)
            except BaseException as exc:
                completed.set_exception(exc)
            else:
                completed.set_result(None)

        loop.call_soon_threadsafe(replace_on_loop)
        completed.result()

    def replace_skill_tools(self, entries: list[ToolEntry]) -> None:
        new_names: set[str] = set()
        for entry in entries:
            if not entry.skill_name:
                raise ValueError(f"Replacement entry {entry.name!r} is not skill-backed")
            if entry.name in new_names:
                raise ValueError(f"Duplicate replacement tool {entry.name!r}")
            new_names.add(entry.name)

        for pool in (self._core_tools, self._search_tools):
            for name, entry in pool.items():
                if not entry.skill_name and name in new_names:
                    raise ValueError(f"Tool {name!r} conflicts with existing core tool")

        for pool in (self._core_tools, self._search_tools):
            for name, entry in list(pool.items()):
                if entry.skill_name:
                    del pool[name]

        for entry in entries:
            if entry.searchable:
                self._search_tools[entry.name] = entry
            else:
                self._core_tools[entry.name] = entry

    def get_tool_schemas(
        self,
        tier: TrustTier,
        activated: set[str] | None = None,
        user_id: str | None = None,
        guild_id: str | None = None,
        blocked: frozenset[str] | None = None,
    ) -> list[dict]:
        schemas: list[dict] = []
        for t in self.get_tools_for_tier(tier, activated, user_id, guild_id, blocked):
            params = t.parameters_builder(tier) if t.parameters_builder else t.parameters
            schemas.append({"name": t.name, "description": t.description, "parameters": params})
        return schemas

    def has_tool(
        self,
        name: str,
        user_id: str | None = None,
        guild_id: str | None = None,
        blocked: frozenset[str] | None = None,
        tier: TrustTier | None = None,
    ) -> bool:
        entry = self._core_tools.get(name) or self._search_tools.get(name)
        if entry is None:
            return False
        return self._entry_visible(
            entry,
            tier=tier,
            user_id=user_id,
            guild_id=guild_id,
            blocked=blocked,
            # No user_id means "don't check the owner gate" here, unlike the
            # listing surfaces, where it means "not the owner".
            check_owner=user_id is not None,
        )

    def catalog(
        self,
        tier: TrustTier,
        user_id: str | None = None,
        guild_id: str | None = None,
        blocked: frozenset[str] | None = None,
    ) -> list[ToolEntry]:
        entries = [
            entry
            for entry in self._search_tools.values()
            if self._entry_visible(
                entry, tier=tier, user_id=user_id, guild_id=guild_id, blocked=blocked
            )
        ]
        return sorted(entries, key=lambda entry: entry.name)

    def get_searchable_entry(
        self,
        name: str,
        tier: TrustTier,
        guild_id: str | None = None,
        blocked: frozenset[str] | None = None,
    ) -> ToolEntry | None:
        entry = self._search_tools.get(name)
        if entry is None:
            return None
        # check_owner=False: activation lookups have never owner-gated; dispatch
        # still blocks an owner_only tool for everyone but the owner.
        if not self._entry_visible(
            entry, tier=tier, user_id=None, guild_id=guild_id, blocked=blocked, check_owner=False
        ):
            return None
        return entry

    def dispatch_gate(self, name: str, ctx: MessageContext) -> str | None:
        """Return the authoritative dispatch error, or ``None`` when allowed.

        This check has no handler side effects, so wrappers that replay or
        synthesize a result can apply the exact production boundary first.
        """
        entry = self._core_tools.get(name) or self._search_tools.get(name)
        if entry is None:
            return tool_error(f"Unknown tool: {name}")

        # The authoritative privilege boundary; the visibility filtering
        # (get_tools_for_tier/catalog/get_searchable_entry) is cosmetic. Every
        # gate (owner, guild scope, operator denylist, trust tier) is masked
        # as a missing tool so nothing about an unavailable tool (existence,
        # required tier, which gate failed) leaks, and all of them run before
        # the searchable-activation check so an unavailable searchable tool
        # never reveals itself through the different activation error.
        if not self._entry_visible(
            entry,
            tier=ctx.trust_tier,
            user_id=ctx.user_id,
            guild_id=ctx.guild_id,
            blocked=ctx.blocked_tools,
        ):
            return tool_error(f"Unknown tool: {name}")

        if name in self._search_tools and name not in ctx.activated_tools:
            return tool_error(
                f"Tool '{name}' is not available in this conversation. "
                f"Call browse_tools to see the catalog, then browse_tools with "
                f'load:["{name}"] to enable it.'
            )
        return None

    async def dispatch(self, name: str, args: dict, ctx: MessageContext) -> str:
        gate_error = self.dispatch_gate(name, ctx)
        if gate_error is not None:
            return gate_error

        entry = self._core_tools.get(name) or self._search_tools.get(name)
        assert entry is not None

        try:
            return await entry.handler(args, ctx)
        except Exception:
            log.exception("Tool %r raised an exception", name)
            return tool_error("Tool execution failed.")
