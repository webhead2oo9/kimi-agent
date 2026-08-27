from __future__ import annotations

from config.settings import Settings


def test_compaction_controls_are_not_runtime_settings() -> None:
    """Compaction is mandatory for every chat provider. Model and window routing
    belong only in config/models.yaml; Settings must expose neither a disable
    switch nor a second routing source.
    """
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert not hasattr(s, "compaction_enabled")
    assert not hasattr(s, "compaction_model_window_tokens")
    assert not hasattr(s, "compaction_provider")
    assert not hasattr(s, "compaction_base_url")
    assert not hasattr(s, "compaction_model")
