from __future__ import annotations

from config.settings import Settings
from tests.helpers import env_example_active, env_example_declarations

# Keys that legitimately appear in `.env.example` without being a `Settings`
# field. Empty today; kept as the seam so a deliberate addition is a one-line
# allowlist entry rather than a reason to delete the guard.
_NON_SETTINGS_KEYS: frozenset[str] = frozenset()


def test_env_example_declares_no_unknown_keys() -> None:
    """Every key in the template maps to a real `Settings` field.

    `test_config_sync.py` guards the forward direction (no field missing from
    the template); this is the reverse, which is what catches a typo or a key
    left behind after its setting was renamed or removed.
    """

    fields = {name.upper() for name in Settings.model_fields}
    unknown = sorted(env_example_declarations().keys() - fields - _NON_SETTINGS_KEYS)
    assert not unknown, (
        "`.env.example` declares keys that are not Settings fields (rename, "
        f"delete, or allowlist each): {unknown}"
    )


def test_env_example_keeps_model_routing_out_of_settings() -> None:
    values = env_example_active()

    moved_keys = {
        "LLM_PROVIDER",
        "LLM_BASE_URL",
        "LLM_MODEL",
        "OPENAI_SERVICE_TIER",
        "OPENAI_STORE_RESPONSES",
        "OPENAI_TIMEOUT_SECONDS",
        "OPENROUTER_PROVIDER_JSON",
        "OPENROUTER_APP_URL",
        "OPENROUTER_APP_NAME",
        "COMPACTION_MODEL_WINDOW_TOKENS",
        "COMPACTION_PROVIDER",
        "COMPACTION_BASE_URL",
        "COMPACTION_MODEL",
        "USER_PERSONA_COMPILER_MODEL",
    }

    assert not (moved_keys & values.keys())
    assert values["MODEL_API_KEY"] == ""
    assert values["OPENCODE_GO_API_KEY"] == ""
    assert values["RUNINFRA_GATEWAY_KEY"] == ""
    assert values["ANTHROPIC_API_KEY"] == ""
    assert values["COMPACTION_API_KEY"] == ""


def test_moderation_env_example_documents_dedicated_openai_key() -> None:
    values = env_example_active()

    assert values["MODERATION_ENABLED"] == "false"
    assert values["MODERATION_API_KEY"] == ""
    assert values["MODERATION_MODEL"] == "omni-moderation-latest"
    assert values["MODERATION_INPUT_IMAGES"] == "true"
    assert values["MODERATION_OUTPUT_IMAGES"] == "true"
    assert values["MODERATION_OUTPUT_EXEMPT_TIER"] == ""
