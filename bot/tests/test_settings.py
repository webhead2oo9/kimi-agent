"""Settings parsing and validation.

Every ``Settings(...)`` here passes ``_env_file=None``. Without it
``pydantic-settings`` reads the developer's real ``.env``, so a test asserting a
default would pass or fail depending on whose machine ran it. The tests that
happen to be safe today are only safe because they assert exclusively on fields
they passed explicitly. Env *variables* still apply, which is what
``monkeypatch.setenv`` below relies on; only the file is cut out.
"""

from typing import Any

import pytest
from pydantic import ValidationError

from config.settings import Settings


def test_code_exec_public_defaults_are_safe() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.code_exec_enabled is False
    assert settings.code_exec_min_tier == "member"
    assert settings.code_exec_network_mode == "none"
    assert settings.code_exec_workspace_quota_poll_seconds == 5.0
    assert settings.code_exec_workspace_quota_scan_retries == 4


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("code_exec_workspace_quota_poll_seconds", 0),
        ("code_exec_workspace_quota_scan_retries", 0),
        ("code_exec_workspace_quota_scan_retries", 11),
    ],
)
def test_code_exec_workspace_quota_monitor_settings_must_be_positive(
    field: str, value: int
) -> None:
    with pytest.raises(ValidationError, match=field.upper()):
        Settings(_env_file=None, **{field: value})  # type: ignore[call-arg, arg-type]


@pytest.mark.parametrize("mode", ["none", "host", "netns"])
def test_code_exec_network_modes_are_normalized(mode: str) -> None:
    kwargs: dict[str, Any] = {"code_exec_network_mode": mode.upper()}
    if mode == "netns":
        kwargs.update(
            code_exec_enabled=True,
            code_exec_netns_helper_bin="/usr/local/sbin/code-exec-netns",
            code_exec_netns_resolv_conf="/etc/netns/code-exec/resolv.conf",
            code_exec_network_probe_blocked_ip="10.0.0.2:80",
        )
    settings = Settings(_env_file=None, **kwargs)  # type: ignore[call-arg]

    assert settings.code_exec_network_mode == mode


def test_enabled_netns_code_exec_requires_complete_fail_closed_probe_config() -> None:
    with pytest.raises(ValidationError, match="CODE_EXEC_NETNS_HELPER_BIN"):
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            code_exec_enabled=True,
            code_exec_network_mode="netns",
        )


def test_invalid_code_exec_network_mode_fails_fast() -> None:
    with pytest.raises(ValidationError, match="none, host, netns"):
        Settings(_env_file=None, code_exec_network_mode="vpn")  # type: ignore[call-arg]


@pytest.mark.parametrize("tier", ["member", "regular", "staff"])
def test_code_exec_min_tier_is_normalized(tier: str) -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        code_exec_min_tier=tier.upper(),
    )

    assert settings.code_exec_min_tier == tier


def test_invalid_code_exec_min_tier_fails_fast() -> None:
    with pytest.raises(ValidationError, match="member, regular, staff"):
        Settings(_env_file=None, code_exec_min_tier="owner")  # type: ignore[call-arg]


def test_code_exec_min_tier_reads_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODE_EXEC_MIN_TIER", "staff")

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.code_exec_min_tier == "staff"


def test_browser_is_off_by_default_and_defaults_to_host_networking() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.browser_enabled is False
    assert settings.browser_network_mode == "host"


def test_enabled_netns_browser_requires_complete_fail_closed_probe_config() -> None:
    with pytest.raises(ValidationError, match="BROWSER_NETNS_HELPER_BIN"):
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            browser_enabled=True,
            browser_network_mode="netns",
        )


def test_browser_network_mode_is_normalized() -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        browser_network_mode="HOST",
    )

    assert settings.browser_network_mode == "host"


def test_video_understanding_is_off_and_secret_is_blank_by_default() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.video_understanding_enabled is False
    assert settings.gemini_api_key.get_secret_value() == ""
    assert settings.video_understanding_max_concurrency == 4


def test_image_generation_is_off_and_oauth_first_by_default() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.image_gen_enabled is False
    assert settings.image_gen_backend == "openai"
    assert settings.image_gen_auth_mode == "auto"
    assert settings.image_gen_api_key.get_secret_value() == ""
    assert settings.image_gen_max_concurrency == 1
    assert settings.image_gen_timeout_seconds == 300.0


@pytest.mark.parametrize("value", [0, 9])
def test_image_generation_concurrency_is_bounded(value: int) -> None:
    with pytest.raises(ValidationError):
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            image_gen_max_concurrency=value,
        )


@pytest.mark.parametrize("value", [29.9, 900.1])
def test_image_generation_timeout_is_bounded(value: float) -> None:
    with pytest.raises(ValidationError):
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            image_gen_timeout_seconds=value,
        )


@pytest.mark.parametrize("value", [0, 33])
def test_video_concurrency_is_bounded(value: int) -> None:
    with pytest.raises(ValidationError):
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            video_understanding_max_concurrency=value,
        )


def test_internet_search_defaults_match_tool_contract() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.internet_search_max_results == 10
    assert settings.internet_search_max_backend_calls_per_turn == 10
    assert settings.internet_search_safesearch == "moderate"
    assert settings.exa_search_cost_usd is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("internet_search_max_results", 51),
        ("internet_search_max_backend_calls_per_turn", 0),
        ("internet_search_safesearch", "maybe"),
        ("exa_search_cost_usd", -0.01),
        ("brave_search_cost_usd", float("nan")),
    ],
)
def test_invalid_internet_search_settings_fail_fast(field: str, value: object) -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        setattr(settings, field, value)


def test_allowed_channels_parses_numeric_ids() -> None:
    settings = Settings(_env_file=None, allowed_channel_ids="123, 456")  # type: ignore[call-arg]
    assert settings.allowed_channels == {123, 456}


def test_allowed_channels_empty_is_empty_set() -> None:
    settings = Settings(_env_file=None, allowed_channel_ids="")  # type: ignore[call-arg]
    assert settings.allowed_channels == set()


def test_non_numeric_allowed_channel_id_fails_fast_at_construction() -> None:
    # A malformed entry must be rejected at startup, not raise lazily inside
    # should_respond on every inbound message.
    with pytest.raises(ValidationError):
        Settings(_env_file=None, allowed_channel_ids="123,abc,456")  # type: ignore[call-arg]


def test_role_id_sets_parse_numeric_ids() -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        staff_role_ids="700000000000000102",
        regular_role_ids="700000000000000103, 700000000000000104",
    )

    assert settings.staff_role_id_set == {"700000000000000102"}
    assert settings.regular_role_id_set == {
        "700000000000000103",
        "700000000000000104",
    }


def test_non_numeric_role_id_fails_fast_at_construction() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, staff_role_ids="700000000000000102,Admin")  # type: ignore[call-arg]


def test_discord_search_exclusions_parse_numeric_ids() -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        discord_search_excluded_channels="800000000000000101, 800000000000000102",
    )

    assert settings.discord_text_search_enabled is True
    assert settings.discord_search_excluded_channel_ids == {
        "800000000000000101",
        "800000000000000102",
    }


def test_discord_search_exclusions_reject_non_numeric_id() -> None:
    with pytest.raises(ValidationError):
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            discord_search_excluded_channels="123,general",
        )


def test_discord_search_exclusions_reject_duplicate_id() -> None:
    with pytest.raises(ValidationError):
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            discord_search_excluded_channels="123,123",
        )


def test_legacy_discord_search_allowlist_fails_with_migration_message() -> None:
    with pytest.raises(ValidationError, match="DISCORD_SEARCH_EXCLUDED_CHANNELS"):
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            discord_search_channels="123:general",
        )


def test_blank_react_temperature_uses_endpoint_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REACT_TEMPERATURE", "")

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.react_temperature is None


def test_react_turn_timeout_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, react_turn_timeout_seconds=0)  # type: ignore[call-arg]


def test_moderation_output_exempt_tier_defaults_to_blank() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.moderation_output_exempt_tier == ""


@pytest.mark.parametrize("tier", ["member", "regular", "staff", " Regular "])
def test_moderation_output_exempt_tier_accepts_known_tiers(tier: str) -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        moderation_output_exempt_tier=tier,
    )

    assert settings.moderation_output_exempt_tier == tier.strip().lower()


def test_moderation_output_exempt_tier_rejects_unknown_tier() -> None:
    with pytest.raises(ValidationError):
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            moderation_output_exempt_tier="wizard",
        )


@pytest.mark.parametrize(
    "field",
    [
        "attachment_max_bytes",
        "attachment_max_total_bytes",
        "attachment_orphan_ttl_seconds",
        "attachment_orphan_sweep_interval_seconds",
        "attachment_orphan_sweep_max_files",
    ],
)
@pytest.mark.parametrize("value", [0, -1])
def test_attachment_lifecycle_values_must_be_positive(field: str, value: int) -> None:
    with pytest.raises(ValidationError, match=field.upper()):
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            **{field: value},  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    "field",
    [
        "workspace_file_ttl",
        "workspace_max_size_mb",
        "workspace_sweep_interval",
    ],
)
@pytest.mark.parametrize("value", [0, -1])
def test_workspace_lifecycle_values_must_be_positive(field: str, value: int) -> None:
    with pytest.raises(ValidationError, match=field.upper()):
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            **{field: value},  # type: ignore[arg-type]
        )


def test_hindsight_url_default_is_empty() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.hindsight_url == ""


def test_no_automatic_channel_context_setting_and_concurrency_default() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert not hasattr(settings, "channel_context_backfill_limit")
    assert settings.llm_max_concurrency == 8
    assert settings.turn_max_concurrency == 16
    assert settings.turn_max_concurrency_per_user == 2


@pytest.mark.parametrize(
    "field",
    ["turn_max_concurrency", "turn_max_concurrency_per_user"],
)
@pytest.mark.parametrize("value", [0, -1])
def test_turn_concurrency_caps_must_be_positive(field: str, value: int) -> None:
    with pytest.raises(ValidationError, match=field.upper()):
        Settings(_env_file=None, **{field: value})  # type: ignore[call-arg, arg-type]


def test_thread_handoff_gate_defaults_on() -> None:
    """The handoff gate's real check is per-guild config, so it defaults enabled."""
    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.thread_handoff_enabled is True
    assert settings.thread_handoff_suggest_after_tool_calls == 5


@pytest.mark.parametrize("value", [-1, -10])
def test_thread_handoff_suggestion_threshold_must_be_non_negative(value: int) -> None:
    with pytest.raises(ValidationError, match="THREAD_HANDOFF_SUGGEST_AFTER_TOOL_CALLS"):
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            thread_handoff_suggest_after_tool_calls=value,
        )


@pytest.mark.parametrize(
    "field",
    [
        "user_persona_max_chars",
        "user_persona_request_max_chars",
        "user_persona_compiler_max_tokens",
    ],
)
def test_user_persona_caps_must_be_positive(field: str) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: 0})  # type: ignore[call-arg, arg-type]
