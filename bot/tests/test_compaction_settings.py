from __future__ import annotations

from config.settings import Settings


def test_removed_compaction_settings_stay_removed() -> None:
    """Compaction is mandatory for every chat provider, and its model/window
    knobs moved to config/models.yaml. Re-adding any of these as a Settings
    field would silently resurrect a disable switch or a second routing source."""
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert not hasattr(s, "compaction_enabled")
    assert not hasattr(s, "compaction_model_window_tokens")
    assert not hasattr(s, "compaction_provider")
    assert not hasattr(s, "compaction_base_url")
    assert not hasattr(s, "compaction_model")
