from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from config.fragments.channel_pins import (
    ChannelBlockedToolsLoadError,
    filter_pins_to_searchable,
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
