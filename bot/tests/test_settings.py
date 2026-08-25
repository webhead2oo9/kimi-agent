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
    assert settings.code_exec_network_mode == "none"


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


FIRST_GUILD_CHANNELS = (
    "800000000000000001:general,"
    "800000000000000002:xr-talk,"
    "800000000000000003:rumormill,"
    "800000000000000004:offtopic,"
    "800000000000000005:community-support,"
    "800000000000000006:ai-discussion,"
    "800000000000000007:games,"
    "800000000000000008:vehicles-and-racing,"
    "800000000000000009:development"
)
SECOND_GUILD_CHANNELS = (
    "700000000000000001:general-support,"
    "700000000000000002:general-support-forum,"
    "700000000000000003:headset-support,"
    "700000000000000004:pc-support,"
    "700000000000000005:standalone-support,"
    "700000000000000006:mobile-support,"
    "700000000000000007:handheld-support,"
    "700000000000000008:streaming-support,"
    "700000000000000009:cloud-support,"
    "700000000000000010:network,"
    "700000000000000011:mac-os,"
    "700000000000000012:video-player,"
    "700000000000000013:feature-requests,"
    "700000000000000014:random"
)


def test_discord_search_channels_parse_ids_and_names() -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        discord_search_channels=(
            "800000000000000101:dev-testing,"
            "800000000000000102:bot-testing,"
            "800000000000000103:random"
        ),
    )

    assert settings.discord_search_channel_map == {
        "800000000000000101": "dev-testing",
        "800000000000000102": "bot-testing",
        "800000000000000103": "random",
    }


def test_discord_search_channels_allows_large_allowlist() -> None:
    # Two full communities' channel catalogs at once, the realistic worst case.
    configured = f"{FIRST_GUILD_CHANNELS},{SECOND_GUILD_CHANNELS}"

    settings = Settings(_env_file=None, discord_search_channels=configured)  # type: ignore[call-arg]

    assert len(settings.discord_search_channel_map) == 23
    assert settings.discord_search_channel_map["800000000000000001"] == "general"
    assert settings.discord_search_channel_map["700000000000000002"] == ("general-support-forum")
    assert settings.discord_search_channel_map["700000000000000014"] == "random"


def test_discord_search_channels_rejects_missing_name() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, discord_search_channels="123")  # type: ignore[call-arg]


def test_discord_search_channels_rejects_non_numeric_id() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, discord_search_channels="abc:general")  # type: ignore[call-arg]


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
