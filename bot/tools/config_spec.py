"""Typed per-tool configuration specs.

A tool declares one of these at ``registry.register(..., config_spec=...)`` to
say "an operator may tune these knobs". Values live in
``<config_dir>/tools/<tool_name>.md`` frontmatter, are read fresh each turn by
``config/fragments/tool_config.py``, and reach handlers already resolved on
``MessageContext.tool_configs``. Handlers never merge defaults themselves.

**Credentials, endpoints, and filesystem paths are never tool config.** A
fragment is plaintext on disk, readable by anyone with host access; a spec
that put an API token or a base URL there would leak the one and hand an
operator-facing markdown file control over where the bot connects. So a config fragment may only
switch between backends the deployment already has credentials for, and that is
enforced, not merely documented: :func:`validate_config_spec` rejects any field
whose name ends on a credential/endpoint/path word (:data:`_DENIED_FIELD_WORDS`)
at **registration** (boot, where the tool's author sees it), mirroring
``config/operator_settings.py``'s ``_EXCLUDED_SUFFIXES`` for the sibling
Settings catalog.

This is a **stdlib-only leaf**: ``tools/registry.py`` imports it, and it must
never import ``config.settings`` (or any other bot runtime module), because a
spec is authored at import time by tool modules that run long before the
settings singleton is layered, and coupling it would put boot ordering between a
tool and its own declaration. Enforced by ``tests/test_import_isolation.py``.

It deliberately mirrors ``config/operator_settings.py``'s ``SettingSpec`` and
``coerce_value`` rather than importing them: that module is bound to the
``Settings`` model, and the two catalogs answer different questions (which
process-wide setting an operator may override, versus which knobs one tool
exposes). What is copied is the *shape*: a frozen field descriptor plus one
coercion chokepoint, so both catalogs share one validation discipline.

Differences from ``SettingSpec``, all deliberate:

* No ``live``/restart metadata. Every tool config value is re-read per turn, so
  there is nothing to mark as pending a restart.
* No ``group``. One tool's knobs are a short list, not a 300-field catalog.
* No ``nullable``. Absent means "use the declared default", which is the only
  unset state a tool needs; a field that wants an empty value declares one.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from config import field_kinds
from config.field_kinds import coerce_scalar

# Value shapes the tool and the fragment agree on.
# Re-exported so tool modules keep declaring specs with `from tools.config_spec
# import KIND_INT`; the vocabulary itself is shared (see config/field_kinds.py).
KIND_INT = field_kinds.KIND_INT
KIND_FLOAT = field_kinds.KIND_FLOAT
KIND_BOOL = field_kinds.KIND_BOOL
KIND_TEXT = field_kinds.KIND_TEXT
KIND_CHOICE = field_kinds.KIND_CHOICE

CONFIG_KINDS = (
    KIND_INT,
    KIND_FLOAT,
    KIND_BOOL,
    KIND_TEXT,
    KIND_CHOICE,
)

# Field names double as YAML frontmatter keys.
FIELD_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
# Choice entries are operator-visible tokens, never free prose.
_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

MAX_FIELDS = 32
MAX_VOCABULARY = 64

# Words a config field name may not end on. A tool config value is operator
# prose: it is persisted in plaintext to ``<config_dir>/tools/<tool>.md`` and
# handed straight to the handler. Nothing with that blast radius may say
# *where* the bot connects, *what* it authenticates with, or *which* paths it
# touches. Those stay environment-only.
#
# Matched against the last ``_``-separated segment, so both ``api_token`` and a
# bare ``token`` are caught. This mirrors ``config/operator_settings.py``'s
# ``_EXCLUDED_SUFFIXES`` for the sibling Settings catalog; the wordings differ
# only because a tool spec carries no type information to fall back on, making
# the name the whole check.
_DENIED_FIELD_WORDS = frozenset(
    {
        # Credentials, which would be stored and served in the clear.
        "auth",
        "credential",
        "credentials",
        "key",
        "password",
        "secret",
        "token",
        # Endpoints: where the bot sends traffic. Retargeting a backend from a
        # markdown fragment is an SSRF surface, not a knob.
        "base",
        "endpoint",
        "host",
        "uri",
        "url",
        # Filesystem locations: where the bot reads and writes.
        "bin",
        "dir",
        "directory",
        "file",
        "path",
    }
)


@dataclass(frozen=True)
class ToolConfigField:
    """One operator-tunable knob on one tool.

    ``default`` is the shipped behavior and is mandatory: a tool must work with
    no fragment on disk, so there is no "unset" state to represent.
    """

    field: str
    label: str
    kind: str
    default: Any
    help: str = ""
    # KIND_CHOICE only: the closed set of accepted values.
    choices: tuple[str, ...] = ()
    # KIND_INT/KIND_FLOAT only.
    minimum: int | float | None = None
    maximum: int | float | None = None
    # KIND_TEXT only: render a textarea rather than a single-line input.
    multiline: bool = False


class ToolConfigSpecError(ValueError):
    """A tool declared a config spec that cannot be rendered or resolved."""


def _check_token_list(values: tuple[str, ...], *, what: str) -> None:
    if len(values) > MAX_VOCABULARY:
        raise ToolConfigSpecError(f"{what} is capped at {MAX_VOCABULARY} entries")
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not _TOKEN_RE.fullmatch(value):
            raise ToolConfigSpecError(f"invalid {what} entry: {value!r}")
        if value in seen:
            raise ToolConfigSpecError(f"duplicate {what} entry: {value!r}")
        seen.add(value)


def _validate_field(spec: ToolConfigField) -> None:
    if not isinstance(spec, ToolConfigField):
        raise ToolConfigSpecError(f"config spec entries must be ToolConfigField, got {spec!r}")
    if not FIELD_NAME_RE.fullmatch(spec.field):
        raise ToolConfigSpecError(f"invalid config field name: {spec.field!r}")
    word = spec.field.rsplit("_", 1)[-1]
    if word in _DENIED_FIELD_WORDS:
        raise ToolConfigSpecError(
            f"config field {spec.field!r} names a credential, endpoint, or path "
            f"({word!r}); those are environment-only and are never tool config"
        )
    if not spec.label.strip():
        raise ToolConfigSpecError(f"config field {spec.field!r} needs a label")
    if spec.kind not in CONFIG_KINDS:
        raise ToolConfigSpecError(f"unknown config kind {spec.kind!r} for {spec.field!r}")

    if spec.choices and spec.kind != KIND_CHOICE:
        raise ToolConfigSpecError(f"{spec.field!r}: choices only apply to a choice field")
    if spec.minimum is not None and spec.kind not in (KIND_INT, KIND_FLOAT):
        raise ToolConfigSpecError(f"{spec.field!r}: minimum only applies to a numeric field")
    if spec.maximum is not None and spec.kind not in (KIND_INT, KIND_FLOAT):
        raise ToolConfigSpecError(f"{spec.field!r}: maximum only applies to a numeric field")
    if spec.minimum is not None and spec.maximum is not None and spec.minimum > spec.maximum:
        raise ToolConfigSpecError(f"{spec.field!r}: minimum must be less than or equal to maximum")
    if spec.multiline and spec.kind != KIND_TEXT:
        raise ToolConfigSpecError(f"{spec.field!r}: multiline only applies to a text field")

    if spec.kind == KIND_CHOICE:
        if not spec.choices:
            raise ToolConfigSpecError(f"{spec.field!r}: a choice field needs choices")
        _check_token_list(spec.choices, what=f"{spec.field!r} choices")

    try:
        coerce_config_value(spec, spec.default)
    except ValueError as exc:
        raise ToolConfigSpecError(f"{spec.field!r}: invalid default: {exc}") from exc


def validate_config_spec(
    tool_name: str, spec: Sequence[ToolConfigField]
) -> tuple[ToolConfigField, ...]:
    """Boot-time validation, called by ``ToolRegistry.register``.

    Raises :class:`ToolConfigSpecError` so a malformed declaration fails at
    registration, where the author sees it, rather than degrading silently
    in a turn.
    """
    entries = tuple(spec)
    if len(entries) > MAX_FIELDS:
        raise ToolConfigSpecError(f"{tool_name}: config spec is capped at {MAX_FIELDS} fields")
    seen: set[str] = set()
    for entry in entries:
        try:
            _validate_field(entry)
        except ToolConfigSpecError as exc:
            raise ToolConfigSpecError(f"{tool_name}: {exc}") from exc
        if entry.field in seen:
            raise ToolConfigSpecError(f"{tool_name}: duplicate config field {entry.field!r}")
        seen.add(entry.field)
    return entries


def coerce_config_value(spec: ToolConfigField, raw: Any) -> Any:
    """Convert one frontmatter value into the value a handler receives.

    Raises ``ValueError`` on anything the field cannot represent; the runtime
    loader turns that into "use the default".
    """
    return coerce_scalar(
        spec.kind,
        raw,
        choices=spec.choices,
        minimum=spec.minimum,
        maximum=spec.maximum,
    )


def resolve_config(
    spec: Sequence[ToolConfigField],
    overrides: Mapping[str, Any],
    *,
    strict: bool = False,
    on_issue: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Resolve operator overrides over the spec's defaults.

    The single resolution chokepoint: every loader goes through it, so what a
    fragment says and what a handler receives can never drift apart.

    ``strict`` picks the failure direction. ``True``: an unknown key or an
    uncoercible value raises (a writer that validates before persisting passes
    this, because carrying a typo forward hides it forever). The runtime passes
    ``False``: an unknown key is reported and ignored, and a bad value falls
    back to that field's default, because a hand-edited fragment must never
    take a tool down.
    """

    def report(message: str) -> None:
        if on_issue is not None:
            on_issue(message)

    by_field = {entry.field: entry for entry in spec}
    resolved: dict[str, Any] = {}

    for key in overrides:
        if str(key) not in by_field:
            message = f"unknown config key {str(key)!r}"
            if strict:
                raise ValueError(message)
            report(f"{message}; ignoring it")

    for entry in spec:
        if entry.field in overrides:
            raw = overrides[entry.field]
            try:
                resolved[entry.field] = coerce_config_value(entry, raw)
                continue
            except ValueError as exc:
                message = f"invalid value for {entry.field!r}: {exc}"
                if strict:
                    raise ValueError(message) from exc
                report(f"{message}; using the default")
        resolved[entry.field] = coerce_config_value(entry, entry.default)
    return resolved


def default_config(spec: Sequence[ToolConfigField]) -> dict[str, Any]:
    """The shipped behavior: every field at its declared default."""
    return resolve_config(spec, {})
