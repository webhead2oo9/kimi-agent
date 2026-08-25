from __future__ import annotations

from branding import DEFAULT_BOT_NAME, PROVIDER_IDENTITY_MAX_LENGTH, provider_identity


def test_provider_identity_transliterates_unicode_and_drops_header_controls() -> None:
    assert provider_identity("  Kímí 🤖\r\nInjected: value  ") == "Kimi Injected- value"


def test_provider_identity_falls_back_when_no_safe_characters_remain() -> None:
    assert provider_identity("🤖\r\n") == DEFAULT_BOT_NAME


def test_provider_identity_caps_header_length() -> None:
    assert provider_identity("A" * 100) == "A" * PROVIDER_IDENTITY_MAX_LENGTH
