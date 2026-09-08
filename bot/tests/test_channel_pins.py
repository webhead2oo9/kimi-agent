from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from config.fragments.channel_pins import (
    ChannelAccessPolicy,
    ChannelBlockedToolsLoadError,
    channel_access_allowed,
    channel_access_scope_id,
    filter_pins_to_searchable,
    load_channel_access_policy,
    load_channel_auto_thread,
    load_channel_blocked_tools,
    load_channel_pinned_tools,
    load_channel_thread_auto_respond,
    load_channel_thread_handoff,
    resolve_tristate,
)
from config.fragments.prompt import load_fragment
from tools.registry import ToolRegistry
from trust.tiers import TrustTier


def _write_fragment(config_dir: Path, channel_id: str, text: str) -> None:
    channels = config_dir / "channels"
    channels.mkdir(parents=True, exist_ok=True)
    (channels / f"{channel_id}.md").write_text(text, encoding="utf-8")


def _member(user_id: int, *role_ids: int) -> Any:
    return type(
        "MemberEvidence",
        (),
        {
            "id": user_id,
            "roles": [type("RoleEvidence", (), {"id": role_id})() for role_id in role_ids],
        },
    )()


def test_missing_channel_fragment_is_unrestricted(tmp_path: Path) -> None:
    assert load_channel_access_policy("100", config_dir=tmp_path) == ChannelAccessPolicy()
    assert channel_access_allowed(
        type("ChannelEvidence", (), {"id": 100})(),
        _member(1),
        config_dir=tmp_path,
    )


def test_valid_channel_fragment_without_admission_keys_is_unrestricted(tmp_path: Path) -> None:
    _write_fragment(tmp_path, "100", "---\npinned_tools: []\n---\nbody\n")

    policy = load_channel_access_policy("100", config_dir=tmp_path)

    assert not policy.configured
    assert policy.allows(_member(1))


@pytest.mark.parametrize(
    ("user", "expected"),
    [
        (_member(10), True),
        (_member(99, 20), True),
        (_member(99, 98, 97), False),
    ],
)
def test_channel_access_user_and_role_lists_have_or_semantics(
    tmp_path: Path, user: object, expected: bool
) -> None:
    _write_fragment(
        tmp_path,
        "100",
        "---\nallowed_user_ids: [10]\nallowed_role_ids: [20, 30]\n---\nbody\n",
    )

    assert (
        channel_access_allowed(
            type("ChannelEvidence", (), {"id": 100})(), user, config_dir=tmp_path
        )
        is expected
    )


def test_channel_access_does_not_implicitly_admit_staff(tmp_path: Path) -> None:
    _write_fragment(tmp_path, "100", "---\nallowed_role_ids: [20]\n---\nbody\n")
    globally_staff = _member(999, 888)
    globally_staff.guild_permissions = type("Permissions", (), {"administrator": True})()
    globally_staff.is_owner = True
    globally_staff.trust_tier = TrustTier.STAFF

    assert not channel_access_allowed(
        type("ChannelEvidence", (), {"id": 100})(),
        globally_staff,
        config_dir=tmp_path,
    )


@pytest.mark.parametrize(
    "frontmatter",
    [
        "allowed_user_ids: []",
        "allowed_role_ids: []",
        "allowed_user_ids: not-a-list",
        "allowed_role_ids: [false, null, nope]",
        "allowed_role_ids: [unclosed",
    ],
)
def test_empty_or_malformed_configured_channel_access_fails_closed(
    tmp_path: Path, frontmatter: str
) -> None:
    _write_fragment(tmp_path, "100", f"---\n{frontmatter}\n---\nbody\n")

    policy = load_channel_access_policy("100", config_dir=tmp_path)

    assert policy.configured
    assert not policy.allows(_member(1, 2))


def test_mixed_valid_and_invalid_access_entries_keep_only_valid_ids(tmp_path: Path) -> None:
    _write_fragment(
        tmp_path,
        "100",
        "---\nallowed_user_ids: [10, false, nope]\n"
        "allowed_role_ids: [20, null, ' 30 ']\n---\nbody\n",
    )

    policy = load_channel_access_policy("100", config_dir=tmp_path)

    assert policy.allowed_user_ids == frozenset({"10"})
    assert policy.allowed_role_ids == frozenset({"20", "30"})
    assert policy.allows(_member(10))
    assert policy.allows(_member(99, 30))
    assert not policy.allows(_member(99, 40))


@pytest.mark.parametrize(
    "invalid_frontmatter",
    [
        "pinned_tools: [unclosed",
        (
            "defaults: &gate\n  allowed_user_ids: [10]\n!!merge arbitrary: *gate\n"
            "pinned_tools: [unclosed"
        ),
        "[]\nallowed_user_ids: [10]",
        "- unrelated\n--- # YAML document marker\nallowed_role_ids: [20]",
        "plain scalar",
        "[]",
    ],
)
@pytest.mark.parametrize(
    "previous_frontmatter",
    [None, "pinned_tools: []", "allowed_user_ids: [10]"],
)
def test_invalid_policy_always_denies_regardless_of_prior_policy(
    tmp_path: Path, invalid_frontmatter: str, previous_frontmatter: str | None
) -> None:
    if previous_frontmatter is not None:
        _write_fragment(tmp_path, "100", f"---\n{previous_frontmatter}\n---\nbody\n")
        assert load_channel_access_policy("100", config_dir=tmp_path).allows(_member(10))

    _write_fragment(tmp_path, "100", f"---\n{invalid_frontmatter}\n---\nbody\n")
    policy = load_channel_access_policy("100", config_dir=tmp_path)

    assert policy.configured
    assert not policy.allows(_member(10, 20))


@pytest.mark.parametrize(
    ("frontmatter", "allowed_user_ids", "allowed_role_ids"),
    [
        (
            "defaults: &gate\n  allowed_user_ids: [10]\n<<: *gate",
            frozenset({"10"}),
            frozenset(),
        ),
        (
            "defaults: &gate\n  allowed_role_ids: [20]\n!!merge arbitrary: *gate",
            frozenset(),
            frozenset({"20"}),
        ),
    ],
)
def test_valid_yaml_merges_are_resolved_by_the_safe_parser(
    tmp_path: Path,
    frontmatter: str,
    allowed_user_ids: frozenset[str],
    allowed_role_ids: frozenset[str],
) -> None:
    _write_fragment(tmp_path, "100", f"---\n{frontmatter}\n---\nbody\n")

    policy = load_channel_access_policy("100", config_dir=tmp_path)

    assert policy.configured
    assert policy.allowed_user_ids == allowed_user_ids
    assert policy.allowed_role_ids == allowed_role_ids


def test_deleting_channel_fragment_restores_missing_file_convention(tmp_path: Path) -> None:
    _write_fragment(tmp_path, "100", "---\nallowed_user_ids: [10]\n---\nbody\n")
    path = tmp_path / "channels" / "100.md"
    assert load_channel_access_policy("100", config_dir=tmp_path).configured

    path.unlink()

    assert load_channel_access_policy("100", config_dir=tmp_path) == ChannelAccessPolicy()


def test_unreadable_policy_denies_even_after_permissive_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_fragment(tmp_path, "100", "---\nallowed_user_ids: [10]\n---\nbody\n")
    assert load_channel_access_policy("100", config_dir=tmp_path).allows(_member(10))

    def fail_read(_path: Path, encoding: str | None = None, errors: str | None = None) -> str:
        del encoding, errors
        raise OSError("synthetic read failure")

    monkeypatch.setattr(Path, "read_text", fail_read)

    policy = load_channel_access_policy("100", config_dir=tmp_path)
    assert policy.configured
    assert not policy.allows(_member(10))


def test_invalid_utf8_policy_denies_on_cold_load_and_recovers_when_corrected(
    tmp_path: Path,
) -> None:
    path = tmp_path / "channels" / "100.md"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xff")

    assert not load_channel_access_policy("100", config_dir=tmp_path).allows(_member(10, 20))

    _write_fragment(tmp_path, "100", "---\nallowed_user_ids: [10]\n---\nbody\n")

    assert load_channel_access_policy("100", config_dir=tmp_path).allows(_member(10, 20))


def test_invalid_policy_recovers_as_soon_as_valid_yaml_is_restored(tmp_path: Path) -> None:
    _write_fragment(tmp_path, "100", "---\npinned_tools: [unclosed\n---\nbody\n")
    assert not load_channel_access_policy("100", config_dir=tmp_path).allows(_member(10, 20))

    _write_fragment(tmp_path, "100", "---\nallowed_role_ids: [20]\n---\nbody\n")

    assert load_channel_access_policy("100", config_dir=tmp_path).allows(_member(10, 20))


def test_thread_access_inherits_parent_and_missing_parent_fails_closed(tmp_path: Path) -> None:
    _write_fragment(tmp_path, "100", "---\nallowed_role_ids: [20]\n---\nbody\n")
    thread = type("ThreadEvidence", (), {"id": 200, "parent_id": 100})()
    broken_thread = type("ThreadEvidence", (), {"id": 200, "parent_id": None})()

    assert channel_access_scope_id(thread) == "100"
    assert channel_access_allowed(thread, _member(1, 20), config_dir=tmp_path)
    assert not channel_access_allowed(thread, _member(1, 99), config_dir=tmp_path)
    assert channel_access_scope_id(broken_thread) == ""
    assert not channel_access_allowed(broken_thread, _member(1, 20), config_dir=tmp_path)


def test_role_gate_denies_when_member_or_role_evidence_is_missing(tmp_path: Path) -> None:
    _write_fragment(tmp_path, "100", "---\nallowed_role_ids: [20]\n---\nbody\n")
    channel = type("ChannelEvidence", (), {"id": 100})()

    assert not channel_access_allowed(channel, object(), config_dir=tmp_path)
    assert not channel_access_allowed(
        channel,
        type("UserEvidence", (), {"id": 1})(),
        config_dir=tmp_path,
    )

    _write_fragment(tmp_path, "100", "---\nallowed_user_ids: [1]\n---\nbody\n")
    assert channel_access_allowed(
        channel,
        type("UserEvidence", (), {"id": 1})(),
        config_dir=tmp_path,
    )


def test_load_pins_from_frontmatter(tmp_path: Path) -> None:
    _write_fragment(
        tmp_path,
        "100",
        "---\npinned_tools: [move_to_thread, leave_thread]\n---\nYou are in #off-topic.\n",
    )

    pins = load_channel_pinned_tools("100", config_dir=tmp_path)

    assert pins == frozenset({"move_to_thread", "leave_thread"})


def test_load_pins_missing_file_is_empty(tmp_path: Path) -> None:
    assert load_channel_pinned_tools("100", config_dir=tmp_path) == frozenset()


def test_load_pins_without_frontmatter_is_empty(tmp_path: Path) -> None:
    _write_fragment(tmp_path, "100", "You are in #off-topic.\n")

    assert load_channel_pinned_tools("100", config_dir=tmp_path) == frozenset()


def test_load_pins_rejects_non_snowflake_ids(tmp_path: Path) -> None:
    _write_fragment(tmp_path, "100", "---\npinned_tools: [move_to_thread]\n---\nbody\n")

    assert load_channel_pinned_tools("", config_dir=tmp_path) == frozenset()
    assert load_channel_pinned_tools("abc", config_dir=tmp_path) == frozenset()
    assert load_channel_pinned_tools("../100", config_dir=tmp_path) == frozenset()
    assert load_channel_pinned_tools("100\n", config_dir=tmp_path) == frozenset()


def test_load_pins_ignores_invalid_utf8(tmp_path: Path) -> None:
    path = tmp_path / "channels" / "100.md"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xff")

    assert load_channel_pinned_tools("100", config_dir=tmp_path) == frozenset()


def test_load_pins_ignores_non_list_value(tmp_path: Path) -> None:
    _write_fragment(tmp_path, "100", "---\npinned_tools: move_to_thread\n---\nbody\n")

    assert load_channel_pinned_tools("100", config_dir=tmp_path) == frozenset()


def test_load_pins_drops_invalid_entries(tmp_path: Path) -> None:
    _write_fragment(
        tmp_path,
        "100",
        "---\npinned_tools: [move_to_thread, 7, '', 'bad name', \"trailing\\n\", null]\n"
        "---\nbody\n",
    )

    pins = load_channel_pinned_tools("100", config_dir=tmp_path)

    assert pins == frozenset({"move_to_thread"})


def test_load_pins_caps_entry_count(tmp_path: Path) -> None:
    names = [f"tool_{i}" for i in range(20)]
    _write_fragment(
        tmp_path,
        "100",
        "---\npinned_tools: [" + ", ".join(names) + "]\n---\nbody\n",
    )

    pins = load_channel_pinned_tools("100", config_dir=tmp_path)

    assert len(pins) == 16
    assert pins == frozenset(names[:16])


def test_load_blocked_tools_from_frontmatter(tmp_path: Path) -> None:
    _write_fragment(
        tmp_path,
        "100",
        "---\nblocked_tools: [lookup_game_info, staff_search]\n---\nbody\n",
    )

    blocked = load_channel_blocked_tools("100", config_dir=tmp_path)

    assert blocked == frozenset({"lookup_game_info", "staff_search"})


def test_load_blocked_tools_missing_or_invalid_id_is_empty(tmp_path: Path) -> None:
    assert load_channel_blocked_tools("100", config_dir=tmp_path) == frozenset()
    assert load_channel_blocked_tools("", config_dir=tmp_path) == frozenset()
    assert load_channel_blocked_tools("../100", config_dir=tmp_path) == frozenset()
    _write_fragment(tmp_path, "100", "no frontmatter\n")
    assert load_channel_blocked_tools("100", config_dir=tmp_path) == frozenset()


def test_load_blocked_tools_invalid_first_value_fails_closed(tmp_path: Path) -> None:
    _write_fragment(tmp_path, "101", "---\nblocked_tools: not_a_list\n---\nbody\n")

    with pytest.raises(ChannelBlockedToolsLoadError, match="channel tool policy"):
        load_channel_blocked_tools("101", config_dir=tmp_path)


def test_load_blocked_tools_retains_invalid_reload_but_missing_or_omitted_clears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_fragment(tmp_path, "102", "---\nblocked_tools: [dangerous_tool]\n---\nbody\n")
    path = tmp_path / "channels" / "102.md"
    expected = frozenset({"dangerous_tool"})
    assert load_channel_blocked_tools("102", config_dir=tmp_path) == expected

    _write_fragment(tmp_path, "102", "---\nblocked_tools: not_a_list\n---\nbody\n")
    assert load_channel_blocked_tools("102", config_dir=tmp_path) == expected

    original_read_text = Path.read_text

    def unreadable(self: Path, *args: Any, **kwargs: Any) -> str:
        if self == path:
            raise PermissionError("config cannot be read")
        return original_read_text(self, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", unreadable)
        assert load_channel_blocked_tools("102", config_dir=tmp_path) == expected

    path.unlink()
    assert load_channel_blocked_tools("102", config_dir=tmp_path) == frozenset()

    path.write_text("", encoding="utf-8")
    assert load_channel_blocked_tools("102", config_dir=tmp_path) == frozenset()

    _write_fragment(tmp_path, "102", "---\nblocked_tools: [dangerous_tool]\n---\nbody\n")
    assert load_channel_blocked_tools("102", config_dir=tmp_path) == expected
    path.write_text("body only\n", encoding="utf-8")
    assert load_channel_blocked_tools("102", config_dir=tmp_path) == frozenset()


def test_load_auto_thread_reads_both_thresholds(tmp_path: Path) -> None:
    _write_fragment(
        tmp_path,
        "100",
        "---\nauto_thread_min_lines: 4\nauto_thread_min_chars: 600\n---\nbody\n",
    )

    cfg = load_channel_auto_thread("100", config_dir=tmp_path)

    assert cfg is not None
    assert cfg.min_lines == 4
    assert cfg.min_chars == 600


def test_load_auto_thread_one_threshold_present(tmp_path: Path) -> None:
    _write_fragment(tmp_path, "100", "---\nauto_thread_min_lines: 4\n---\nbody\n")

    cfg = load_channel_auto_thread("100", config_dir=tmp_path)

    assert cfg is not None
    assert cfg.min_lines == 4
    assert cfg.min_chars is None


def test_load_auto_thread_not_enrolled_without_keys(tmp_path: Path) -> None:
    _write_fragment(tmp_path, "100", "---\npinned_tools: [move_to_thread]\n---\nbody\n")

    assert load_channel_auto_thread("100", config_dir=tmp_path) is None


def test_load_auto_thread_missing_file_is_none(tmp_path: Path) -> None:
    assert load_channel_auto_thread("100", config_dir=tmp_path) is None


def test_load_auto_thread_rejects_non_snowflake_ids(tmp_path: Path) -> None:
    _write_fragment(tmp_path, "100", "---\nauto_thread_min_lines: 4\n---\nbody\n")

    assert load_channel_auto_thread("", config_dir=tmp_path) is None
    assert load_channel_auto_thread("abc", config_dir=tmp_path) is None
    assert load_channel_auto_thread("../100", config_dir=tmp_path) is None


def test_load_auto_thread_ignores_non_positive_and_bool(tmp_path: Path) -> None:
    _write_fragment(
        tmp_path,
        "100",
        "---\nauto_thread_min_lines: 0\nauto_thread_min_chars: true\n---\nbody\n",
    )

    assert load_channel_auto_thread("100", config_dir=tmp_path) is None


def test_load_auto_thread_coerces_string_value(tmp_path: Path) -> None:
    _write_fragment(tmp_path, "100", "---\nauto_thread_min_lines: '4'\n---\nbody\n")

    cfg = load_channel_auto_thread("100", config_dir=tmp_path)

    assert cfg is not None
    assert cfg.min_lines == 4


def test_load_auto_thread_always_enrolls_without_thresholds(tmp_path: Path) -> None:
    _write_fragment(tmp_path, "100", "---\nauto_thread_always: true\n---\nbody\n")

    cfg = load_channel_auto_thread("100", config_dir=tmp_path)

    assert cfg is not None
    assert cfg.always is True
    assert cfg.min_lines is None
    assert cfg.min_chars is None


def test_load_auto_thread_always_coexists_with_thresholds(tmp_path: Path) -> None:
    _write_fragment(
        tmp_path,
        "100",
        "---\nauto_thread_always: true\nauto_thread_min_chars: 600\n---\nbody\n",
    )

    cfg = load_channel_auto_thread("100", config_dir=tmp_path)

    assert cfg is not None
    assert cfg.always is True
    assert cfg.min_chars == 600


def test_load_auto_thread_always_requires_real_bool(tmp_path: Path) -> None:
    # A non-bool value is ignored (fail-closed); with no thresholds either,
    # the channel is simply not enrolled. `false` likewise does not enroll.
    for value in ("'true'", "1", "false"):
        _write_fragment(tmp_path, "100", f"---\nauto_thread_always: {value}\n---\nbody\n")
        assert load_channel_auto_thread("100", config_dir=tmp_path) is None


async def _noop_handler(args: dict, ctx: object) -> str:
    _ = (args, ctx)
    return "ok"


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        "core_tool",
        "always visible",
        {"type": "object", "properties": {}},
        _noop_handler,
        min_tier=TrustTier.MEMBER,
    )
    registry.register(
        "move_to_thread",
        "searchable member tool",
        {"type": "object", "properties": {}},
        _noop_handler,
        min_tier=TrustTier.MEMBER,
        searchable=True,
    )
    registry.register(
        "staff_search",
        "searchable staff tool",
        {"type": "object", "properties": {}},
        _noop_handler,
        min_tier=TrustTier.STAFF,
        searchable=True,
    )
    return registry


def test_load_thread_handoff_reads_literal_booleans(tmp_path: Path) -> None:
    _write_fragment(tmp_path, "100", "---\nthread_handoff: false\n---\nBody.\n")
    assert load_channel_thread_handoff("100", config_dir=tmp_path) is False

    _write_fragment(tmp_path, "100", "---\nthread_handoff: true\n---\nBody.\n")
    assert load_channel_thread_handoff("100", config_dir=tmp_path) is True


def test_load_thread_handoff_absent_or_malformed_is_none(tmp_path: Path) -> None:
    assert load_channel_thread_handoff("100", config_dir=tmp_path) is None
    assert load_channel_thread_handoff("", config_dir=tmp_path) is None
    assert load_channel_thread_handoff("../100", config_dir=tmp_path) is None
    _write_fragment(tmp_path, "100", "---\npinned_tools: [render_diagram]\n---\nBody.\n")
    assert load_channel_thread_handoff("100", config_dir=tmp_path) is None
    # A typo'd value must fall back to the wider scope, never flip the channel.
    _write_fragment(tmp_path, "100", "---\nthread_handoff: 'false'\n---\nBody.\n")
    assert load_channel_thread_handoff("100", config_dir=tmp_path) is None


def test_resolve_tristate_precedence() -> None:
    # Channel wins over guild; default is on.
    assert resolve_tristate(None, None) is True
    assert resolve_tristate(None, False) is False
    assert resolve_tristate(None, True) is True
    assert resolve_tristate(False, None) is False
    assert resolve_tristate(True, False) is True
    assert resolve_tristate(False, True) is False


def test_load_thread_auto_respond_reads_literal_booleans(tmp_path: Path) -> None:
    _write_fragment(tmp_path, "100", "---\nthread_auto_respond: false\n---\nBody.\n")
    assert load_channel_thread_auto_respond("100", config_dir=tmp_path) is False

    _write_fragment(tmp_path, "100", "---\nthread_auto_respond: true\n---\nBody.\n")
    assert load_channel_thread_auto_respond("100", config_dir=tmp_path) is True


def test_load_thread_auto_respond_absent_or_malformed_is_none(tmp_path: Path) -> None:
    assert load_channel_thread_auto_respond("100", config_dir=tmp_path) is None
    assert load_channel_thread_auto_respond("", config_dir=tmp_path) is None
    assert load_channel_thread_auto_respond("../100", config_dir=tmp_path) is None
    _write_fragment(tmp_path, "100", "---\nthread_handoff: false\n---\nBody.\n")
    assert load_channel_thread_auto_respond("100", config_dir=tmp_path) is None
    # A typo'd value falls back to the wider scope, never flips the channel.
    _write_fragment(tmp_path, "100", "---\nthread_auto_respond: 'false'\n---\nBody.\n")
    assert load_channel_thread_auto_respond("100", config_dir=tmp_path) is None


def test_the_two_thread_tristates_are_independent(tmp_path: Path) -> None:
    _write_fragment(
        tmp_path,
        "100",
        "---\nthread_handoff: true\nthread_auto_respond: false\n---\nBody.\n",
    )
    assert load_channel_thread_handoff("100", config_dir=tmp_path) is True
    assert load_channel_thread_auto_respond("100", config_dir=tmp_path) is False


def test_filter_pins_keeps_searchable_tools_at_tier() -> None:
    pins = frozenset({"move_to_thread", "staff_search", "core_tool", "no_such_tool"})

    member = filter_pins_to_searchable(pins, _registry(), TrustTier.MEMBER)
    staff = filter_pins_to_searchable(pins, _registry(), TrustTier.STAFF)

    assert member == frozenset({"move_to_thread"})
    assert staff == frozenset({"move_to_thread", "staff_search"})


def test_load_fragment_strips_frontmatter(tmp_path: Path) -> None:
    _write_fragment(
        tmp_path,
        "100",
        "---\npinned_tools: [move_to_thread]\n---\nYou are in #off-topic.\n",
    )

    block = load_fragment(tmp_path / "channels", "100", header="Channel Instructions")

    assert block == "## Channel Instructions\nYou are in #off-topic."


def test_load_fragment_frontmatter_only_file_is_empty(tmp_path: Path) -> None:
    _write_fragment(tmp_path, "100", "---\npinned_tools: [move_to_thread]\n---\n")

    block = load_fragment(tmp_path / "channels", "100", header="Channel Instructions")

    assert block == ""
