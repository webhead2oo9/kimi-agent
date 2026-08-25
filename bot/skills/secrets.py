from __future__ import annotations

import logging
from pathlib import Path

import yaml  # type: ignore[import-untyped]

log = logging.getLogger(__name__)


def load_secrets(path: Path) -> dict[str, str]:
    if not path.exists():
        log.warning("Secrets file not found: %s", path)
        return {}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {}
        return {str(k): str(v) for k, v in raw.items()}
    except Exception:
        log.exception("Failed to load secrets from %s", path)
        return {}


def resolve_secrets(required: list[str], all_secrets: dict[str, str]) -> dict[str, str]:
    resolved: dict[str, str] = {}
    for key in required:
        if key in all_secrets:
            resolved[key] = all_secrets[key]
        else:
            log.warning("Required secret %r not found in secrets store", key)
    return resolved


def scrub_output(text: str, secrets: dict[str, str]) -> str:
    if not text or not secrets:
        return text
    secret_values = sorted({value for value in secrets.values() if value}, key=len, reverse=True)
    for value in secret_values:
        if value in text:
            text = text.replace(value, "[REDACTED]")
    return text
