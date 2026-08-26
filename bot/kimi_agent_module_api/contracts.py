"""Module API contracts: declarations, service ports, and validation rules.

Everything here is a shape or a pure rule. Core implements the Protocols in
``modules/``; external packages import only this module and its siblings. This
file must stay free of Discord SDK, database, and core runtime imports so a
module's declarations can be validated without booting anything.

Modules are trusted, in-process code. Declarations are audited through the
owner manifest and enforced through the ports below; they are not a sandbox.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class ModuleContractError(ValueError):
    """A module declaration or call violates the module contract."""


class UndeclaredDiscordAction(ModuleContractError):
    def __init__(self, module_name: str, action: str) -> None:
        super().__init__(f"module {module_name!r} did not declare Discord action {action!r}")
        self.module_name = module_name
        self.action = action


class EventTopicError(ModuleContractError):
    pass


class HostNotAllowed(ModuleContractError):
    pass


class ResponseTooLarge(ModuleContractError):
    pass


class ServiceUnavailable(RuntimeError):
    """Raised through a service proxy after its provider module closed."""


# --------------------------------------------------------------------------
# Declarations carried on ModuleSpec
# --------------------------------------------------------------------------

type DiscordAction = Literal[
    "send_message",
    "send_dm",
    "edit_message",
    "delete_message",
    "ban",
    "kick",
    "timeout",
    "fetch_message",
    "fetch_member",
]
ALL_DISCORD_ACTIONS: frozenset[str] = frozenset(
    {
        "send_message",
        "send_dm",
        "edit_message",
        "delete_message",
        "ban",
        "kick",
        "timeout",
        "fetch_message",
        "fetch_member",
    }
)
# Actions that act on a member and therefore run the core target policy.
TARGETED_DISCORD_ACTIONS: frozenset[str] = frozenset({"ban", "kick", "timeout"})

type NetworkPolicy = Literal["public", "private"]

_HOST_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9-]{1,63}$")
_SETTING_REF_RE = re.compile(r"^\$\{([a-z][a-z0-9_]{0,63})\}$")
DISCORD_CDN_TOKEN = "discord-cdn"
DISCORD_CDN_HOSTS: frozenset[str] = frozenset({"cdn.discordapp.com", "media.discordapp.net"})


@dataclass(frozen=True, slots=True)
class HttpHostRule:
    """One outbound destination a module declares.

    ``host`` is an exact lowercase hostname, the literal ``discord-cdn`` token,
    or ``${setting_name}`` resolved from the module's prepared settings at load.
    ``private`` permits an exact non-public address only for that host; it never
    widens to a network range. Cloud metadata endpoints stay blocked regardless.
    """

    host: str
    schemes: tuple[str, ...] = ("https",)
    ports: tuple[int, ...] = ()
    network: NetworkPolicy = "public"

    @property
    def setting_name(self) -> str | None:
        match = _SETTING_REF_RE.match(self.host)
        return match.group(1) if match else None

    @property
    def is_discord_cdn(self) -> bool:
        return self.host == DISCORD_CDN_TOKEN


@dataclass(frozen=True, slots=True)
class ModulePermissions:
    discord_actions: frozenset[str] = frozenset()
    event_topics: tuple[str, ...] = ()
    http_hosts: tuple[HttpHostRule, ...] = ()
    override_target_policy: bool = False
    raw_bot: bool = False
    raw_storage: bool = False


@dataclass(frozen=True, slots=True)
class ServiceDeclaration:
    name: str
    version: int


@dataclass(frozen=True, slots=True)
class ServiceRequirement:
    name: str
    version: int
    provider: str


type GuildSettingKind = Literal["int", "id", "id_list", "str", "str_list", "enum", "bool"]
type InvalidPolicy = Literal["disable_module", "disable_guild"]


@dataclass(frozen=True, slots=True)
class GuildSettingField:
    name: str
    kind: GuildSettingKind
    required: bool = False
    default: Any = None
    choices: tuple[str, ...] = ()
    help: str = ""


@dataclass(frozen=True, slots=True)
class GuildSettingsSchema:
    fields: tuple[GuildSettingField, ...]
    invalid_policy: InvalidPolicy = "disable_guild"
    validate: Callable[[Mapping[str, Any]], Sequence[str]] | None = None


# --------------------------------------------------------------------------
# Naming rules shared by declarations and runtime ports
# --------------------------------------------------------------------------

_MODULE_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_TOPIC_SEGMENT_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SERVICE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")
_SETTING_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
CORE_TOPIC_PREFIX = "discord"
CUSTOM_ID_PREFIX = "m"
CUSTOM_ID_MAX_LENGTH = 100


def table_prefix(module_name: str) -> str:
    return module_name.replace("-", "_")


def validate_module_name(name: str) -> None:
    if not _MODULE_NAME_RE.match(name):
        raise ModuleContractError(f"invalid module name {name!r}")


def split_topic(topic: str, *, allow_wildcard: bool = False) -> tuple[str, str]:
    """Split ``<namespace>.<name>``; ``<namespace>.*`` only where patterns are legal."""
    namespace, sep, name = topic.partition(".")
    name_ok = _TOPIC_SEGMENT_RE.match(name) or (allow_wildcard and name == "*")
    if not sep or not _TOPIC_SEGMENT_RE.match(namespace) or not name_ok:
        raise EventTopicError(f"invalid event topic {topic!r}; expected '<namespace>.<name>'")
    return namespace, name


def validate_publish_topic(module_name: str, topic: str) -> None:
    namespace, _ = split_topic(topic)
    if namespace != table_prefix(module_name):
        raise EventTopicError(
            f"module {module_name!r} may only publish under {table_prefix(module_name)!r}.*"
        )


def validate_subscription(module_name: str, permissions: ModulePermissions, pattern: str) -> None:
    """A module may always hear itself; other namespaces need a declaration.

    ``pattern`` is a topic or ``<namespace>.*``. Declared topics use the same
    forms, so a subscription must be covered by an equal or wider declaration.
    """
    namespace, name = split_topic(pattern, allow_wildcard=True)
    if namespace == table_prefix(module_name):
        return
    for declared in permissions.event_topics:
        declared_namespace, declared_name = split_topic(declared, allow_wildcard=True)
        if declared_namespace != namespace:
            continue
        if declared_name == "*" or declared_name == name:
            return
    raise EventTopicError(f"module {module_name!r} did not declare event topic {pattern!r}")


def build_custom_id(module_name: str, key: str, *parts: str) -> str:
    if not _TOPIC_SEGMENT_RE.match(key):
        raise ModuleContractError(f"invalid component key {key!r}")
    for part in parts:
        if ":" in part:
            raise ModuleContractError("custom_id parts may not contain ':'")
    custom_id = ":".join((CUSTOM_ID_PREFIX, module_name, key, *parts))
    if len(custom_id) > CUSTOM_ID_MAX_LENGTH:
        raise ModuleContractError(f"custom_id exceeds {CUSTOM_ID_MAX_LENGTH} characters")
    return custom_id


def parse_custom_id(custom_id: str) -> tuple[str, str, tuple[str, ...]] | None:
    """Return (module_name, key, parts) for a module-owned ID, else None."""
    pieces = custom_id.split(":")
    if len(pieces) < 3 or pieces[0] != CUSTOM_ID_PREFIX:
        return None
    return pieces[1], pieces[2], tuple(pieces[3:])


def validate_host_rule(rule: HttpHostRule) -> None:
    if rule.is_discord_cdn or rule.setting_name is not None:
        pass
    elif not _HOST_RE.match(rule.host):
        raise ModuleContractError(f"invalid HTTP host {rule.host!r}; wildcards are not supported")
    if rule.is_discord_cdn and rule.network != "public":
        raise ModuleContractError("discord-cdn is always public")
    if not rule.schemes or any(scheme not in ("http", "https") for scheme in rule.schemes):
        raise ModuleContractError(f"invalid schemes {rule.schemes!r} for host {rule.host!r}")
    if any(port <= 0 or port > 65535 for port in rule.ports):
        raise ModuleContractError(f"invalid ports {rule.ports!r} for host {rule.host!r}")


def validate_permissions(module_name: str, permissions: ModulePermissions) -> None:
    unknown = permissions.discord_actions - ALL_DISCORD_ACTIONS
    if unknown:
        raise ModuleContractError(
            f"module {module_name!r} declares unknown Discord actions {sorted(unknown)!r}"
        )
    if permissions.override_target_policy and not (
        permissions.discord_actions & TARGETED_DISCORD_ACTIONS
    ):
        raise ModuleContractError(
            f"module {module_name!r} overrides the target policy without a targeted action"
        )
    for topic in permissions.event_topics:
        namespace, _ = split_topic(topic, allow_wildcard=True)
        if namespace == table_prefix(module_name):
            raise EventTopicError(
                f"module {module_name!r} need not declare its own topic {topic!r}"
            )
    for rule in permissions.http_hosts:
        validate_host_rule(rule)


def validate_services(
    module_name: str,
    dependencies: Sequence[str],
    provides: Sequence[ServiceDeclaration],
    consumes: Sequence[ServiceRequirement],
) -> None:
    seen: set[tuple[str, int]] = set()
    for declaration in provides:
        if not _SERVICE_NAME_RE.match(declaration.name) or declaration.version < 1:
            raise ModuleContractError(
                f"module {module_name!r} provides invalid service {declaration!r}"
            )
        key = (declaration.name, declaration.version)
        if key in seen:
            raise ModuleContractError(f"module {module_name!r} provides {key!r} twice")
        seen.add(key)
    for requirement in consumes:
        if not _SERVICE_NAME_RE.match(requirement.name) or requirement.version < 1:
            raise ModuleContractError(
                f"module {module_name!r} consumes invalid service {requirement!r}"
            )
        if requirement.provider == module_name:
            raise ModuleContractError(f"module {module_name!r} cannot consume its own service")
        if requirement.provider not in dependencies:
            raise ModuleContractError(
                f"module {module_name!r} consumes {requirement.name!r} from "
                f"{requirement.provider!r} without depending on it"
            )


def validate_guild_settings_schema(module_name: str, schema: GuildSettingsSchema) -> None:
    names: set[str] = set()
    for field_spec in schema.fields:
        if not _SETTING_NAME_RE.match(field_spec.name):
            raise ModuleContractError(
                f"module {module_name!r} guild setting {field_spec.name!r} has an invalid name"
            )
        if field_spec.name in names:
            raise ModuleContractError(
                f"module {module_name!r} declares guild setting {field_spec.name!r} twice"
            )
        names.add(field_spec.name)
        if field_spec.kind == "enum" and not field_spec.choices:
            raise ModuleContractError(
                f"module {module_name!r} enum setting {field_spec.name!r} needs choices"
            )
        if field_spec.kind != "enum" and field_spec.choices:
            raise ModuleContractError(
                f"module {module_name!r} setting {field_spec.name!r} has choices but is not enum"
            )
        if field_spec.required and field_spec.default is not None:
            raise ModuleContractError(
                f"module {module_name!r} setting {field_spec.name!r} is required with a default"
            )


# --------------------------------------------------------------------------
# Runtime ports (implemented by core in modules/)
# --------------------------------------------------------------------------

type HealthState = Literal["starting", "healthy", "degraded", "failed"]
HEALTH_DETAIL_MAX_LENGTH = 500
HEALTH_METRICS_MAX_KEYS = 32


@dataclass(frozen=True, slots=True)
class ModuleHealth:
    state: HealthState
    detail: str = ""
    metrics: Mapping[str, float] = field(default_factory=dict)
    updated_at: float = 0.0


class HealthReporter(Protocol):
    def report(
        self,
        state: HealthState,
        detail: str = "",
        metrics: Mapping[str, float] | None = None,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class Event:
    topic: str
    payload: Any
    source_module: str
    published_at: float


type EventHandler = Callable[[Event], Awaitable[None]]


class Subscription(Protocol):
    def close(self) -> None: ...


class EventBus(Protocol):
    def publish(self, topic: str, payload: Any) -> None: ...

    def subscribe(self, pattern: str, handler: EventHandler) -> Subscription: ...


@dataclass(frozen=True, slots=True)
class Backoff:
    base_seconds: float = 30.0
    max_seconds: float = 3600.0
    multiplier: float = 2.0


@dataclass(frozen=True, slots=True)
class JobRun:
    job_id: str
    key: str
    payload: Mapping[str, Any]
    attempt: int
    scheduled_for: float


@dataclass(frozen=True, slots=True)
class JobInfo:
    key: str
    handler: str
    next_run_at: float
    interval_seconds: float | None
    attempt: int
    last_error: str | None


type JobHandler = Callable[[JobRun], Awaitable[None]]


class Scheduler(Protocol):
    def register(self, handler_name: str, handler: JobHandler) -> None: ...

    async def run_at(
        self,
        key: str,
        when: float,
        handler_name: str,
        payload: Mapping[str, Any] | None = None,
    ) -> None: ...

    async def run_every(
        self,
        key: str,
        interval_seconds: float,
        handler_name: str,
        payload: Mapping[str, Any] | None = None,
        *,
        jitter_seconds: float = 0.0,
        backoff: Backoff | None = None,
    ) -> None: ...

    async def cancel(self, key: str) -> bool: ...

    async def list(self) -> Sequence[JobInfo]: ...


class ModuleStorage(Protocol):
    @property
    def connection(self) -> Any: ...

    def table(self, name: str) -> str: ...

    def write_transaction(self) -> Any: ...


@dataclass(frozen=True, slots=True)
class MigrationContext:
    connection: Any
    table: Callable[[str], str]


type ScopedModuleMigration = tuple[str, Callable[[MigrationContext], Awaitable[None]]]


@dataclass(frozen=True, slots=True)
class MessageRef:
    guild_id: int
    channel_id: int
    message_id: int
    # Parent channel when the message is in a thread; None otherwise.
    parent_channel_id: int | None = None


@dataclass(frozen=True, slots=True)
class AttachmentSnapshot:
    attachment_id: int
    filename: str
    url: str
    size: int
    content_type: str | None


@dataclass(frozen=True, slots=True)
class MessageSnapshot:
    ref: MessageRef
    author_id: int
    content: str
    attachments: tuple[AttachmentSnapshot, ...]
    jump_url: str
    created_at: float
    author_display_name: str = ""
    author_is_bot: bool = False
    # Image URLs from the message's embeds (proxy URLs when Discord provides them).
    embed_image_urls: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MemberSnapshot:
    guild_id: int
    user_id: int
    display_name: str
    role_ids: tuple[int, ...]
    is_bot: bool
    joined_at: float | None
    timed_out_until: float | None


@dataclass(frozen=True, slots=True)
class OutgoingEmbed:
    title: str | None = None
    description: str | None = None
    color: int | None = None
    fields: tuple[tuple[str, str, bool], ...] = ()
    footer: str | None = None
    timestamp: bool = False


class DiscordActions(Protocol):
    """Declared Discord operations on stable IDs.

    ``actor_id`` on ban/kick/timeout is the staff member acting through the
    module, or ``None`` when the module acts on its own (automated
    enforcement). The target policy then requires the target to be below
    staff tier instead of below the actor's tier.
    """

    async def send_message(
        self,
        channel_id: int,
        content: str | None = None,
        *,
        embed: OutgoingEmbed | None = None,
        reply_to: MessageRef | None = None,
        components: Sequence[Any] = (),
    ) -> MessageRef: ...

    async def send_dm(
        self, user_id: int, content: str, *, embed: OutgoingEmbed | None = None
    ) -> bool: ...

    async def edit_message(
        self, ref: MessageRef, content: str | None = None, *, embed: OutgoingEmbed | None = None
    ) -> None: ...

    async def delete_message(self, ref: MessageRef, *, reason: str = "") -> None: ...

    async def ban(
        self,
        guild_id: int,
        user_id: int,
        *,
        actor_id: int | None,
        reason: str,
        delete_message_seconds: int = 0,
    ) -> None: ...

    async def kick(
        self, guild_id: int, user_id: int, *, actor_id: int | None, reason: str
    ) -> None: ...

    async def timeout(
        self,
        guild_id: int,
        user_id: int,
        *,
        actor_id: int | None,
        reason: str,
        duration_seconds: int,
    ) -> None: ...

    async def fetch_message(self, ref: MessageRef) -> MessageSnapshot | None: ...

    async def fetch_member(self, guild_id: int, user_id: int) -> MemberSnapshot | None: ...


type TrustTierName = Literal["member", "regular", "staff"]


class TrustLookup(Protocol):
    """Read-only trust tier lookup, mirroring core's member < regular < staff."""

    async def tier(self, guild_id: int, user_id: int) -> TrustTierName: ...


type CommandOptionKind = Literal["string", "integer", "boolean", "user", "channel", "role"]


@dataclass(frozen=True, slots=True)
class CommandOption:
    name: str
    kind: CommandOptionKind
    description: str
    required: bool = False
    choices: tuple[tuple[str, str | int], ...] = ()
    min_value: int | None = None
    max_value: int | None = None
    autocomplete: bool = False


@dataclass(frozen=True, slots=True)
class CommandSpec:
    name: str
    description: str
    options: tuple[CommandOption, ...] = ()
    min_tier: TrustTierName = "staff"
    group: str | None = None
    group_description: str = ""


class ModuleInteraction(Protocol):
    @property
    def guild_id(self) -> int: ...

    @property
    def channel_id(self) -> int: ...

    @property
    def user_id(self) -> int: ...

    @property
    def guild_name(self) -> str | None: ...

    @property
    def options(self) -> Mapping[str, Any]: ...

    @property
    def custom_id(self) -> str | None: ...

    @property
    def values(self) -> tuple[str, ...]: ...

    async def respond(
        self,
        content: str | None = None,
        *,
        embed: OutgoingEmbed | None = None,
        ephemeral: bool = False,
        components: Sequence[Any] = (),
    ) -> None: ...

    async def defer(self, *, ephemeral: bool = False) -> None: ...

    async def edit_original(
        self,
        content: str | None = None,
        *,
        embed: OutgoingEmbed | None = None,
        components: Sequence[Any] = (),
    ) -> None: ...

    async def follow_up(
        self, content: str, *, embed: OutgoingEmbed | None = None, ephemeral: bool = False
    ) -> None: ...


type ButtonStyle = Literal["primary", "secondary", "success", "danger"]


@dataclass(frozen=True, slots=True)
class ButtonSpec:
    """A persistent button; ``key`` names the handler registered for it."""

    key: str
    label: str
    style: ButtonStyle = "secondary"
    parts: tuple[str, ...] = ()
    disabled: bool = False
    emoji: str | None = None


@dataclass(frozen=True, slots=True)
class SelectSpec:
    """A persistent single/multi select; options are (label, value, description)."""

    key: str
    options: tuple[tuple[str, str, str | None], ...]
    placeholder: str | None = None
    parts: tuple[str, ...] = ()
    min_values: int = 1
    max_values: int = 1


type CommandHandler = Callable[[ModuleInteraction], Awaitable[None]]
type AutocompleteHandler = Callable[
    [ModuleInteraction, str, str], Awaitable[Sequence[tuple[str, str | int]]]
]
type ComponentKind = Literal["button", "select"]


class Registration(Protocol):
    def close(self) -> None: ...


class InteractionRouter(Protocol):
    def add_command(
        self,
        spec: CommandSpec,
        handler: CommandHandler,
        *,
        autocomplete: AutocompleteHandler | None = None,
    ) -> Registration: ...

    def register_component(
        self,
        kind: ComponentKind,
        key: str,
        handler: CommandHandler,
        *,
        expires_after_seconds: float | None = None,
    ) -> Registration: ...

    def custom_id(self, key: str, *parts: str) -> str: ...


@dataclass(frozen=True, slots=True)
class GuildSettingsSnapshot:
    values: Mapping[str, Any]
    valid: bool
    errors: tuple[str, ...]
    revision: str
    legacy: bool = False


class GuildSettings(Protocol):
    def get(self, guild_id: int) -> GuildSettingsSnapshot: ...

    def is_enabled(self, guild_id: int) -> bool: ...

    def on_change(self, callback: Callable[[int], None]) -> Registration: ...


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes

    def json(self) -> Any:
        import json

        return json.loads(self.body)


class ModuleHttp(Protocol):
    async def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout_seconds: float = 20.0,
        max_bytes: int = 8 * 1024 * 1024,
    ) -> HttpResponse: ...

    async def post_json(
        self,
        url: str,
        payload: Any,
        *,
        headers: Mapping[str, str] | None = None,
        timeout_seconds: float = 20.0,
        max_bytes: int = 8 * 1024 * 1024,
    ) -> HttpResponse: ...

    def download(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout_seconds: float = 30.0,
        max_bytes: int = 8 * 1024 * 1024,
    ) -> AsyncIterator[bytes]: ...


class ServiceRegistry(Protocol):
    def provide(self, name: str, version: int, implementation: object) -> Registration: ...

    def get(self, name: str, version: int) -> object: ...
