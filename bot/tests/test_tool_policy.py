from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from config.fragments.channel_pins import load_channel_blocked_tools
from config.fragments._fragment_cache import LastKnownGoodCache
from config.fragments.guild_config import load_guild_blocked_tools
from config.fragments.tool_policy import load_blocked_tools, load_global_blocked_tools


def test_last_known_good_cache_is_bounded(tmp_path: Path) -> None:
    cache = LastKnownGoodCache[int](max_entries=2)
    keys = [cache.key(tmp_path / f"{index}.md") for index in range(3)]
    for index, key in enumerate(keys):
        cache.remember(key, index)

    assert cache.last_good(keys[0]) is None
    assert cache.last_good(keys[1]) == 1
    assert cache.last_good(keys[2]) == 2


def test_global_blocked_tools_retains_invalid_reload_but_missing_or_omitted_clears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "tools.md"
    path.write_text("---\nblocked_tools: [dangerous_tool]\n---\n", encoding="utf-8")
    expected = frozenset({"dangerous_tool"})
    assert load_global_blocked_tools(config_dir=tmp_path) == expected

    path.write_text("---\nblocked_tools: not_a_list\n---\n", encoding="utf-8")
    assert load_global_blocked_tools(config_dir=tmp_path) == expected

    original_read_text = Path.read_text

    def unreadable(self: Path, *args: Any, **kwargs: Any) -> str:
        if self == path:
            raise PermissionError("config cannot be read")
        return original_read_text(self, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", unreadable)
        assert load_global_blocked_tools(config_dir=tmp_path) == expected

    path.unlink()
    assert load_global_blocked_tools(config_dir=tmp_path) == frozenset()

    path.write_text("", encoding="utf-8")
    assert load_global_blocked_tools(config_dir=tmp_path) == frozenset()

    path.write_text("---\nblocked_tools: [dangerous_tool]\n---\n", encoding="utf-8")
    assert load_global_blocked_tools(config_dir=tmp_path) == expected
    path.write_text("body only\n", encoding="utf-8")
    assert load_global_blocked_tools(config_dir=tmp_path) == frozenset()


def test_next_turn_merged_policy_retains_failed_scopes_and_explicit_empty_clears(
    tmp_path: Path,
) -> None:
    global_path = tmp_path / "tools.md"
    guild_path = tmp_path / "servers" / "100.md"
    channel_path = tmp_path / "channels" / "200.md"
    guild_path.parent.mkdir()
    channel_path.parent.mkdir()
    global_path.write_text("---\nblocked_tools: [global_tool]\n---\n", encoding="utf-8")
    guild_path.write_text("---\nblocked_tools: [guild_tool]\n---\n", encoding="utf-8")
    channel_path.write_text("---\nblocked_tools: [channel_tool]\n---\n", encoding="utf-8")

    def merged() -> frozenset[str]:
        return load_blocked_tools(
            "100",
            "200",
            load_global=lambda: load_global_blocked_tools(config_dir=tmp_path),
            load_guild=lambda guild_id: load_guild_blocked_tools(guild_id, config_dir=tmp_path),
            load_channel=lambda channel_id: load_channel_blocked_tools(
                channel_id, config_dir=tmp_path
            ),
        )

    expected = frozenset({"global_tool", "guild_tool", "channel_tool"})
    assert merged() == expected

    global_path.unlink()
    guild_path.write_text("---\nblocked_tools: [invalid\n---\n", encoding="utf-8")
    channel_path.unlink()
    assert merged() == frozenset({"guild_tool"})

    for path in (global_path, guild_path, channel_path):
        path.write_text("---\nblocked_tools: []\n---\n", encoding="utf-8")
    assert merged() == frozenset()
