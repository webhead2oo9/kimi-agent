"""The operator settings file: the spec, coercion, and layering it over env.

This file is applied to the real Settings object at startup, so the tests that
matter end at "the running configuration says X", not at "the parser returned
X". Missing/empty means inherit; any present invalid overlay must stop startup
before it can silently widen access or partially mutate Settings.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from config.operator_settings import (
    KIND_BOOL,
    KIND_CHOICE,
    KIND_FLOAT,
    KIND_ID_LIST,
    KIND_INT,
    KIND_TEXT,
    OperatorSettingsError,
    SETTINGS_SPEC,
    apply_operator_settings,
    coerce_value,
    load_operator_settings,
    spec_for,
)
from config.settings import Settings


def _settings(**values: Any) -> Settings:
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def _write(config_dir: Path, body: str) -> None:
    (config_dir / "settings.md").write_text(body, encoding="utf-8")


def test_default_bot_name_uses_the_shared_brand() -> None:
    assert _settings().bot_name == "Kimi"


# ── The spec itself ──────────────────────────────────────────────────────────


def test_every_managed_field_exists_on_settings() -> None:
    """A typo in the spec would be a field no overlay file could ever set."""
    unknown = [spec.field for spec in SETTINGS_SPEC if spec.field not in Settings.model_fields]
    assert unknown == []


def test_no_secret_is_managed() -> None:
    """A file the overlay can pin is the wrong place to keep a credential:
    secrets stay in .env, referenced by env-var name."""
    from pydantic import SecretStr

    leaked = [
        spec.field
        for spec in SETTINGS_SPEC
        if Settings.model_fields[spec.field].annotation is SecretStr
    ]
    assert leaked == []


def test_no_path_or_url_is_managed() -> None:
    """Paths decide where the bot reads and writes; URLs decide where its
    traffic goes. Neither belongs in an overlay file."""
    suspicious = [
        spec.field
        for spec in SETTINGS_SPEC
        if spec.field.endswith(("_dir", "_path", "_bin", "_url", "_base"))
    ]
    assert suspicious == []


def test_code_exec_filesystem_settings_are_environment_only() -> None:
    managed = {spec.field for spec in SETTINGS_SPEC}
    assert "code_exec_extra_ro_binds" not in managed
    assert "code_exec_netns_resolv_conf" not in managed


def test_code_loading_settings_are_environment_only() -> None:
    managed = {spec.field for spec in SETTINGS_SPEC}
    assert "plugin_modules" not in managed
    assert "kimi_modules" not in managed


def test_sensitive_observability_content_mode_is_environment_only() -> None:
    managed = {spec.field for spec in SETTINGS_SPEC}
    assert "tool_event_log_enabled" in managed
    assert "tool_event_log_content_mode" not in managed


def test_no_remote_control_surface_is_managed() -> None:
    """The overlay must never be able to configure a remote control surface.

    The file wins over the environment, so a managed ``*_enabled`` switch for a
    network-facing console would let a data file open or close that surface.
    The console this guarded was removed; the invariant outlives it, because
    re-adding one and forgetting this exclusion is the regression."""
    managed = {spec.field for spec in SETTINGS_SPEC}
    assert sorted(field for field in managed if field.startswith("webui_")) == []


def test_field_names_are_unique() -> None:
    fields = [spec.field for spec in SETTINGS_SPEC]
    assert len(fields) == len(set(fields))


def test_every_numeric_setting_declares_a_minimum() -> None:
    """For a field with no pydantic validator behind it, this floor is the only
    thing between an overlay file and a bot booting with a zeroed budget."""
    missing = [
        spec.field
        for spec in SETTINGS_SPEC
        if spec.kind in (KIND_INT, KIND_FLOAT) and spec.minimum is None
    ]
    assert missing == []


def test_numeric_defaults_satisfy_their_declared_minimum() -> None:
    """Every value is coerced against its spec on load, so a floor above the
    shipped default would reject a file that only pins unrelated fields."""
    violations = [
        (spec.field, Settings.model_fields[spec.field].default, spec.minimum)
        for spec in SETTINGS_SPEC
        if spec.minimum is not None
        and isinstance(Settings.model_fields[spec.field].default, (int, float))
        and Settings.model_fields[spec.field].default < spec.minimum
    ]
    assert violations == []


def test_declared_kinds_match_the_settings_field_types() -> None:
    expected_types: dict[str, Any] = {
        KIND_BOOL: bool,
        KIND_INT: int,
        KIND_FLOAT: float,
        KIND_TEXT: str,
        KIND_CHOICE: str,
        KIND_ID_LIST: str,
    }
    mismatched = []
    for spec in SETTINGS_SPEC:
        expected = expected_types[spec.kind]
        if spec.nullable:
            expected = expected | None
        if Settings.model_fields[spec.field].annotation != expected:
            mismatched.append(spec.field)
    assert mismatched == []


def test_choice_specs_declare_choices_including_the_current_default() -> None:
    for spec in SETTINGS_SPEC:
        if spec.kind != KIND_CHOICE:
            continue
        assert spec.choices, f"{spec.field} is a choice with no options"
        assert Settings.model_fields[spec.field].default in spec.choices


def test_choice_vocabularies_match_the_code_that_consumes_them() -> None:
    """A closed list is only safe while it tracks its consumer: a form that
    rejects a value the runtime accepts is worse than a free text box."""
    from providers.types import REASONING_EFFORT_ORDER

    expected = {
        "codex_reasoning_effort": {"", *REASONING_EFFORT_ORDER},
    }
    for field, vocabulary in expected.items():
        spec = spec_for(field)
        assert spec is not None
        assert spec.kind == KIND_CHOICE
        assert set(spec.choices) == vocabulary


# ── Coercion ─────────────────────────────────────────────────────────────────


def test_int_coercion_rejects_bools_and_enforces_minimums() -> None:
    spec = spec_for("react_max_iterations")
    assert spec is not None
    assert coerce_value(spec, 5) == 5
    # bool is an int subclass; true would silently become 1.
    with pytest.raises(ValueError):
        coerce_value(spec, True)
    with pytest.raises(ValueError):
        coerce_value(spec, 0)
    with pytest.raises(ValueError):
        coerce_value(spec, "12")


def test_id_list_accepts_yaml_lists_and_comma_strings() -> None:
    spec = spec_for("staff_user_ids")
    assert spec is not None
    assert coerce_value(spec, [111, 222]) == "111,222"
    assert coerce_value(spec, "111, 222") == "111,222"
    assert coerce_value(spec, []) == ""
    with pytest.raises(ValueError):
        coerce_value(spec, ["not-an-id"])


def test_choice_coercion_rejects_values_outside_the_list() -> None:
    spec = spec_for("image_detail")
    assert spec is not None
    assert coerce_value(spec, "high") == "high"
    with pytest.raises(ValueError):
        coerce_value(spec, "ultra")


@pytest.mark.parametrize("value", (float("nan"), float("inf"), float("-inf")))
def test_float_coercion_rejects_non_finite_values(value: float) -> None:
    spec = spec_for("react_temperature")
    assert spec is not None
    assert spec.kind == KIND_FLOAT
    with pytest.raises(ValueError, match="finite"):
        coerce_value(spec, value)


@pytest.mark.parametrize(
    ("field", "yaml_value"),
    (
        ("react_temperature", ".nan"),
        ("react_temperature", ".inf"),
        ("react_temperature", "-.inf"),
    ),
)
def test_invalid_manual_overlay_is_rejected_without_partial_application(
    field: str,
    yaml_value: str,
    tmp_path: Path,
) -> None:
    settings = _settings(react_max_iterations=12)
    document = f"---\nreact_max_iterations: 7\n{field}: {yaml_value}\n---\n"
    _write(tmp_path, document)

    with pytest.raises(OperatorSettingsError):
        apply_operator_settings(settings, config_dir=tmp_path)

    assert settings.react_max_iterations == 12
    assert (tmp_path / "settings.md").read_text(encoding="utf-8") == document


# ── Loading ──────────────────────────────────────────────────────────────────


def test_absent_file_loads_nothing(tmp_path: Path) -> None:
    assert load_operator_settings(config_dir=tmp_path) == {}


def test_empty_file_loads_nothing(tmp_path: Path) -> None:
    _write(tmp_path, " \n\t")

    assert load_operator_settings(config_dir=tmp_path) == {}


def test_unknown_setting_rejects_the_entire_present_file(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "---\nnot_a_setting: 1\nmemory_max_writes_per_turn: 4\n---\n",
    )

    with pytest.raises(OperatorSettingsError):
        load_operator_settings(config_dir=tmp_path)


def test_malformed_file_fails_startup_loudly(tmp_path: Path) -> None:
    _write(tmp_path, "---\nthis: [is: not: yaml\n---\n")

    with pytest.raises(OperatorSettingsError):
        load_operator_settings(config_dir=tmp_path)


def test_invalid_value_rejects_the_entire_present_file(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "---\nreact_max_iterations: nonsense\nmemory_max_writes_per_turn: 4\n---\n",
    )

    with pytest.raises(OperatorSettingsError):
        load_operator_settings(config_dir=tmp_path)


def test_closing_delimiter_at_eof_is_accepted(tmp_path: Path) -> None:
    _write(tmp_path, "---\nmemory_max_writes_per_turn: 4\n---")

    assert load_operator_settings(config_dir=tmp_path) == {"memory_max_writes_per_turn": 4}


def test_decode_error_fails_startup_loudly(tmp_path: Path) -> None:
    (tmp_path / "settings.md").write_bytes(b"\xff\xfe")

    with pytest.raises(OperatorSettingsError):
        load_operator_settings(config_dir=tmp_path)


def test_read_error_fails_startup_loudly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(tmp_path, "")

    def deny_read(_path: Path, **_kwargs: Any) -> str:
        raise OSError("denied")

    monkeypatch.setattr(Path, "read_text", deny_read)
    with pytest.raises(OperatorSettingsError):
        load_operator_settings(config_dir=tmp_path)


# ── Applying ─────────────────────────────────────────────────────────────────


def test_apply_layers_the_file_over_the_environment(tmp_path: Path) -> None:
    """The file is the operator's deliberate override, so environment precedence
    would make editing it silently do nothing."""
    settings = _settings(memory_max_writes_per_turn=99, staff_user_ids="1")
    _write(
        tmp_path,
        "---\nmemory_max_writes_per_turn: 3\nstaff_user_ids: [700, 800]\n---\n",
    )

    applied = apply_operator_settings(settings, config_dir=tmp_path)

    assert sorted(applied) == ["memory_max_writes_per_turn", "staff_user_ids"]
    assert settings.memory_max_writes_per_turn == 3
    assert settings.staff_user_ids == "700,800"


def test_apply_leaves_unmentioned_fields_alone(tmp_path: Path) -> None:
    """A key only ever set in .env keeps working exactly as before."""
    settings = _settings(memory_max_writes_per_turn=99, memory_recall_max_tokens=7)
    _write(tmp_path, "---\nmemory_max_writes_per_turn: 3\n---\n")

    apply_operator_settings(settings, config_dir=tmp_path)

    assert settings.memory_max_writes_per_turn == 3
    assert settings.memory_recall_max_tokens == 7


@pytest.mark.parametrize(
    ("field", "yaml_value"),
    (
        # Caught only by Settings itself: a format the spec cannot express.
        ("discord_search_excluded_channels", "not-a-channel-id"),
    ),
)
def test_complete_candidate_is_validated_before_any_field_is_applied(
    field: str,
    yaml_value: str,
    tmp_path: Path,
) -> None:
    settings = _settings(memory_max_writes_per_turn=9)
    _write(
        tmp_path,
        f"---\nmemory_max_writes_per_turn: 3\n{field}: {yaml_value}\n---\n",
    )

    with pytest.raises(OperatorSettingsError):
        apply_operator_settings(settings, config_dir=tmp_path)

    assert settings.memory_max_writes_per_turn == 9
    assert getattr(settings, field) == Settings.model_fields[field].default


def test_apply_reads_back_every_value_kind(tmp_path: Path) -> None:
    """One hand-written file covering each kind, as an operator would write it."""
    _write(
        tmp_path,
        "---\n"
        'staff_user_ids: ["700", "800"]\n'
        "memory_max_writes_per_turn: 3\n"
        "privacy_consent_enabled: true\n"
        "image_detail: high\n"
        "bot_name: Kimi\n"
        "---\n",
    )

    settings = _settings()
    apply_operator_settings(settings, config_dir=tmp_path)

    assert settings.staff_user_ids == "700,800"
    assert settings.memory_max_writes_per_turn == 3
    assert settings.privacy_consent_enabled is True
    assert settings.image_detail == "high"
    assert settings.bot_name == "Kimi"
