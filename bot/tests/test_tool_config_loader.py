"""The per-turn tool-config loader: fail-open reads, per-key leniency, caching.

The contract this file pins down is the *direction* of failure. The denylist
loader (``config/fragments/tool_policy.py``) raises rather than risk silently granting a
tool; this one returns the tool's own defaults rather than risk taking a working
tool down over a hand-edited file. Nothing here may raise.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from config.fragments.tool_config import (
    load_tool_config,
    load_tool_configs,
    tool_config_path,
)
from tools.config_spec import KIND_CHOICE, KIND_INT, KIND_TEXT, ToolConfigField

MODE = ToolConfigField(
    field="mode",
    label="Mode",
    kind=KIND_CHOICE,
    default="failover",
    choices=("failover", "blend"),
)
NOTICE = ToolConfigField(
    field="result_notice",
    label="Result notice",
    kind=KIND_TEXT,
    default="",
)
LIMIT = ToolConfigField(
    field="max_results", label="Max results", kind=KIND_INT, default=5, minimum=1
)
SPEC = (MODE, NOTICE, LIMIT)

DEFAULTS = {
    "mode": "failover",
    "result_notice": "",
    "max_results": 5,
}


def _write(config_dir: Path, name: str, text: str) -> Path:
    path = tool_config_path(name, config_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_absent_fragment_resolves_to_the_declared_defaults(tmp_path: Path) -> None:
    assert load_tool_config("demo_tool", SPEC, config_dir=tmp_path) == DEFAULTS


def test_overrides_layer_over_defaults(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "demo_tool",
        '---\nmode: blend\nresult_notice: "source: wiki"\n---\n',
    )

    assert load_tool_config("demo_tool", SPEC, config_dir=tmp_path) == {
        "mode": "blend",
        "result_notice": "source: wiki",
        "max_results": 5,
    }


def test_deleting_the_fragment_reverts_the_tool(tmp_path: Path) -> None:
    """An absent file is an explicit "defaults", so it also clears the cache."""
    path = _write(tmp_path, "demo_tool", "---\nmode: blend\n---\n")
    assert load_tool_config("demo_tool", SPEC, config_dir=tmp_path)["mode"] == "blend"

    path.unlink()

    assert load_tool_config("demo_tool", SPEC, config_dir=tmp_path) == DEFAULTS


def test_a_body_only_or_empty_fragment_is_a_valid_no_override_document(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "demo_tool", "An operator note, no frontmatter.\n")
    assert load_tool_config("demo_tool", SPEC, config_dir=tmp_path) == DEFAULTS

    _write(tmp_path, "other_tool", "")
    assert load_tool_config("other_tool", SPEC, config_dir=tmp_path) == DEFAULTS


def test_one_bad_value_costs_only_that_field(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _write(
        tmp_path,
        "demo_tool",
        '---\nmode: nonsense\nresult_notice: "hi"\nmax_results: 3\n---\n',
    )

    resolved = load_tool_config("demo_tool", SPEC, config_dir=tmp_path)

    assert resolved == {
        "mode": "failover",
        "result_notice": "hi",
        "max_results": 3,
    }
    assert "invalid value for 'mode'" in caplog.text


def test_an_unknown_key_is_warned_about_and_ignored(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _write(tmp_path, "demo_tool", "---\nmodee: blend\n---\n")

    assert load_tool_config("demo_tool", SPEC, config_dir=tmp_path) == DEFAULTS
    assert "unknown config key 'modee'" in caplog.text


def test_a_value_below_the_declared_minimum_falls_back(tmp_path: Path) -> None:
    _write(tmp_path, "demo_tool", "---\nmax_results: 0\n---\n")

    assert load_tool_config("demo_tool", SPEC, config_dir=tmp_path)["max_results"] == 5


def test_a_corrupt_fragment_retains_the_last_known_good_values(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = _write(tmp_path, "demo_tool", "---\nmode: blend\n---\n")
    assert load_tool_config("demo_tool", SPEC, config_dir=tmp_path)["mode"] == "blend"

    path.write_text("---\nmode: [unclosed\n", encoding="utf-8")

    assert load_tool_config("demo_tool", SPEC, config_dir=tmp_path)["mode"] == "blend"
    assert "retaining the last-known-good values" in caplog.text


def test_a_corrupt_fragment_with_no_cache_falls_back_to_defaults(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _write(tmp_path, "demo_tool", "---\nmode: blend\n")

    assert load_tool_config("demo_tool", SPEC, config_dir=tmp_path) == DEFAULTS
    assert "falls back to its defaults" in caplog.text


def test_frontmatter_that_is_not_a_mapping_is_a_load_failure_not_an_override(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "demo_tool", "---\n- blend\n---\n")

    assert load_tool_config("demo_tool", SPEC, config_dir=tmp_path) == DEFAULTS


def test_an_unreadable_fragment_retains_the_last_known_good_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write(tmp_path, "demo_tool", "---\nmode: blend\n---\n")
    assert load_tool_config("demo_tool", SPEC, config_dir=tmp_path)["mode"] == "blend"
    original_read_text = Path.read_text

    def unreadable(self: Path, *args: Any, **kwargs: Any) -> str:
        if self == path:
            raise PermissionError("config cannot be read")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable)

    assert load_tool_config("demo_tool", SPEC, config_dir=tmp_path)["mode"] == "blend"


def test_retained_values_are_re_resolved_against_the_current_spec(
    tmp_path: Path,
) -> None:
    """A cached override is re-coerced on every load, not trusted as-is."""
    path = _write(tmp_path, "demo_tool", "---\nmode: blend\n---\n")
    assert load_tool_config("demo_tool", SPEC, config_dir=tmp_path)["mode"] == "blend"
    path.write_text("---\nmode: [unclosed\n", encoding="utf-8")

    narrowed = (
        ToolConfigField(
            field="mode",
            label="Mode",
            kind=KIND_CHOICE,
            default="failover",
            choices=("failover",),
        ),
        NOTICE,
        LIMIT,
    )

    resolved = load_tool_config("demo_tool", narrowed, config_dir=tmp_path)

    assert resolved["mode"] == "failover"


def test_the_cache_is_keyed_by_path_so_two_config_dirs_do_not_bleed(
    tmp_path: Path,
) -> None:
    good = tmp_path / "good"
    broken = tmp_path / "broken"
    _write(good, "demo_tool", "---\nmode: blend\n---\n")
    _write(broken, "demo_tool", "---\nmode: [unclosed\n")

    assert load_tool_config("demo_tool", SPEC, config_dir=good)["mode"] == "blend"
    assert load_tool_config("demo_tool", SPEC, config_dir=broken)["mode"] == "failover"


def test_the_last_known_good_cache_is_bounded(tmp_path: Path) -> None:
    from config.fragments import tool_config as loader
    from config.fragments._fragment_cache import DEFAULT_MAX_ENTRIES

    for index in range(DEFAULT_MAX_ENTRIES + 8):
        directory = tmp_path / f"dir{index}"
        _write(directory, "demo_tool", "---\nmode: blend\n---\n")
        load_tool_config("demo_tool", SPEC, config_dir=directory)

    assert len(loader._cache._entries) <= DEFAULT_MAX_ENTRIES


def test_an_implausible_tool_name_never_builds_a_path(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    assert load_tool_config("../../etc/passwd", SPEC, config_dir=tmp_path) == DEFAULTS
    assert "implausible tool name" in caplog.text


def test_load_tool_configs_resolves_every_spec_in_one_snapshot(tmp_path: Path) -> None:
    _write(tmp_path, "demo_tool", "---\nmode: blend\n---\n")

    resolved = load_tool_configs({"demo_tool": SPEC, "other_tool": (LIMIT,)}, config_dir=tmp_path)

    assert resolved["demo_tool"]["mode"] == "blend"
    assert resolved["other_tool"] == {"max_results": 5}


def test_load_tool_configs_with_no_specs_is_empty(tmp_path: Path) -> None:
    assert load_tool_configs({}, config_dir=tmp_path) == {}
