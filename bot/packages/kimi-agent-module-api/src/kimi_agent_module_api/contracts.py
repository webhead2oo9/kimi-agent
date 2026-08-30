"""Module API contracts: declarations, service ports, and validation rules.

Everything here is a shape or a pure rule. Core implements the Protocols in
``modules/``; external packages import only this module and its siblings. This
file must stay free of Discord SDK, database, and core runtime imports so a
module's declarations can be validated without booting anything.

Modules are trusted, in-process code. Declarations are audited through the
owner manifest and enforced through the ports below; they are not a sandbox.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, TypeVar, overload

_T = TypeVar("_T")

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


class CommandSyncError(RuntimeError):
    """Discord did not accept a live guild-command synchronization."""


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
    "fetch_channel",
    "fetch_messages",
    "fetch_pins",
    "fetch_public_threads",
    "fetch_roles",
    "fetch_invites",
    "can_view_channel",
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
        "fetch_channel",
        "fetch_messages",
        "fetch_pins",
        "fetch_public_threads",
        "fetch_roles",
        "fetch_invites",
        "can_view_channel",
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


_GUILD_ID_RE = re.compile(r"^\d{1,25}$")
_GUILD_SETTING_LIST_MAX = 512
_GUILD_SETTING_STRING_MAX = 2_000


def _render_scalar(key: str, value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return _quote(value)
    raise TypeError(f"guild setting {key!r} has unrenderable value {value!r}")


def _quote(text: str) -> str:
    """YAML double-quoted scalar for any Python string.

    JSON string syntax is valid YAML double-quoted syntax, which handles
    colons, hashes, quotes, newlines, and words like ``true``. YAML also
    forbids raw C1 controls, DEL, surrogates, and the two non-characters
    that JSON leaves unescaped, so those are written as ``\\uXXXX`` too.
    """
    escaped = json.dumps(text, ensure_ascii=False)
    return "".join(f"\\u{ord(ch):04x}" if _yaml_unprintable(ch) else ch for ch in escaped)


def _yaml_unprintable(ch: str) -> bool:
    """Characters a YAML double-quoted scalar cannot carry literally.

    C1 controls and DEL are not printable; surrogates and the two
    non-characters are invalid; U+2028/U+2029 are YAML line breaks that would
    be folded together with surrounding spaces; the BOM is a stream marker.
    """
    code = ord(ch)
    return (
        0x7F <= code <= 0x9F
        or 0xD800 <= code <= 0xDFFF
        or code in (0x2028, 0x2029, 0xFEFF, 0xFFFE, 0xFFFF)
    )


def render_guild_settings(values: Mapping[str, Any]) -> str:
    """Render guild settings as the frontmatter-only document the host stores.

    This is the format of ``<CONFIG_DIR>/guild-modules/<guild_id>/<module>.md``
    and the content a module passes to ``ProposalService.propose`` for a
    ``guild:<id>:<module>`` target. Pass the snapshot's ``values`` with your
    change applied: keys are emitted sorted, ``None`` (an unset optional
    field) is omitted, booleans become ``true``/``false``, ids and ints are
    bare, strings are always quoted, and lists use flow style. Invalid field
    names raise ``ValueError``; unsupported values raise ``TypeError``. The
    schema kinds cover every value a snapshot holds.
    """
    keys = tuple(values)
    for key in keys:
        if not isinstance(key, str) or not _SETTING_NAME_RE.fullmatch(key):
            raise ValueError(f"invalid guild setting name {key!r}")

    lines = ["---"]
    for key in sorted(keys):
        value = values[key]
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            rendered = "[" + ", ".join(_render_scalar(key, item) for item in value) + "]"
        else:
            rendered = _render_scalar(key, value)
        lines.append(f"{key}: {rendered}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def coerce_guild_setting_value(field_spec: GuildSettingField, raw: Any) -> tuple[Any, str | None]:
    """Validate and normalize a configured value or the field's default."""
    if raw is None:
        if field_spec.required:
            return None, f"{field_spec.name} is required"
        if field_spec.default is None:
            return None, None
        raw = field_spec.default
    kind = field_spec.kind
    if kind == "bool":
        if isinstance(raw, bool):
            return raw, None
        return None, f"{field_spec.name} must be true or false"
    if kind == "int":
        if isinstance(raw, bool) or not isinstance(raw, int):
            return None, f"{field_spec.name} must be an integer"
        return raw, None
    if kind == "id":
        token = str(raw).strip()
        if not _GUILD_ID_RE.match(token):
            return None, f"{field_spec.name} must be a numeric Discord id"
        return int(token), None
    if kind == "id_list":
        if not isinstance(raw, (list, tuple)):
            return None, f"{field_spec.name} must be a list of Discord ids"
        if len(raw) > _GUILD_SETTING_LIST_MAX:
            return None, f"{field_spec.name} has more than {_GUILD_SETTING_LIST_MAX} entries"
        ids: list[int] = []
        for entry in raw:
            token = str(entry).strip()
            if not _GUILD_ID_RE.match(token):
                return None, f"{field_spec.name} contains a non-numeric id {entry!r}"
            ids.append(int(token))
        return tuple(ids), None
    if kind == "str":
        if not isinstance(raw, str):
            return None, f"{field_spec.name} must be text"
        if len(raw) > _GUILD_SETTING_STRING_MAX:
            return None, (
                f"{field_spec.name} is longer than {_GUILD_SETTING_STRING_MAX} characters"
            )
        return raw, None
    if kind == "str_list":
        if not isinstance(raw, (list, tuple)):
            return None, f"{field_spec.name} must be a list of text values"
        if len(raw) > _GUILD_SETTING_LIST_MAX:
            return None, f"{field_spec.name} has more than {_GUILD_SETTING_LIST_MAX} entries"
        items: list[str] = []
        for entry in raw:
            if not isinstance(entry, str) or len(entry) > _GUILD_SETTING_STRING_MAX:
                return None, f"{field_spec.name} contains an invalid entry {entry!r}"
            items.append(entry)
        return tuple(items), None
    if kind == "enum":
        token = str(raw).strip()
        if token not in field_spec.choices:
            return None, f"{field_spec.name} must be one of {', '.join(field_spec.choices)}"
        return token, None
    return None, f"{field_spec.name} has unsupported kind {kind!r}"


# --------------------------------------------------------------------------
# Naming rules shared by declarations and runtime ports
# --------------------------------------------------------------------------

_MODULE_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
# Logical table names a module may ask ``storage.table()`` for.
TABLE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_TOPIC_SEGMENT_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SERVICE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")
_SETTING_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
CORE_TOPIC_PREFIX = "discord"
_CORE_RESERVED_MODULE_NAMES = frozenset({CORE_TOPIC_PREFIX, "proposals"})
CUSTOM_ID_PREFIX = "m"
CUSTOM_ID_MAX_LENGTH = 100


def table_prefix(module_name: str) -> str:
    return module_name.replace("-", "_")


def validate_module_name(name: str) -> None:
    if not _MODULE_NAME_RE.match(name):
        raise ModuleContractError(f"invalid module name {name!r}")
    if table_prefix(name) in _CORE_RESERVED_MODULE_NAMES:
        raise ModuleContractError(f"module name {name!r} is reserved by core")


def split_topic(topic: str, *, allow_wildcard: bool = False) -> tuple[str, str]:
    """Split ``<namespace>.<name>``; ``<namespace>.*`` only where patterns are legal."""
    namespace, sep, name = topic.partition(".")
    name_ok = _TOPIC_SEGMENT_RE.match(name) or (allow_wildcard and name == "*")
    if not sep or not _TOPIC_SEGMENT_RE.match(namespace) or not name_ok:
        raise EventTopicError(f"invalid event topic {topic!r}; expected '<namespace>.<name>'")
    return namespace, name


def validate_publish_topic(module_name: str, topic: str) -> None:
    namespace, _ = split_topic(topic)
    if namespace == CORE_TOPIC_PREFIX:
        raise EventTopicError(f"event namespace {CORE_TOPIC_PREFIX!r} is reserved by core")
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
    required: dict[tuple[str, int], str] = {}
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
        key = (requirement.name, requirement.version)
        previous = required.get(key)
        if previous is not None:
            detail = "twice" if previous == requirement.provider else "from multiple providers"
            raise ModuleContractError(
                f"module {module_name!r} consumes {requirement.name}@{requirement.version} {detail}"
            )
        required[key] = requirement.provider


def validate_guild_settings_schema(module_name: str, schema: GuildSettingsSchema) -> None:
    if schema.invalid_policy not in ("disable_module", "disable_guild"):
        raise ModuleContractError(
            f"module {module_name!r} guild settings has invalid policy {schema.invalid_policy!r}"
        )
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
        if field_spec.default is not None:
            _value, error = coerce_guild_setting_value(field_spec, field_spec.default)
            if error is not None:
                raise ModuleContractError(
                    f"module {module_name!r} setting {field_spec.name!r} has an invalid "
                    f"default: {error}"
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
    """Module-side health reporting.

    ``report(...)`` without ``key`` sets the module's overall state and replaces
    the previous unkeyed report. ``report(..., key="digest")`` sets one named
    concern that is tracked independently: the module's visible state is the
    worst of every keyed concern plus the unkeyed report, so one subsystem
    going ``degraded`` is not erased by another reporting ``healthy``. A keyed
    ``healthy`` report with no detail and no metrics clears that concern.
    """

    def report(
        self,
        state: HealthState,
        detail: str = "",
        metrics: Mapping[str, float] | None = None,
        *,
        key: str | None = None,
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

    def __post_init__(self) -> None:
        for name, value in (
            ("base_seconds", self.base_seconds),
            ("max_seconds", self.max_seconds),
            ("multiplier", self.multiplier),
        ):
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise ModuleContractError(f"backoff {name} must be a finite number")
            if not math.isfinite(value):
                raise ModuleContractError(f"backoff {name} must be finite")
        if self.base_seconds <= 0:
            raise ModuleContractError("backoff base_seconds must be positive")
        if self.max_seconds <= 0:
            raise ModuleContractError("backoff max_seconds must be positive")
        if self.multiplier < 1:
            raise ModuleContractError("backoff multiplier must be at least 1")


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


type ProposalState = Literal["pending", "applied", "rejected"]


class ProposalError(RuntimeError):
    """A configuration proposal could not be read, created, or decided."""


@dataclass(frozen=True, slots=True)
class ProposalActor:
    user_id: str
    source: str
    guild_id: str | None = None
    channel_id: str | None = None


@dataclass(frozen=True, slots=True)
class ConfigSnapshot:
    target: str
    revision: str
    content: str


@dataclass(frozen=True, slots=True)
class ProposalRef:
    proposal_id: str
    target: str
    state: ProposalState
    message: MessageRef | None = None
    decided_by: str | None = None
    decision_reason: str = ""


class ProposalService(Protocol):
    """Guild-scoped fragment proposals, already bound to one module."""

    async def snapshot(self, target: str, *, actor: ProposalActor) -> ConfigSnapshot: ...

    async def propose(
        self,
        *,
        target: str,
        content: str,
        summary: str,
        actor: ProposalActor,
        expected_revision: str | None = None,
    ) -> ProposalRef: ...

    async def get(self, proposal_id: str, *, actor: ProposalActor) -> ProposalRef | None: ...


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
    reply_to_message_id: int | None = None
    pinned: bool = False
    edited_at: float | None = None
    embed_texts: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class InviteSnapshot:
    """Discord invite metadata available through gateway events or a guild fetch.

    Gateway delete events are intentionally partial, so every field except the
    guild and code may be absent. ``fetch_invites`` returns the richer form used
    for best-effort join attribution by comparing ``uses`` counters.
    """

    guild_id: int
    code: str
    channel_id: int | None = None
    inviter_id: int | None = None
    uses: int | None = None
    max_uses: int | None = None
    max_age_seconds: int | None = None
    temporary: bool | None = None
    created_at: float | None = None
    expires_at: float | None = None


type ChannelKind = Literal["text", "forum", "thread"]


@dataclass(frozen=True, slots=True)
class ChannelSnapshot:
    guild_id: int
    channel_id: int
    kind: ChannelKind
    name: str
    parent_channel_id: int | None = None
    topic: str = ""
    archived: bool = False
    private: bool = False
    applied_tags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MessagePage:
    messages: tuple[MessageSnapshot, ...]
    next_cursor: int | None
    has_more: bool


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
class RoleSnapshot:
    guild_id: int
    role_id: int
    name: str
    # Higher positions sit above lower ones in the guild's role list.
    position: int
    # Managed roles belong to an integration or bot and cannot be assigned by hand.
    managed: bool = False


@dataclass(frozen=True, slots=True)
class OutgoingEmbed:
    title: str | None = None
    description: str | None = None
    color: int | None = None
    fields: tuple[tuple[str, str, bool], ...] = ()
    footer: str | None = None
    timestamp: bool = False


@dataclass(frozen=True, slots=True)
class LayoutText:
    content: str


type LayoutSeparatorSpacing = Literal["small", "large"]


@dataclass(frozen=True, slots=True)
class LayoutSeparator:
    visible: bool = True
    spacing: LayoutSeparatorSpacing = "small"


@dataclass(frozen=True, slots=True)
class LayoutGallery:
    urls: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LayoutSection:
    texts: tuple[str, ...]
    thumbnail_url: str


type LayoutItem = LayoutText | LayoutSeparator | LayoutGallery | LayoutSection


@dataclass(frozen=True, slots=True)
class OutgoingLayout:
    """One Components V2 container, optionally followed by interactive controls."""

    items: tuple[LayoutItem, ...]
    accent_color: int | None = None


def validate_outgoing_layout(layout: OutgoingLayout) -> None:
    """Validate the Discord hard limits represented by ``OutgoingLayout``."""
    if not 1 <= len(layout.items) <= 40:
        raise ModuleContractError("a layout must contain between one and 40 items")
    if layout.accent_color is not None and (
        isinstance(layout.accent_color, bool) or not 0 <= layout.accent_color <= 0xFFFFFF
    ):
        raise ModuleContractError("layout accent_color must be between 0 and 0xFFFFFF")

    def validate_text(text: Any, label: str) -> None:
        if not isinstance(text, str) or not 1 <= len(text) <= 4_000:
            raise ModuleContractError(f"{label} must contain between one and 4000 characters")

    text_length = 0
    for item in layout.items:
        if isinstance(item, LayoutText):
            validate_text(item.content, "layout text")
            text_length += len(item.content)
        elif isinstance(item, LayoutSeparator):
            if item.spacing not in ("small", "large"):
                raise ModuleContractError(f"invalid layout separator spacing {item.spacing!r}")
        elif isinstance(item, LayoutGallery):
            if not 1 <= len(item.urls) <= 10:
                raise ModuleContractError("a layout gallery must contain between one and 10 URLs")
            if any(not isinstance(url, str) or not url for url in item.urls):
                raise ModuleContractError("layout gallery URLs must be non-empty strings")
        elif isinstance(item, LayoutSection):
            if not 1 <= len(item.texts) <= 3:
                raise ModuleContractError("a layout section must contain between one and 3 texts")
            for text in item.texts:
                validate_text(text, "layout section text")
                text_length += len(text)
            if not isinstance(item.thumbnail_url, str) or not item.thumbnail_url:
                raise ModuleContractError("a layout section thumbnail URL must be non-empty")
        else:
            raise ModuleContractError(f"unsupported layout item {item!r}")
    if text_length > 4_000:
        raise ModuleContractError("layout text cannot exceed 4000 characters in total")


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

    async def fetch_channel(self, guild_id: int, channel_id: int) -> ChannelSnapshot | None: ...

    async def fetch_messages(
        self,
        guild_id: int,
        channel_id: int,
        *,
        after_message_id: int | None = None,
        before_message_id: int | None = None,
        limit: int = 100,
    ) -> MessagePage: ...

    async def fetch_pins(self, guild_id: int, channel_id: int) -> tuple[MessageSnapshot, ...]: ...

    async def fetch_public_threads(
        self, guild_id: int, parent_channel_id: int
    ) -> tuple[ChannelSnapshot, ...]: ...

    async def fetch_roles(self, guild_id: int) -> tuple[RoleSnapshot, ...]: ...

    async def fetch_invites(self, guild_id: int) -> tuple[InviteSnapshot, ...]: ...

    async def can_view_channel(self, guild_id: int, user_id: int, channel_id: int) -> bool: ...


type TrustTierName = Literal["member", "regular", "staff"]


class TrustLookup(Protocol):
    """Read-only trust tier lookup, mirroring core's member < regular < staff."""

    async def tier(self, guild_id: int, user_id: int) -> TrustTierName: ...


type CommandOptionKind = Literal["string", "integer", "boolean", "user", "channel", "role"]
_OPTION_KINDS = frozenset({"string", "integer", "boolean", "user", "channel", "role"})


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

    @property
    def text_values(self) -> Mapping[str, str]: ...

    @property
    def message(self) -> MessageRef | None:
        """The message a button or select lives on; ``None`` for slash commands."""
        ...

    async def respond(
        self,
        content: str | None = None,
        *,
        embed: OutgoingEmbed | None = None,
        layout: OutgoingLayout | None = None,
        ephemeral: bool = False,
        components: Sequence[Any] = (),
    ) -> None: ...

    async def defer(self, *, ephemeral: bool = False) -> None: ...

    async def show_modal(self, modal: ModalSpec) -> None: ...

    async def edit_original(
        self,
        content: str | None = None,
        *,
        embed: OutgoingEmbed | None = None,
        layout: OutgoingLayout | None = None,
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


def validate_layout_components(
    components: Sequence[Any], *, layout: OutgoingLayout | None = None
) -> None:
    """Validate control rows and the full Components V2 descendant limit."""
    rows = 0
    buttons_in_row = 0
    for component in components:
        if isinstance(component, ButtonSpec):
            if buttons_in_row == 0:
                rows += 1
            buttons_in_row = (buttons_in_row + 1) % 5
        elif isinstance(component, SelectSpec):
            buttons_in_row = 0
            rows += 1
        else:
            raise ModuleContractError(f"unsupported component {component!r}")
    if rows > 5:
        raise ModuleContractError("layout controls cannot require more than five action rows")
    if layout is not None:
        descendants = 1 + rows + len(components)
        for item in layout.items:
            descendants += len(item.texts) + 2 if isinstance(item, LayoutSection) else 1
        if descendants > 40:
            raise ModuleContractError("a layout cannot exceed 40 components in total")


type TextInputStyle = Literal["short", "paragraph"]


@dataclass(frozen=True, slots=True)
class TextInputSpec:
    key: str
    label: str
    style: TextInputStyle = "short"
    default: str | None = None
    placeholder: str | None = None
    required: bool = True
    min_length: int | None = None
    max_length: int | None = None


@dataclass(frozen=True, slots=True)
class ModalSpec:
    """A modal whose submit handler is registered under ``key``."""

    key: str
    title: str
    inputs: tuple[TextInputSpec, ...]
    parts: tuple[str, ...] = ()


_OPTIONS_WITH_CHOICES = frozenset({"string", "integer"})


def validate_command_spec(spec: CommandSpec) -> None:
    """Validate the Discord limits a command payload must satisfy.

    A whole ``tree.sync()`` is one bulk PUT, so a single malformed command
    rejects every command in that scope. Discord.py checks command and group
    *names* itself and reorders required options ahead of optional ones; these
    are the limits nothing else enforces before the payload reaches Discord.
    """

    if not isinstance(spec.description, str) or not 1 <= len(spec.description) <= 100:
        raise ModuleContractError("a command description must contain 1 to 100 characters")
    if spec.group is not None and (
        not isinstance(spec.group_description, str) or len(spec.group_description) > 100
    ):
        raise ModuleContractError("a command group description cannot exceed 100 characters")
    if len(spec.options) > 25:
        raise ModuleContractError("a command cannot declare more than 25 options")

    names: set[str] = set()
    for option in spec.options:
        if not isinstance(option.name, str) or not 1 <= len(option.name) <= 32:
            raise ModuleContractError(f"invalid command option name {option.name!r}")
        if option.name in names:
            raise ModuleContractError("command option names must be unique")
        names.add(option.name)
        if option.kind not in _OPTION_KINDS:
            raise ModuleContractError(f"unsupported command option kind {option.kind!r}")
        if not isinstance(option.description, str) or not 1 <= len(option.description) <= 100:
            raise ModuleContractError(
                f"the description for option {option.name!r} must contain 1 to 100 characters"
            )
        if option.choices and option.autocomplete:
            # Discord rejects a payload carrying both, and discord.py emits both.
            raise ModuleContractError(
                f"option {option.name!r} cannot use choices and autocomplete together"
            )
        if (option.choices or option.autocomplete) and option.kind not in _OPTIONS_WITH_CHOICES:
            raise ModuleContractError(
                f"option {option.name!r} cannot use choices or autocomplete on a "
                f"{option.kind} option"
            )
        if len(option.choices) > 25:
            raise ModuleContractError(f"option {option.name!r} cannot declare more than 25 choices")
        values: set[str | int] = set()
        for name, value in option.choices:
            if not isinstance(name, str) or not 1 <= len(name) <= 100:
                raise ModuleContractError(f"invalid choice name {name!r} on option {option.name!r}")
            if isinstance(value, bool) or not isinstance(value, str | int):
                raise ModuleContractError(
                    f"invalid choice value {value!r} on option {option.name!r}"
                )
            if isinstance(value, str) and not 1 <= len(value) <= 100:
                raise ModuleContractError(
                    f"a choice value on option {option.name!r} must contain 1 to 100 characters"
                )
            if value in values:
                raise ModuleContractError(f"choice values on option {option.name!r} must be unique")
            values.add(value)
        for bound in (option.min_value, option.max_value):
            if bound is None:
                continue
            if isinstance(bound, bool) or not isinstance(bound, int):
                raise ModuleContractError(f"option {option.name!r} bounds must be integers")
            if option.kind != "integer":
                # Only integer options carry the bounds through to Discord, so a
                # bound anywhere else would be silently dropped.
                raise ModuleContractError(
                    f"option {option.name!r} cannot set min_value or max_value on a "
                    f"{option.kind} option"
                )
        if (
            option.min_value is not None
            and option.max_value is not None
            and option.min_value > option.max_value
        ):
            raise ModuleContractError(f"option {option.name!r} min_value cannot exceed max_value")


def validate_select_spec(select: SelectSpec) -> None:
    """Validate Discord's hard select-menu limits."""

    if not isinstance(select.key, str) or not _TOPIC_SEGMENT_RE.fullmatch(select.key):
        raise ModuleContractError(f"invalid select key {select.key!r}")
    if any(not isinstance(part, str) or ":" in part for part in select.parts):
        raise ModuleContractError("select custom_id parts must be strings without ':'")
    if select.placeholder is not None and (
        not isinstance(select.placeholder, str) or len(select.placeholder) > 150
    ):
        raise ModuleContractError("a select placeholder cannot exceed 150 characters")
    if not 1 <= len(select.options) <= 25:
        raise ModuleContractError("a select must contain between one and 25 options")

    values: set[str] = set()
    for label, value, description in select.options:
        if not isinstance(label, str) or not 1 <= len(label) <= 100:
            raise ModuleContractError("a select option label must contain 1 to 100 characters")
        if not isinstance(value, str) or not 1 <= len(value) <= 100:
            raise ModuleContractError("a select option value must contain 1 to 100 characters")
        if value in values:
            raise ModuleContractError("select option values must be unique")
        values.add(value)
        if description is not None and (
            not isinstance(description, str) or not 1 <= len(description) <= 100
        ):
            raise ModuleContractError(
                "a select option description must contain 1 to 100 characters"
            )

    for name, bound, low in (
        ("min_values", select.min_values, 0),
        ("max_values", select.max_values, 1),
    ):
        if isinstance(bound, bool) or not isinstance(bound, int) or not low <= bound <= 25:
            raise ModuleContractError(f"select {name} must be between {low} and 25")
    if select.min_values > select.max_values:
        raise ModuleContractError("select min_values cannot exceed max_values")


def validate_modal_spec(modal: ModalSpec) -> None:
    """Validate Discord's hard modal and text-input limits."""
    if not isinstance(modal.key, str) or not _TOPIC_SEGMENT_RE.fullmatch(modal.key):
        raise ModuleContractError(f"invalid modal key {modal.key!r}")
    if any(not isinstance(part, str) or ":" in part for part in modal.parts):
        raise ModuleContractError("modal custom_id parts must be strings without ':'")
    if not isinstance(modal.title, str) or not 1 <= len(modal.title) <= 45:
        raise ModuleContractError("a modal title must contain between one and 45 characters")
    if not 1 <= len(modal.inputs) <= 5:
        raise ModuleContractError("a modal must contain between one and five text inputs")

    keys: set[str] = set()
    for input_spec in modal.inputs:
        if not isinstance(input_spec.key, str) or not _TOPIC_SEGMENT_RE.fullmatch(input_spec.key):
            raise ModuleContractError(f"invalid modal text input key {input_spec.key!r}")
        if input_spec.key in keys:
            raise ModuleContractError("modal text input keys must be unique")
        keys.add(input_spec.key)
        if not isinstance(input_spec.label, str) or not 1 <= len(input_spec.label) <= 45:
            raise ModuleContractError(
                "a modal text input label must contain between one and 45 characters"
            )
        if input_spec.style not in ("short", "paragraph"):
            raise ModuleContractError(f"invalid modal text input style {input_spec.style!r}")
        if input_spec.placeholder is not None and (
            not isinstance(input_spec.placeholder, str) or len(input_spec.placeholder) > 100
        ):
            raise ModuleContractError("a modal text input placeholder cannot exceed 100 characters")
        if input_spec.default is not None and (
            not isinstance(input_spec.default, str) or len(input_spec.default) > 4_000
        ):
            raise ModuleContractError("a modal text input default cannot exceed 4000 characters")
        if input_spec.min_length is not None and (
            isinstance(input_spec.min_length, bool)
            or not isinstance(input_spec.min_length, int)
            or not 0 <= input_spec.min_length <= 4_000
        ):
            raise ModuleContractError("modal text input min_length must be between 0 and 4000")
        if input_spec.max_length is not None and (
            isinstance(input_spec.max_length, bool)
            or not isinstance(input_spec.max_length, int)
            or not 1 <= input_spec.max_length <= 4_000
        ):
            raise ModuleContractError("modal text input max_length must be between 1 and 4000")
        if (
            input_spec.min_length is not None
            and input_spec.max_length is not None
            and input_spec.min_length > input_spec.max_length
        ):
            raise ModuleContractError("modal text input min_length cannot exceed max_length")


type CommandHandler = Callable[[ModuleInteraction], Awaitable[None]]
type AutocompleteHandler = Callable[
    [ModuleInteraction, str, str], Awaitable[Sequence[tuple[str, str | int]]]
]
type ComponentKind = Literal["button", "select", "modal"]


@dataclass(frozen=True, slots=True)
class GuildCommand:
    """One command in a module's desired command set for a guild."""

    spec: CommandSpec
    handler: CommandHandler
    autocomplete: AutocompleteHandler | None = None


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

    async def replace_guild_commands(
        self,
        guild_id: int,
        commands: Sequence[GuildCommand],
    ) -> None:
        """Replace this module's complete command set for one guild."""
        ...

    def register_component(
        self,
        kind: ComponentKind,
        key: str,
        handler: CommandHandler,
        *,
        expires_after_seconds: float | None = None,
        min_tier: TrustTierName = "member",
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
    def guild_ids(self) -> Sequence[int]: ...

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

    @overload
    def get(self, name: str, version: int) -> object: ...

    @overload
    def get(self, name: str, version: int, type_: type[_T]) -> _T: ...

    def get(self, name: str, version: int, type_: type[_T] | None = None) -> object:
        """Resolve a consumed service; with ``type_`` the result is checked and typed.

        The result is always a proxy that forwards attribute access and raises
        ``ServiceUnavailable`` once the provider closes. ``type_`` is checked
        against the provided object at resolution time, so a provider that
        changed its class fails here instead of at the first call, and the
        proxy is then typed as ``type_`` for method calls. Because it is a
        proxy, ``isinstance`` on the result is false and special methods
        (``__call__``, ``__getitem__``, ...) are not forwarded: a service is an
        object with ordinary methods, nothing more.
        """
        ...
