"""The per-tool config spec type, its coercion, and its registry round-trip.

Every spec here is a scratch one, deliberately: the mechanism has to be
exercisable end to end without a customer, or a change to it would always be
debugged through whichever tool happens to use it. The shapes mirror what the
real customer declares: ``discord_text_search``'s numeric ``max_results``
(``tests/test_discord_text_search_tool.py`` covers that tool's own use of it).
"""

from __future__ import annotations

import asyncio

import pytest

from tools.config_spec import (
    KIND_BOOL,
    KIND_CHOICE,
    KIND_FLOAT,
    KIND_INT,
    KIND_TEXT,
    ToolConfigField,
    ToolConfigSpecError,
    coerce_config_value,
    default_config,
    resolve_config,
    validate_config_spec,
)
from tools.registry import MessageContext, ToolRegistry
from trust.tiers import TrustTier

MODE = ToolConfigField(
    field="mode",
    label="Search mode",
    kind=KIND_CHOICE,
    default="failover",
    choices=("failover", "blend"),
    help="How configured providers are combined.",
)
NOTICE = ToolConfigField(
    field="result_notice",
    label="Result notice",
    kind=KIND_TEXT,
    default="",
)
MAX_RESULTS = ToolConfigField(
    field="max_results",
    label="Max results",
    kind=KIND_INT,
    default=5,
    minimum=1,
    maximum=25,
)
SPEC = (MODE, NOTICE, MAX_RESULTS)


async def _handler(args: dict, ctx: MessageContext) -> str:
    return "ran"


# ── Spec validation ──────────────────────────────────────────────────────────


def test_validate_accepts_the_shape_the_customer_declares() -> None:
    assert validate_config_spec("discord_text_search", SPEC) == SPEC


@pytest.mark.parametrize(
    ("field", "reason"),
    [
        (
            ToolConfigField(field="Mode", label="Mode", kind=KIND_TEXT, default=""),
            "invalid config field name",
        ),
        (
            ToolConfigField(field="mode", label="  ", kind=KIND_TEXT, default=""),
            "needs a label",
        ),
        (
            ToolConfigField(field="mode", label="Mode", kind="colour", default=""),
            "unknown config kind",
        ),
        (
            ToolConfigField(field="mode", label="Mode", kind=KIND_CHOICE, default="a"),
            "a choice field needs choices",
        ),
        (
            ToolConfigField(
                field="mode", label="Mode", kind=KIND_CHOICE, default="c", choices=("a", "b")
            ),
            "invalid default",
        ),
        (
            ToolConfigField(field="count", label="Count", kind=KIND_INT, default=0, minimum=1),
            "must be at least 1",
        ),
        (
            ToolConfigField(field="count", label="Count", kind=KIND_INT, default="5"),
            "expected an integer",
        ),
        (
            ToolConfigField(field="mode", label="Mode", kind=KIND_TEXT, default="", choices=("a",)),
            "choices only apply",
        ),
        (
            ToolConfigField(field="mode", label="Mode", kind=KIND_TEXT, default="", minimum=1),
            "minimum only applies",
        ),
        (
            ToolConfigField(field="mode", label="Mode", kind=KIND_TEXT, default="", maximum=1),
            "maximum only applies",
        ),
        (
            ToolConfigField(
                field="count",
                label="Count",
                kind=KIND_INT,
                default=5,
                minimum=5,
                maximum=4,
            ),
            "minimum must be less than or equal to maximum",
        ),
        (
            ToolConfigField(field="count", label="Count", kind=KIND_INT, default=11, maximum=10),
            "must be at most 10",
        ),
        (
            ToolConfigField(
                field="mode", label="Mode", kind=KIND_BOOL, default=True, multiline=True
            ),
            "multiline only applies",
        ),
    ],
)
def test_validate_rejects_malformed_fields(field: ToolConfigField, reason: str) -> None:
    with pytest.raises(ToolConfigSpecError, match=reason):
        validate_config_spec("demo_tool", (field,))


def test_validate_rejects_duplicate_fields() -> None:
    with pytest.raises(ToolConfigSpecError, match="duplicate config field"):
        validate_config_spec("demo_tool", (MODE, MODE))


def test_validate_names_the_tool_so_a_boot_failure_is_actionable() -> None:
    bad = ToolConfigField(field="mode", label="Mode", kind="colour", default="")
    with pytest.raises(ToolConfigSpecError, match="^demo_tool: "):
        validate_config_spec("demo_tool", (bad,))


@pytest.mark.parametrize(
    "name",
    [
        # Credentials.
        "api_key",
        "key",
        "api_token",
        "token",
        "client_secret",
        "secret",
        "password",
        "auth",
        "service_credential",
        "credentials",
        # Endpoints.
        "base_url",
        "url",
        "api_base",
        "search_endpoint",
        "proxy_host",
        "reader_uri",
        # Filesystem locations.
        "report_dir",
        "output_directory",
        "cache_path",
        "python_bin",
        "index_file",
    ],
)
def test_validate_rejects_a_field_that_would_manage_a_secret_endpoint_or_path(
    name: str,
) -> None:
    """The invariant is enforced at registration, not left as prose.

    A tool config value is stored in plaintext and handed to the handler. A
    spec declaring an endpoint or a credential there would leak it and would
    let a markdown fragment retarget where the bot connects, so the tool's
    author has to hear about it at boot.
    """
    field = ToolConfigField(field=name, label="Nope", kind=KIND_TEXT, default="")

    with pytest.raises(ToolConfigSpecError, match="environment-only"):
        validate_config_spec("demo_tool", (field,))


@pytest.mark.parametrize("name", ["mode", "search_providers", "keyword_boost", "max_results"])
def test_the_denylist_matches_whole_words_not_substrings(name: str) -> None:
    """``keyword_boost`` ends in ``boost``, not ``key``, so it must stay legal."""
    field = ToolConfigField(field=name, label="Fine", kind=KIND_TEXT, default="")

    assert validate_config_spec("demo_tool", (field,)) == (field,)


# ── Coercion ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("field", "raw", "expected"),
    [
        (ToolConfigField(field="f", label="F", kind=KIND_BOOL, default=False), True, True),
        (ToolConfigField(field="f", label="F", kind=KIND_INT, default=1), 7, 7),
        (
            ToolConfigField(field="f", label="F", kind=KIND_INT, default=5, minimum=1, maximum=10),
            10,
            10,
        ),
        (ToolConfigField(field="f", label="F", kind=KIND_FLOAT, default=1.0), 2, 2.0),
        (ToolConfigField(field="f", label="F", kind=KIND_TEXT, default=""), "  x ", "x"),
        (MODE, "blend", "blend"),
    ],
)
def test_coerce_accepts_valid_values(field: ToolConfigField, raw: object, expected: object) -> None:
    assert coerce_config_value(field, raw) == expected


@pytest.mark.parametrize(
    ("field", "raw"),
    [
        (ToolConfigField(field="f", label="F", kind=KIND_BOOL, default=False), "yes"),
        # A bool is an int in Python; a config value is not the place for that.
        (ToolConfigField(field="f", label="F", kind=KIND_INT, default=1), True),
        (ToolConfigField(field="f", label="F", kind=KIND_INT, default=1), 1.5),
        (
            ToolConfigField(field="f", label="F", kind=KIND_INT, default=5, minimum=1),
            0,
        ),
        (
            ToolConfigField(field="f", label="F", kind=KIND_INT, default=5, maximum=10),
            11,
        ),
        (ToolConfigField(field="f", label="F", kind=KIND_FLOAT, default=1.0), float("inf")),
        (ToolConfigField(field="f", label="F", kind=KIND_TEXT, default=""), 3),
        (MODE, "swarm"),
        (MODE, ["blend"]),
    ],
)
def test_coerce_rejects_invalid_values(field: ToolConfigField, raw: object) -> None:
    with pytest.raises(ValueError):
        coerce_config_value(field, raw)


# ── Resolution ───────────────────────────────────────────────────────────────


def test_defaults_resolve_without_overrides() -> None:
    assert default_config(SPEC) == {
        "mode": "failover",
        "result_notice": "",
        "max_results": 5,
    }


def test_resolve_layers_overrides_over_defaults() -> None:
    resolved = resolve_config(SPEC, {"mode": "blend", "max_results": 12})

    assert resolved == {
        "mode": "blend",
        "result_notice": "",
        "max_results": 12,
    }


def test_lenient_resolution_is_per_key() -> None:
    issues: list[str] = []
    resolved = resolve_config(
        SPEC,
        {"mode": "nonsense", "typo": 1, "result_notice": "hi"},
        on_issue=issues.append,
    )

    # One bad value never costs the operator their other overrides.
    assert resolved["mode"] == "failover"
    assert resolved["result_notice"] == "hi"
    assert any("unknown config key 'typo'" in issue for issue in issues)
    assert any("invalid value for 'mode'" in issue for issue in issues)


@pytest.mark.parametrize(
    "overrides",
    [{"typo": 1}, {"mode": "nonsense"}],
)
def test_strict_resolution_raises_instead(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        resolve_config(SPEC, overrides, strict=True)


# ── Registry round-trip ──────────────────────────────────────────────────────


def test_register_stores_the_validated_spec_and_config_specs_reports_it() -> None:
    registry = ToolRegistry()
    registry.register("plain_tool", "Plain.", {}, _handler)
    registry.register("configurable", "Configurable.", {}, _handler, config_spec=SPEC)

    assert registry.config_specs() == {"configurable": SPEC}
    entry = next(e for e in registry.get_all_tools() if e.name == "configurable")
    assert entry.config_spec == SPEC
    # A list argument is normalized to a tuple, so the entry stays immutable.
    assert isinstance(entry.config_spec, tuple)


def test_register_rejects_a_malformed_spec_at_registration_time() -> None:
    registry = ToolRegistry()
    bad = ToolConfigField(field="mode", label="Mode", kind=KIND_CHOICE, default="x")

    with pytest.raises(ToolConfigSpecError):
        registry.register("broken", "Broken.", {}, _handler, config_spec=[bad])

    assert registry.is_registered("broken") is False


def test_config_specs_reports_searchable_tools_and_ignores_unspecced_ones() -> None:
    registry = ToolRegistry()
    registry.register("hidden_tool", "Searchable.", {}, _handler, searchable=True, config_spec=SPEC)
    registry.register("plain_tool", "Plain.", {}, _handler)

    assert registry.config_specs() == {"hidden_tool": SPEC}


def test_the_spec_survives_clone_without_and_skill_replacement() -> None:
    registry = ToolRegistry()
    registry.register("configurable", "Configurable.", {}, _handler, config_spec=SPEC)
    registry.register("stripped", "Stripped.", {}, _handler, config_spec=SPEC)

    clone = registry.clone_without({"stripped"})
    assert clone.config_specs() == {"configurable": SPEC}

    entries = [e for e in registry.get_all_tools() if e.name == "configurable"]
    registry.replace_skill_tools([])
    # Skill replacement only ever removes skill-backed tools, so a Python tool's
    # spec is untouched by a reload.
    assert registry.config_specs() == {"configurable": SPEC, "stripped": SPEC}
    assert entries[0].config_spec == SPEC


def test_resolved_config_reaches_a_handler_through_the_message_context() -> None:
    """End of the rail: what prepare_turn stashes is what the handler reads."""
    registry = ToolRegistry()
    seen: dict[str, object] = {}

    async def handler(args: dict, ctx: MessageContext) -> str:
        seen.update(ctx.tool_configs.get("configurable") or {})
        return "ran"

    registry.register("configurable", "Configurable.", {}, handler, config_spec=SPEC)
    ctx = MessageContext(
        user_id="1",
        user_name="Tester",
        guild_id="111",
        channel_id="222",
        thread_id=None,
        trust_tier=TrustTier.MEMBER,
        tool_configs={"configurable": resolve_config(SPEC, {"mode": "blend"})},
    )

    assert asyncio.run(registry.dispatch("configurable", {}, ctx)) == "ran"
    assert seen["mode"] == "blend"
    assert seen["max_results"] == 5


def test_a_bare_context_leaves_a_handler_with_no_config_rather_than_a_crash() -> None:
    ctx = MessageContext(
        user_id="1",
        user_name="Tester",
        guild_id=None,
        channel_id="222",
        thread_id=None,
        trust_tier=TrustTier.MEMBER,
    )

    assert (ctx.tool_configs.get("configurable") or {}) == {}
