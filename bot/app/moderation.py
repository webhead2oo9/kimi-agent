from __future__ import annotations

import logging

from config.settings import Settings
from moderation.backends.openai_omni import OpenAIOmniModerationBackend
from moderation.service import ModerationService
from trust.tiers import trust_tier_from_value

log = logging.getLogger(__name__)


def build_moderation_service(settings: Settings) -> ModerationService | None:
    if not settings.moderation_enabled:
        log.info("Content moderation disabled; MODERATION_ENABLED is false")
        return None
    api_key = settings.moderation_api_key.get_secret_value()
    if not api_key:
        log.warning("Content moderation disabled; MODERATION_API_KEY is not set")
        return None
    return ModerationService(
        backend=OpenAIOmniModerationBackend(
            api_key=api_key,
            base_url=settings.moderation_base_url,
            model=settings.moderation_model,
        ),
        enabled=True,
        timeout_seconds=settings.moderation_timeout_seconds,
        input_images=settings.moderation_input_images,
        output_images=settings.moderation_output_images,
        input_refusal=settings.moderation_input_refusal,
        output_refusal=settings.moderation_output_refusal,
        error_refusal=settings.moderation_error_refusal,
        output_exempt_tier=(
            trust_tier_from_value(
                settings.moderation_output_exempt_tier,
                label="moderation output exemption tier",
            )
            if settings.moderation_output_exempt_tier
            else None
        ),
    )
