from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from config.fragments.guild_config import (
    load_guild_blocked_tools,
    load_guild_pinned_tools,
    load_guild_thread_auto_respond,
    load_guild_thread_targets,
    load_guild_thread_handoff,
    load_guild_trust,
    read_guild_frontmatter,
)
from config.fragments.prompt import load_fragment
from trust.resolver import EMPTY_GUILD_TRUST


def _write_guild(config_dir: Path, guild_id: str, text: str) -> None:
    servers = config_dir / "servers"
    servers.mkdir(parents=True, exist_ok=True)
    (servers / f"{guild_id}.md").write_text(text, encoding="utf-8")


def test_load_guild_pins_from_frontmatter(tmp_path: Path) -> None:
    _write_guild(
        tmp_path,
        "100",
        "---\npinned_tools: [internet_search, render_diagram]\n---\nGuild rules.\n",
    )

    pins = load_guild_pinned_tools("100", config_dir=tmp_path)

    assert pins == frozenset({"internet_search", "render_diagram"})


def test_load_guild_pins_missing_or_invalid_is_empty(tmp_path: Path) -> None:
    assert load_guild_pinned_tools("100", config_dir=tmp_path) == frozenset()
    assert load_guild_pinned_tools("", config_dir=tmp_path) == frozenset()
    assert load_guild_pinned_tools("../100", config_dir=tmp_path) == frozenset()
    _write_guild(tmp_path, "100", "No frontmatter here.\n")
    assert load_guild_pinned_tools("100", config_dir=tmp_path) == frozenset()


def test_load_guild_blocked_tools_from_frontmatter(tmp_path: Path) -> None:
    _write_guild(
        tmp_path,
        "100",
        "---\nblocked_tools: [get_steam_game_info, staff_search]\n---\nGuild rules.\n",
    )

    blocked = load_guild_blocked_tools("100", config_dir=tmp_path)

    assert blocked == frozenset({"get_steam_game_info", "staff_search"})


def test_load_guild_blocked_tools_missing_or_invalid_is_empty(tmp_path: Path) -> None:
    assert load_guild_blocked_tools("100", config_dir=tmp_path) == frozenset()


def test_guild_blocked_tools_retain_invalid_reload_but_missing_or_omitted_clears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_guild(tmp_path, "101", "---\nblocked_tools: [dangerous_tool]\n---\n")
    path = tmp_path / "servers" / "101.md"
    expected = frozenset({"dangerous_tool"})
    assert load_guild_blocked_tools("101", config_dir=tmp_path) == expected

    path.write_text("---\nblocked_tools: not_a_list\n---\n", encoding="utf-8")
    assert load_guild_blocked_tools("101", config_dir=tmp_path) == expected

    original_read_text = Path.read_text

    def unreadable(self: Path, *args: Any, **kwargs: Any) -> str:
        if self == path:
            raise PermissionError("config cannot be read")
        return original_read_text(self, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", unreadable)
        assert load_guild_blocked_tools("101", config_dir=tmp_path) == expected

    path.unlink()
    assert load_guild_blocked_tools("101", config_dir=tmp_path) == frozenset()

    path.write_text("", encoding="utf-8")
    assert load_guild_blocked_tools("101", config_dir=tmp_path) == frozenset()

    _write_guild(tmp_path, "101", "---\nblocked_tools: [dangerous_tool]\n---\n")
    assert load_guild_blocked_tools("101", config_dir=tmp_path) == expected
    path.write_text("body only\n", encoding="utf-8")
    assert load_guild_blocked_tools("101", config_dir=tmp_path) == frozenset()
    assert load_guild_blocked_tools("", config_dir=tmp_path) == frozenset()
    assert load_guild_blocked_tools("../100", config_dir=tmp_path) == frozenset()
    _write_guild(tmp_path, "100", "No frontmatter here.\n")
    assert load_guild_blocked_tools("100", config_dir=tmp_path) == frozenset()


def test_load_guild_thread_handoff_reads_literal_booleans(tmp_path: Path) -> None:
    _write_guild(tmp_path, "100", "---\nthread_handoff: false\n---\nGuild rules.\n")
    assert load_guild_thread_handoff("100", config_dir=tmp_path) is False

    _write_guild(tmp_path, "100", "---\nthread_handoff: true\n---\nGuild rules.\n")
    assert load_guild_thread_handoff("100", config_dir=tmp_path) is True


def test_load_guild_thread_handoff_absent_or_malformed_is_none(tmp_path: Path) -> None:
    assert load_guild_thread_handoff("100", config_dir=tmp_path) is None
    assert load_guild_thread_handoff("", config_dir=tmp_path) is None
    _write_guild(tmp_path, "100", "---\nthread_handoff: 1\n---\nGuild rules.\n")
    assert load_guild_thread_handoff("100", config_dir=tmp_path) is None


def test_load_guild_thread_auto_respond_reads_literal_booleans(tmp_path: Path) -> None:
    _write_guild(tmp_path, "100", "---\nthread_auto_respond: false\n---\nGuild rules.\n")
    assert load_guild_thread_auto_respond("100", config_dir=tmp_path) is False

    _write_guild(tmp_path, "100", "---\nthread_auto_respond: true\n---\nGuild rules.\n")
    assert load_guild_thread_auto_respond("100", config_dir=tmp_path) is True


def test_load_guild_thread_auto_respond_absent_or_malformed_is_none(tmp_path: Path) -> None:
    assert load_guild_thread_auto_respond("100", config_dir=tmp_path) is None
    assert load_guild_thread_auto_respond("", config_dir=tmp_path) is None
    _write_guild(tmp_path, "100", "---\nthread_auto_respond: 1\n---\nGuild rules.\n")
    assert load_guild_thread_auto_respond("100", config_dir=tmp_path) is None
    # The two switches are independent: one set must not imply the other.
    _write_guild(tmp_path, "100", "---\nthread_handoff: false\n---\nGuild rules.\n")
    assert load_guild_thread_auto_respond("100", config_dir=tmp_path) is None


def test_load_guild_thread_targets_parses_channel_ids(tmp_path: Path) -> None:
    _write_guild(
        tmp_path,
        "100",
        "---\nthread_targets: [123456789012345678, 987654321098765432]\n---\nGuild rules.\n",
    )

    targets = load_guild_thread_targets("100", config_dir=tmp_path)

    assert targets == frozenset({"123456789012345678", "987654321098765432"})


def test_load_guild_thread_targets_absent_or_invalid_is_empty(tmp_path: Path) -> None:
    # Empty is the capability being off, so every unreadable shape lands there.
    assert load_guild_thread_targets("100", config_dir=tmp_path) == frozenset()
    assert load_guild_thread_targets("", config_dir=tmp_path) == frozenset()
    _write_guild(tmp_path, "100", "---\nthread_targets: bot-spam\n---\n")
    assert load_guild_thread_targets("100", config_dir=tmp_path) == frozenset()
    # A non-numeric entry is dropped without taking the valid ones with it.
    _write_guild(tmp_path, "100", "---\nthread_targets: [123456789012345678, '#bot-spam']\n---\n")
    assert load_guild_thread_targets("100", config_dir=tmp_path) == frozenset(
        {"123456789012345678"}
    )


def test_load_guild_trust_parses_numeric_id_lists(tmp_path: Path) -> None:
    _write_guild(
        tmp_path,
        "200",
        "---\n"
        "staff_user_ids: [700000000000000101]\n"
        "staff_role_ids: [700000000000000102]\n"
        "regular_role_ids: [123456789012345678]\n"
        "---\n",
    )

    trust = load_guild_trust("200", config_dir=tmp_path)

    assert trust.staff_user_ids == frozenset({"700000000000000101"})
    assert trust.staff_role_ids == frozenset({"700000000000000102"})
    assert trust.regular_role_ids == frozenset({"123456789012345678"})
    assert not trust.is_empty


def test_load_guild_trust_drops_non_numeric_entries(tmp_path: Path) -> None:
    _write_guild(
        tmp_path,
        "200",
        "---\nstaff_user_ids: [123, not-an-id, 456]\nstaff_role_ids: notalist\n---\n",
    )

    trust = load_guild_trust("200", config_dir=tmp_path)

    assert trust.staff_user_ids == frozenset({"123", "456"})
    assert trust.staff_role_ids == frozenset()


def test_load_guild_trust_missing_or_empty_returns_sentinel(tmp_path: Path) -> None:
    assert load_guild_trust("200", config_dir=tmp_path) is EMPTY_GUILD_TRUST
    _write_guild(tmp_path, "200", "---\npinned_tools: [internet_search]\n---\nBody.\n")
    # Frontmatter present but no trust keys -> still the empty sentinel.
    assert load_guild_trust("200", config_dir=tmp_path) is EMPTY_GUILD_TRUST


def test_read_guild_frontmatter_returns_meta_and_source(tmp_path: Path) -> None:
    _write_guild(tmp_path, "400", "---\ncustom_key: [a, b]\n---\nBody.\n")

    result = read_guild_frontmatter("400", config_dir=tmp_path)

    assert result is not None
    meta, source = result
    assert meta == {"custom_key": ["a", "b"]}
    assert source.endswith("400.md")


def test_read_guild_frontmatter_missing_or_invalid_is_none(tmp_path: Path) -> None:
    assert read_guild_frontmatter("400", config_dir=tmp_path) is None
    assert read_guild_frontmatter("", config_dir=tmp_path) is None
    assert read_guild_frontmatter("../400", config_dir=tmp_path) is None


def test_read_guild_frontmatter_invalid_utf8_is_none(tmp_path: Path) -> None:
    servers = tmp_path / "servers"
    servers.mkdir()
    (servers / "400.md").write_bytes(b"---\nname: \xff\n---\n")

    assert read_guild_frontmatter("400", config_dir=tmp_path) is None


def test_guild_frontmatter_is_stripped_from_server_instructions(tmp_path: Path) -> None:
    _write_guild(
        tmp_path,
        "300",
        "---\nstaff_role_ids: [700000000000000102]\n---\nBe helpful to VR folks.\n",
    )

    rendered = load_fragment(tmp_path / "servers", "300", header="Server Instructions")

    assert "staff_role_ids" not in rendered
    assert "Be helpful to VR folks." in rendered


def test_frontmatter_only_guild_fragment_yields_no_instructions(tmp_path: Path) -> None:
    _write_guild(tmp_path, "300", "---\nstaff_role_ids: [700000000000000102]\n---\n")

    rendered = load_fragment(tmp_path / "servers", "300", header="Server Instructions")

    assert rendered == ""
    assert load_guild_trust("300", config_dir=tmp_path).staff_role_ids == frozenset(
        {"700000000000000102"}
    )
