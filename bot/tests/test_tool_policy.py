from __future__ import annotations

from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any

import pytest

from config.fragments.channel_pins import load_channel_blocked_tools
from config.fragments.guild_config import load_guild_blocked_tools
from config.fragments.tool_policy import load_blocked_tools, load_global_blocked_tools


@pytest.mark.parametrize("scope", ("global", "guild", "channel"))
def test_blocked_tool_policy_survives_more_than_64_distinct_scopes(
    tmp_path: Path,
    scope: str,
) -> None:
    expected = frozenset({"dangerous_tool"})
    oldest: tuple[Path, Callable[[], frozenset[str]]] | None = None

    for index in range(65):
        identifier = str(1000 + index)
        if scope == "global":
            config_dir = tmp_path / f"global-{identifier}"
            path = config_dir / "tools.md"
            loader = partial(load_global_blocked_tools, config_dir=config_dir)
        elif scope == "guild":
            path = tmp_path / "servers" / f"{identifier}.md"
            loader = partial(load_guild_blocked_tools, identifier, config_dir=tmp_path)
        else:
            path = tmp_path / "channels" / f"{identifier}.md"
            loader = partial(load_channel_blocked_tools, identifier, config_dir=tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("---\nblocked_tools: [dangerous_tool]\n---\n", encoding="utf-8")
        assert loader() == expected
        if oldest is None:
            oldest = (path, loader)

    assert oldest is not None
    oldest_path, load_oldest = oldest
    oldest_path.unlink()
    assert load_oldest() == expected


def test_global_blocked_tools_retain_last_good_across_reload_failures_then_clear(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "tools.md"
    path.write_text("---\nblocked_tools: [dangerous_tool]\n---\n", encoding="utf-8")
    expected = frozenset({"dangerous_tool"})
    assert load_global_blocked_tools(config_dir=tmp_path) == expected

    path.write_text("---\nblocked_tools: not_a_list\n---\n", encoding="utf-8")
    assert load_global_blocked_tools(config_dir=tmp_path) == expected

    path.unlink()
    assert load_global_blocked_tools(config_dir=tmp_path) == expected

    path.write_text("", encoding="utf-8")
    assert load_global_blocked_tools(config_dir=tmp_path) == expected

    path.write_text("body only\n", encoding="utf-8")
    assert load_global_blocked_tools(config_dir=tmp_path) == expected

    path.write_text("---\nblocked_tools: []\n---\n", encoding="utf-8")
    original_read_text = Path.read_text

    def unreadable(self: Path, *args: Any, **kwargs: Any) -> str:
        if self == path:
            raise PermissionError("config cannot be read")
        return original_read_text(self, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", unreadable)
        assert load_global_blocked_tools(config_dir=tmp_path) == expected

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
    assert merged() == expected

    for path in (global_path, guild_path, channel_path):
        path.write_text("---\nblocked_tools: []\n---\n", encoding="utf-8")
    assert merged() == frozenset()
