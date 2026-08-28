"""Protocol-level fakes for module unit tests.

Every fake here satisfies one runtime port from ``contracts`` with plain
Python and records what a module asked of it. Nothing imports Discord, the
database, or core runtime packages, so a module package can unit-test its own
logic with only ``kimi_agent_module_api`` installed. The integration harness
that composes real core services lives in core's ``modules.testing``.
"""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import hashlib
import uuid
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, TypeVar, overload

from pydantic_settings import BaseSettings

from kimi_agent_module_api.trust import TrustTier

from kimi_agent_module_api.contracts import (
    ALL_DISCORD_ACTIONS,
    Backoff,
    ChannelSnapshot,
    CommandSpec,
    ConfigSnapshot,
    Event,
    EventHandler,
    GuildSettingsSnapshot,
    HealthState,
    HostNotAllowed,
    HttpResponse,
    JobHandler,
    JobInfo,
    JobRun,
    MemberSnapshot,
    MessagePage,
    MessageRef,
    MessageSnapshot,
    TABLE_NAME_RE,
    MigrationContext,
    ModuleContractError,
    ModuleHealth,
    OutgoingEmbed,
    ProposalActor,
    ProposalError,
    ProposalRef,
    ProposalState,
    RoleSnapshot,
    ScopedModuleMigration,
    ServiceUnavailable,
    TrustTierName,
    UndeclaredDiscordAction,
    build_custom_id,
    validate_publish_topic,
)
from kimi_agent_module_api.tools import ModuleToolHandler
from kimi_agent_module_api import (
    BASELINE_CAPABILITIES,
    ModuleCapabilities,
    ModuleLoadContext,
)

_T = TypeVar("_T")


@dataclass(slots=True)
class _Closable:
    _on_close: Callable[[], object]
    closed: bool = False

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self._on_close()


@dataclass(frozen=True, slots=True)
class ProposedChange:
    proposal_id: str
    module_name: str
    target: str
    content: str
    summary: str
    actor: ProposalActor
    expected_revision: str | None


class FakeProposals:
    """Actor-scoped, module-bound fragment proposal fake."""

    def __init__(
        self,
        module_name: str,
        documents: Mapping[str, str] | None = None,
        *,
        target_guilds: Mapping[str, str] | None = None,
    ) -> None:
        self.module_name = module_name
        self.documents = dict(documents or {})
        self.target_guilds = dict(target_guilds or {})
        self.changes: list[ProposedChange] = []
        self.refs: dict[str, ProposalRef] = {}
        self._proposal_guilds: dict[str, str] = {}

    @staticmethod
    def _revision(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def _require_guild(self, target: str, actor: ProposalActor) -> str:
        actor_guild = str(actor.guild_id or "")
        if not actor_guild.isdecimal() or int(actor_guild) <= 0:
            raise ProposalError("proposal actor must belong to a guild")
        target_guild = self.target_guilds.get(target)
        if target_guild is None and target.startswith("guild:"):
            target_guild = target.split(":", 2)[1]
        if target_guild is None:
            raise ProposalError(f"fake has no guild mapping for {target!r}")
        if target_guild != actor_guild:
            raise ProposalError("proposal target must belong to the actor's guild")
        return actor_guild

    async def snapshot(self, target: str, *, actor: ProposalActor) -> ConfigSnapshot:
        self._require_guild(target, actor)
        content = self.documents.get(target, "")
        return ConfigSnapshot(target, self._revision(content), content)

    async def propose(
        self,
        *,
        target: str,
        content: str,
        summary: str,
        actor: ProposalActor,
        expected_revision: str | None = None,
    ) -> ProposalRef:
        guild_id = self._require_guild(target, actor)
        current = self.documents.get(target, "")
        if expected_revision is not None and expected_revision != self._revision(current):
            raise ProposalError("configuration changed since it was inspected")
        proposal_id = uuid.uuid4().hex
        change = ProposedChange(
            proposal_id,
            self.module_name,
            target,
            content,
            summary,
            actor,
            expected_revision,
        )
        ref = ProposalRef(proposal_id, target, "pending")
        self.changes.append(change)
        self.refs[proposal_id] = ref
        self._proposal_guilds[proposal_id] = guild_id
        return ref

    async def get(self, proposal_id: str, *, actor: ProposalActor) -> ProposalRef | None:
        actor_guild = str(actor.guild_id or "")
        if self._proposal_guilds.get(proposal_id) != actor_guild:
            return None
        return self.refs.get(proposal_id)

    def decide(
        self,
        proposal_id: str,
        state: ProposalState,
        decided_by: str,
        decision_reason: str = "",
    ) -> ProposalRef:
        current = self.refs[proposal_id]
        updated = ProposalRef(
            current.proposal_id,
            current.target,
            state,
            current.message,
            decided_by,
            decision_reason,
        )
        self.refs[proposal_id] = updated
        return updated


class FakeEvents:
    """Records publishes; delivers to subscribers only when ``deliver`` is awaited."""

    def __init__(self, module_name: str) -> None:
        self.module_name = module_name
        self.published: list[Event] = []
        self._subscriptions: list[tuple[str, EventHandler]] = []
        self._clock = 0.0

    def publish(self, topic: str, payload: Any) -> None:
        validate_publish_topic(self.module_name, topic)
        self._clock += 1.0
        self.published.append(Event(topic, payload, self.module_name, self._clock))

    def subscribe(self, pattern: str, handler: EventHandler) -> _Closable:
        entry = (pattern, handler)
        self._subscriptions.append(entry)
        return _Closable(lambda: self._subscriptions.remove(entry))

    async def deliver(self, topic: str, payload: Any, *, source_module: str = "core") -> int:
        """Push one event through matching subscribers; returns handler count."""
        self._clock += 1.0
        event = Event(topic, payload, source_module, self._clock)
        count = 0
        for pattern, handler in list(self._subscriptions):
            if fnmatch.fnmatchcase(topic, pattern):
                await handler(event)
                count += 1
        return count

    @property
    def subscriptions(self) -> tuple[str, ...]:
        return tuple(pattern for pattern, _ in self._subscriptions)


@dataclass(slots=True)
class _FakeJob:
    key: str
    handler: str
    run_at: float
    interval: float | None
    payload: Mapping[str, Any]
    backoff: Backoff = field(default_factory=Backoff)
    attempt: int = 0
    last_error: str | None = None


def _retry_delay(backoff: Backoff, attempt: int) -> float:
    return min(
        backoff.max_seconds, backoff.base_seconds * backoff.multiplier ** max(0, attempt - 1)
    )


class FakeScheduler:
    """Jobs run only when the test advances time with ``run_due``.

    Settlement follows the host: a successful one-shot job is deleted, a
    successful periodic job reschedules from completion, and a failed job of
    either kind is kept and retried after its ``Backoff`` delay with the
    attempt count preserved. Jitter is ignored so reschedules are exact.
    """

    def __init__(self) -> None:
        self.handlers: dict[str, JobHandler] = {}
        self.jobs: dict[str, _FakeJob] = {}
        self.runs: list[JobRun] = []

    def register(self, handler_name: str, handler: JobHandler) -> None:
        self.handlers[handler_name] = handler

    async def run_at(
        self, key: str, when: float, handler_name: str, payload: Mapping[str, Any] | None = None
    ) -> None:
        self.jobs[key] = _FakeJob(key, handler_name, when, None, dict(payload or {}))

    async def run_every(
        self,
        key: str,
        interval_seconds: float,
        handler_name: str,
        payload: Mapping[str, Any] | None = None,
        *,
        jitter_seconds: float = 0.0,
        backoff: Backoff | None = None,
    ) -> None:
        self.jobs[key] = _FakeJob(
            key,
            handler_name,
            0.0,
            interval_seconds,
            dict(payload or {}),
            backoff=backoff or Backoff(),
        )

    async def cancel(self, key: str) -> bool:
        return self.jobs.pop(key, None) is not None

    async def list(self) -> Sequence[JobInfo]:
        return [
            JobInfo(job.key, job.handler, job.run_at, job.interval, job.attempt, job.last_error)
            for job in self.jobs.values()
        ]

    async def run_due(self, now: float) -> int:
        """Run every job scheduled at or before ``now``; returns how many ran."""
        ran = 0
        for job in list(self.jobs.values()):
            if job.run_at > now or job.key not in self.jobs:
                continue
            handler = self.handlers.get(job.handler)
            if handler is None:
                job.last_error = f"no handler {job.handler!r}"
                continue
            job.attempt += 1
            run = JobRun(f"fake-{job.key}", job.key, job.payload, job.attempt, job.run_at)
            self.runs.append(run)
            try:
                await handler(run)
            except Exception as exc:
                job.last_error = repr(exc)
                job.run_at = now + _retry_delay(job.backoff, job.attempt)
            else:
                job.last_error = None
                job.attempt = 0
                if job.interval is None:
                    self.jobs.pop(job.key, None)
                else:
                    job.run_at = now + job.interval
            ran += 1
        return ran


_HEALTH_ORDER: dict[str, int] = {"healthy": 0, "starting": 1, "degraded": 2, "failed": 3}


class FakeHealth:
    """Records every report.

    ``history`` holds every call as ``(key, health)``. ``current`` is the latest
    unkeyed report, ``keyed`` the latest live report per key, and ``state`` the
    worst across both, which is what the host would show for the module.
    """

    def __init__(self) -> None:
        self.history: list[tuple[str | None, ModuleHealth]] = []
        self.reports: list[ModuleHealth] = []
        self.keyed: dict[str, ModuleHealth] = {}

    def report(
        self,
        state: HealthState,
        detail: str = "",
        metrics: Mapping[str, float] | None = None,
        *,
        key: str | None = None,
    ) -> None:
        health = ModuleHealth(state, detail, dict(metrics or {}), float(len(self.history)))
        self.history.append((key, health))
        if key is not None:
            if state == "healthy" and not detail and not metrics:
                self.keyed.pop(key, None)
            else:
                self.keyed[key] = health
            return
        self.reports.append(health)

    @property
    def current(self) -> ModuleHealth | None:
        return self.reports[-1] if self.reports else None

    @property
    def state(self) -> HealthState:
        candidates = [*self.keyed.values(), *([self.current] if self.current else [])]
        if not candidates:
            return "healthy"
        return max((health.state for health in candidates), key=_HEALTH_ORDER.__getitem__)


@dataclass(frozen=True, slots=True)
class DiscordCall:
    action: str
    args: tuple[Any, ...]
    kwargs: Mapping[str, Any]


class FakeDiscordActions:
    """Enforces the module's declared actions and records every call."""

    def __init__(self, module_name: str, declared: frozenset[str] | None = None) -> None:
        self.module_name = module_name
        self.declared = ALL_DISCORD_ACTIONS if declared is None else declared
        self.calls: list[DiscordCall] = []
        self.messages: dict[MessageRef, MessageSnapshot] = {}
        self.members: dict[tuple[int, int], MemberSnapshot] = {}
        self.channels: dict[tuple[int, int], ChannelSnapshot] = {}
        self.histories: dict[tuple[int, int], list[MessageSnapshot]] = {}
        self.pins: dict[tuple[int, int], tuple[MessageSnapshot, ...]] = {}
        self.public_threads: dict[tuple[int, int], tuple[ChannelSnapshot, ...]] = {}
        self.roles: dict[int, tuple[RoleSnapshot, ...]] = {}
        self.channel_access: dict[tuple[int, int, int], bool] = {}
        self._next_message_id = 1000

    def _record(self, action: str, *args: Any, **kwargs: Any) -> None:
        if action not in self.declared:
            raise UndeclaredDiscordAction(self.module_name, action)
        self.calls.append(DiscordCall(action, args, kwargs))

    def calls_for(self, action: str) -> list[DiscordCall]:
        return [call for call in self.calls if call.action == action]

    async def send_message(
        self,
        channel_id: int,
        content: str | None = None,
        *,
        embed: OutgoingEmbed | None = None,
        reply_to: MessageRef | None = None,
        components: Sequence[Any] = (),
    ) -> MessageRef:
        self._record(
            "send_message",
            channel_id,
            content,
            embed=embed,
            reply_to=reply_to,
            components=components,
        )
        self._next_message_id += 1
        return MessageRef(0, channel_id, self._next_message_id)

    async def send_dm(
        self, user_id: int, content: str, *, embed: OutgoingEmbed | None = None
    ) -> bool:
        self._record("send_dm", user_id, content, embed=embed)
        return True

    async def edit_message(
        self, ref: MessageRef, content: str | None = None, *, embed: OutgoingEmbed | None = None
    ) -> None:
        self._record("edit_message", ref, content, embed=embed)

    async def delete_message(self, ref: MessageRef, *, reason: str = "") -> None:
        self._record("delete_message", ref, reason=reason)
        self.messages.pop(ref, None)

    async def ban(
        self,
        guild_id: int,
        user_id: int,
        *,
        actor_id: int | None,
        reason: str,
        delete_message_seconds: int = 0,
    ) -> None:
        self._record(
            "ban",
            guild_id,
            user_id,
            actor_id=actor_id,
            reason=reason,
            delete_message_seconds=delete_message_seconds,
        )

    async def kick(self, guild_id: int, user_id: int, *, actor_id: int | None, reason: str) -> None:
        self._record("kick", guild_id, user_id, actor_id=actor_id, reason=reason)

    async def timeout(
        self,
        guild_id: int,
        user_id: int,
        *,
        actor_id: int | None,
        reason: str,
        duration_seconds: int,
    ) -> None:
        self._record(
            "timeout",
            guild_id,
            user_id,
            actor_id=actor_id,
            reason=reason,
            duration_seconds=duration_seconds,
        )

    async def fetch_message(self, ref: MessageRef) -> MessageSnapshot | None:
        self._record("fetch_message", ref)
        return self.messages.get(ref)

    async def fetch_member(self, guild_id: int, user_id: int) -> MemberSnapshot | None:
        self._record("fetch_member", guild_id, user_id)
        return self.members.get((guild_id, user_id))

    async def fetch_channel(self, guild_id: int, channel_id: int) -> ChannelSnapshot | None:
        self._record("fetch_channel", guild_id, channel_id)
        return self.channels.get((guild_id, channel_id))

    async def fetch_messages(
        self,
        guild_id: int,
        channel_id: int,
        *,
        after_message_id: int | None = None,
        before_message_id: int | None = None,
        limit: int = 100,
    ) -> MessagePage:
        self._record(
            "fetch_messages",
            guild_id,
            channel_id,
            after_message_id=after_message_id,
            before_message_id=before_message_id,
            limit=limit,
        )
        messages = sorted(
            self.histories.get((guild_id, channel_id), ()),
            key=lambda message: message.ref.message_id,
        )
        if after_message_id is not None:
            candidates = [
                message for message in messages if message.ref.message_id > after_message_id
            ]
            selected = candidates[:limit]
            cursor = selected[-1].ref.message_id if selected else None
        else:
            candidates = [
                message
                for message in messages
                if before_message_id is None or message.ref.message_id < before_message_id
            ]
            selected = candidates[-limit:]
            cursor = selected[0].ref.message_id if selected else None
        return MessagePage(tuple(selected), cursor, len(candidates) > len(selected))

    async def fetch_pins(self, guild_id: int, channel_id: int) -> tuple[MessageSnapshot, ...]:
        self._record("fetch_pins", guild_id, channel_id)
        return self.pins.get((guild_id, channel_id), ())

    async def fetch_public_threads(
        self, guild_id: int, parent_channel_id: int
    ) -> tuple[ChannelSnapshot, ...]:
        self._record("fetch_public_threads", guild_id, parent_channel_id)
        return self.public_threads.get((guild_id, parent_channel_id), ())

    async def fetch_roles(self, guild_id: int) -> tuple[RoleSnapshot, ...]:
        self._record("fetch_roles", guild_id)
        return self.roles.get(guild_id, ())

    async def can_view_channel(self, guild_id: int, user_id: int, channel_id: int) -> bool:
        self._record("can_view_channel", guild_id, user_id, channel_id)
        return self.channel_access.get((guild_id, user_id, channel_id), False)


@dataclass(slots=True)
class FakeResponse:
    content: str | None
    embed: OutgoingEmbed | None
    ephemeral: bool
    components: tuple[Any, ...]
    kind: str


class FakeInteraction:
    def __init__(
        self,
        *,
        guild_id: int = 1,
        channel_id: int = 2,
        user_id: int = 3,
        options: Mapping[str, Any] | None = None,
        custom_id: str | None = None,
        values: Sequence[str] = (),
        guild_name: str | None = "Test Guild",
        message: MessageRef | None = None,
    ) -> None:
        self._message = message
        self._guild_name = guild_name
        self._guild_id = guild_id
        self._channel_id = channel_id
        self._user_id = user_id
        self._options = dict(options or {})
        self._custom_id = custom_id
        self._values = tuple(values)
        self.responses: list[FakeResponse] = []
        self.deferred: bool | None = None

    @property
    def guild_id(self) -> int:
        return self._guild_id

    @property
    def channel_id(self) -> int:
        return self._channel_id

    @property
    def user_id(self) -> int:
        return self._user_id

    @property
    def guild_name(self) -> str | None:
        return self._guild_name

    @property
    def options(self) -> Mapping[str, Any]:
        return self._options

    @property
    def custom_id(self) -> str | None:
        return self._custom_id

    @property
    def values(self) -> tuple[str, ...]:
        return self._values

    @property
    def message(self) -> MessageRef | None:
        return self._message

    async def respond(
        self,
        content: str | None = None,
        *,
        embed: OutgoingEmbed | None = None,
        ephemeral: bool = False,
        components: Sequence[Any] = (),
    ) -> None:
        self.responses.append(FakeResponse(content, embed, ephemeral, tuple(components), "respond"))

    async def defer(self, *, ephemeral: bool = False) -> None:
        self.deferred = ephemeral

    async def edit_original(
        self,
        content: str | None = None,
        *,
        embed: OutgoingEmbed | None = None,
        components: Sequence[Any] = (),
    ) -> None:
        self.responses.append(FakeResponse(content, embed, False, tuple(components), "edit"))

    async def follow_up(
        self, content: str, *, embed: OutgoingEmbed | None = None, ephemeral: bool = False
    ) -> None:
        self.responses.append(FakeResponse(content, embed, ephemeral, (), "follow_up"))

    @property
    def last(self) -> FakeResponse:
        return self.responses[-1]


class FakeInteractions:
    """Records command and component registrations; tests invoke handlers directly."""

    def __init__(self, module_name: str) -> None:
        self.module_name = module_name
        self.commands: dict[str, tuple[CommandSpec, Callable[..., Any]]] = {}
        self.components: dict[tuple[str, str], Callable[..., Any]] = {}
        self.component_min_tiers: dict[tuple[str, str], TrustTierName] = {}
        self.autocompletes: dict[str, Callable[..., Any]] = {}

    def add_command(
        self,
        spec: CommandSpec,
        handler: Callable[..., Any],
        *,
        autocomplete: Callable[..., Any] | None = None,
    ) -> _Closable:
        qualified = f"{spec.group}.{spec.name}" if spec.group else spec.name
        self.commands[qualified] = (spec, handler)
        if autocomplete is not None:
            self.autocompletes[qualified] = autocomplete
        return _Closable(
            lambda: (self.commands.pop(qualified, None), self.autocompletes.pop(qualified, None))
        )

    def register_component(
        self,
        kind: str,
        key: str,
        handler: Callable[..., Any],
        *,
        expires_after_seconds: float | None = None,
        min_tier: TrustTierName = "member",
    ) -> _Closable:
        self.components[(kind, key)] = handler
        self.component_min_tiers[(kind, key)] = min_tier
        return _Closable(
            lambda: (
                self.components.pop((kind, key), None),
                self.component_min_tiers.pop((kind, key), None),
            )
        )

    def custom_id(self, key: str, *parts: str) -> str:
        return build_custom_id(self.module_name, key, *parts)


class FakeGuildSettings:
    def __init__(
        self, values: Mapping[int, Mapping[str, Any]] | None = None, *, enabled: bool = True
    ) -> None:
        self.values: dict[int, dict[str, Any]] = {gid: dict(v) for gid, v in (values or {}).items()}
        self.enabled = enabled
        self.errors: dict[int, tuple[str, ...]] = {}
        self._callbacks: list[Callable[[int], None]] = []

    def guild_ids(self) -> tuple[int, ...]:
        return tuple(sorted(self.values))

    def get(self, guild_id: int) -> GuildSettingsSnapshot:
        errors = self.errors.get(guild_id, ())
        return GuildSettingsSnapshot(
            self.values.get(guild_id, {}), not errors, errors, f"rev-{guild_id}"
        )

    def is_enabled(self, guild_id: int) -> bool:
        return self.enabled and not self.errors.get(guild_id)

    def on_change(self, callback: Callable[[int], None]) -> _Closable:
        self._callbacks.append(callback)
        return _Closable(lambda: self._callbacks.remove(callback))

    def set(self, guild_id: int, **values: Any) -> None:
        self.values.setdefault(guild_id, {}).update(values)
        for callback in list(self._callbacks):
            callback(guild_id)


type FakeRoute = Callable[[str, Mapping[str, str]], HttpResponse]


class FakeHttp:
    """Routes are matched by URL prefix; unmatched hosts raise ``HostNotAllowed``."""

    def __init__(
        self, routes: Mapping[str, HttpResponse | bytes | FakeRoute] | None = None
    ) -> None:
        self.routes: dict[str, HttpResponse | bytes | FakeRoute] = dict(routes or {})
        self.requests: list[tuple[str, str, Mapping[str, str]]] = []

    def _resolve(self, method: str, url: str, headers: Mapping[str, str] | None) -> HttpResponse:
        sent = dict(headers or {})
        self.requests.append((method, url, sent))
        for prefix, route in self.routes.items():
            if url.startswith(prefix):
                if callable(route):
                    return route(url, sent)
                if isinstance(route, bytes):
                    return HttpResponse(200, {}, route)
                return route
        raise HostNotAllowed(f"no fake route for {url!r}")

    async def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout_seconds: float = 20.0,
        max_bytes: int = 8 * 1024 * 1024,
    ) -> HttpResponse:
        return self._resolve("GET", url, headers)

    async def post_json(
        self,
        url: str,
        payload: Any,
        *,
        headers: Mapping[str, str] | None = None,
        timeout_seconds: float = 20.0,
        max_bytes: int = 8 * 1024 * 1024,
    ) -> HttpResponse:
        return self._resolve("POST", url, headers)

    async def download(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout_seconds: float = 30.0,
        max_bytes: int = 8 * 1024 * 1024,
    ) -> AsyncIterator[bytes]:
        response = self._resolve("GET", url, headers)
        yield response.body


@dataclass(slots=True)
class _FakeProvided:
    implementation: object
    alive: bool = True


class _FakeServiceProxy:
    """Forwards attributes like the host proxy; dead for good once its registration closes."""

    __slots__ = ("_key", "_provided")

    def __init__(self, provided: _FakeProvided, key: tuple[str, int]) -> None:
        self._provided = provided
        self._key = key

    def __getattr__(self, attribute: str) -> Any:
        if not self._provided.alive:
            name, version = self._key
            raise ServiceUnavailable(f"service {name}@{version} closed")
        return getattr(self._provided.implementation, attribute)


class FakeServiceRegistry:
    def __init__(self) -> None:
        self.provided: dict[tuple[str, int], object] = {}
        self._records: dict[tuple[str, int], _FakeProvided] = {}

    def provide(self, name: str, version: int, implementation: object) -> _Closable:
        key = (name, version)
        record = _FakeProvided(implementation)
        self.provided[key] = implementation
        self._records[key] = record

        def close() -> None:
            # Like the host, a re-provided service is a new object; proxies
            # handed out for this one stay closed.
            record.alive = False
            if self._records.get(key) is record:
                self._records.pop(key, None)
                self.provided.pop(key, None)

        return _Closable(close)

    @overload
    def get(self, name: str, version: int) -> object: ...

    @overload
    def get(self, name: str, version: int, type_: type[_T]) -> _T: ...

    def get(self, name: str, version: int, type_: type[_T] | None = None) -> object:
        try:
            record = self._records[(name, version)]
        except KeyError as exc:
            raise ServiceUnavailable(f"service {name}@{version} is not provided") from exc
        if type_ is not None and not isinstance(record.implementation, type_):
            raise TypeError(f"service {name}@{version} is not a {type_.__name__}")
        # A proxy, as in the host: attribute access only, dead after close.
        return _FakeServiceProxy(record, (name, version))


class FakeTrust:
    def __init__(
        self,
        tiers: Mapping[tuple[int, int], TrustTierName] | None = None,
        *,
        default: TrustTierName = "member",
    ) -> None:
        self.tiers: dict[tuple[int, int], TrustTierName] = dict(tiers or {})
        self.default = default

    async def tier(self, guild_id: int, user_id: int) -> TrustTierName:
        return self.tiers.get((guild_id, user_id), self.default)


class MemoryStorage:
    """``ModuleStorage`` over one in-memory SQLite connection.

    Requires the ``testing`` extra (``kimi-agent-module-api[testing]``), which
    brings ``aiosqlite``. Mirrors the host's guarantees: ``table()`` returns
    the quoted ``"<module>_<name>"``, reads go through ``connection``, and
    ``write_transaction()`` serializes writers, commits on success, and rolls
    back on error. Use ``migrate()`` to apply a module's ``scoped_migrations``.

    Typical use::

        async with MemoryStorage.open("my_module") as storage:
            await storage.migrate(MyModule.scoped_migrations)
            ...
    """

    def __init__(self, connection: Any, module_name: str) -> None:
        self._connection = connection
        self._prefix = module_name.replace("-", "_")
        self._write_lock = asyncio.Lock()

    @classmethod
    @contextlib.asynccontextmanager
    async def open(cls, module_name: str) -> AsyncIterator[MemoryStorage]:
        import aiosqlite  # optional dependency: the ``testing`` extra

        async with aiosqlite.connect(":memory:") as connection:
            yield cls(connection, module_name)

    @property
    def connection(self) -> Any:
        return self._connection

    def table(self, name: str) -> str:
        if not TABLE_NAME_RE.match(name):
            raise ModuleContractError(f"table name {name!r} is not a valid identifier")
        return f'"{self._prefix}_{name}"'

    @contextlib.asynccontextmanager
    async def _transaction(self) -> AsyncIterator[Any]:
        async with self._write_lock:
            try:
                yield self._connection
            except BaseException:
                await self._connection.rollback()
                raise
            await self._connection.commit()

    def write_transaction(self) -> Any:
        return self._transaction()

    async def migrate(self, migrations: Sequence[ScopedModuleMigration]) -> None:
        for _name, migration in migrations:
            await migration(MigrationContext(connection=self._connection, table=self.table))
        await self._connection.commit()


@dataclass(slots=True)
class RecordedTool:
    description: str
    parameters: dict[str, Any]
    handler: ModuleToolHandler
    min_tier: TrustTier
    searchable: bool
    owner_only: bool
    guild_only: bool
    guild_ids: frozenset[int] | None


class RecordingToolRegistry:
    """Satisfies ``ModuleToolRegistry`` and remembers every registration."""

    def __init__(self) -> None:
        self.tools: dict[str, RecordedTool] = {}

    def register(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        handler: ModuleToolHandler,
        *,
        min_tier: TrustTier = TrustTier.MEMBER,
        searchable: bool = False,
        owner_only: bool = False,
        guild_only: bool = True,
        guild_ids: frozenset[int] | None = None,
    ) -> None:
        self.tools[name] = RecordedTool(
            description,
            parameters,
            handler,
            min_tier,
            searchable,
            owner_only,
            guild_only,
            guild_ids,
        )


@dataclass(slots=True)
class LoadContextRecorder:
    """What a ``load_context`` captured from ``create()``."""

    registry: RecordingToolRegistry
    labels: dict[str, str]
    surfaces: dict[str, tuple[str, ...]]


def load_context(
    settings: BaseSettings | None,
    *,
    capabilities: ModuleCapabilities | None = None,
    registry: RecordingToolRegistry | None = None,
) -> tuple[ModuleLoadContext, LoadContextRecorder]:
    """Build a ``ModuleLoadContext`` for calling ``ModuleSpec.create`` in a test."""
    recorder = LoadContextRecorder(registry or RecordingToolRegistry(), {}, {})

    def declare(surface: str, names: Sequence[str]) -> None:
        recorder.surfaces[surface] = tuple(names)

    context = ModuleLoadContext(
        capabilities=capabilities or ModuleCapabilities(BASELINE_CAPABILITIES, False, False),
        registry=recorder.registry,
        module_settings=settings,
        label_sink=recorder.labels.update,
        surface_sink=declare,
    )
    return context, recorder


__all__ = [
    "DiscordCall",
    "FakeDiscordActions",
    "FakeEvents",
    "FakeGuildSettings",
    "FakeHealth",
    "FakeHttp",
    "FakeInteraction",
    "FakeInteractions",
    "FakeProposals",
    "FakeResponse",
    "FakeScheduler",
    "FakeServiceRegistry",
    "FakeTrust",
    "LoadContextRecorder",
    "MemoryStorage",
    "ProposedChange",
    "RecordedTool",
    "RecordingToolRegistry",
    "load_context",
]
