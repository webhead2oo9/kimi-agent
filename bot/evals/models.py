from __future__ import annotations

import os
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from collections.abc import Callable

import yaml  # type: ignore[import-untyped]
from dotenv import dotenv_values

from config.model_config import ModelPricing
from providers import LLMProvider, ProviderConfig, create_provider

_REPO_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


@dataclass(frozen=True)
class ModelSpec:
    label: str
    provider_name: str
    model: str
    base_url: str = ""
    api_key_env: str = ""
    # Optional effort override so an eval arm can mirror a production provider
    # profile (e.g. Sol chats on codex `reasoning_effort: low`). Empty keeps the
    # ProviderConfig default.
    reasoning_effort: str = ""
    # Optional UUID-valued request header generated afresh for every provider
    # call (for gateways such as RunInfra that expose client request tracing).
    request_id_header: str = ""
    # Optional provider-enforced output ceiling. The harness default may be
    # larger than a hosted deployment accepts; clamp before spending an entire
    # run on deterministic HTTP 400s while retaining the requested value in the
    # run summary for comparison transparency.
    max_output_tokens: int | None = None
    # Optional total request timeout for network providers. This is deliberately
    # model-local: experimental gateways should not inherit a production-sized
    # timeout and hold an entire gauntlet open for one stalled request.
    timeout_seconds: float | None = None
    # Minimum start-to-start spacing between calls made through this eval arm.
    # Waiting contributes to scenario wall time but not provider latency.
    min_request_interval_seconds: float = 0.0
    # Per-model capabilities, mirroring config/models.yaml. A provider class
    # advertises what its *transport* can carry, so openai_compat claims image
    # input for every model behind it: true of MiniMax, a 400 from DeepSeek
    # Flash. Empty means "trust the provider", which stays right for the
    # single-model transports (codex).
    capabilities: tuple[str, ...] = ()
    # USD per 1M tokens, same shape and semantics as config/models.yaml. Absent
    # means "not priced" and every cost in the report reads null rather than 0,
    # because a subscription-covered arm genuinely has no per-token price, and
    # inventing one would make a cost comparison confidently wrong.
    pricing: ModelPricing | None = None

    def supports_images(self) -> bool:
        """Whether this arm may be sent image scenarios. Fail-closed.

        Deliberately does not consult the provider object: every provider class
        advertises IMAGE_INPUT because the *transport* carries images, which says
        nothing about the model behind it. An undeclared spec is therefore
        unknown, and unknown is treated as "no". A skipped scenario is
        recoverable and logged, while a mid-run 400 (or a head-to-head where one
        arm could see the picture) silently corrupts the result.
        """
        return "image_input" in self.capabilities

    def effective_max_tokens(self, requested: int) -> int:
        """Apply this deployment's output ceiling to a requested call budget."""
        if self.max_output_tokens is None:
            return requested
        return min(requested, self.max_output_tokens)


@dataclass(frozen=True, kw_only=True)
class ModelsConfig:
    baseline: ModelSpec
    candidates: dict[str, ModelSpec]
    judge: ModelSpec
    # Optional shared vision arm. When present, all compared chat models receive
    # the identical caption.
    image_captioner: ModelSpec | None = None


def _spec_from(label: str, data: dict[str, Any]) -> ModelSpec:
    raw_max_output_tokens = data.get("max_output_tokens")
    spec = ModelSpec(
        label=str(data.get("label", label)),
        provider_name=str(data["provider_name"]),
        model=str(data["model"]),
        base_url=str(data.get("base_url", "")),
        api_key_env=str(data.get("api_key_env", "")),
        reasoning_effort=str(data.get("reasoning_effort", "")),
        request_id_header=str(data.get("request_id_header", "")),
        max_output_tokens=(
            int(raw_max_output_tokens) if raw_max_output_tokens is not None else None
        ),
        timeout_seconds=(
            float(data["timeout_seconds"]) if data.get("timeout_seconds") is not None else None
        ),
        min_request_interval_seconds=float(data.get("min_request_interval_seconds", 0.0)),
        capabilities=tuple(str(c) for c in (data.get("capabilities") or [])),
        pricing=ModelPricing.model_validate(data["pricing"]) if data.get("pricing") else None,
    )
    # An empty base_url constructs an openai_compat client fine but fails on the
    # first request, which run_conversation swallows into a scrubbed error reply --
    # the run would then burn its full budget against an error-emitting model.
    # Reject the unfilled template before any tokens are spent.
    if spec.provider_name == "openai_compat" and not spec.base_url.strip():
        raise ValueError(
            f"Model spec {spec.label!r} uses openai_compat with an empty base_url; "
            "fill base_url in models.yaml before running."
        )
    if spec.max_output_tokens is not None and spec.max_output_tokens < 1:
        raise ValueError(f"Model spec {spec.label!r} max_output_tokens must be >= 1 when set.")
    if spec.timeout_seconds is not None and (
        not math.isfinite(spec.timeout_seconds) or spec.timeout_seconds <= 0
    ):
        raise ValueError(f"Model spec {spec.label!r} timeout_seconds must be > 0 when set.")
    if (
        not math.isfinite(spec.min_request_interval_seconds)
        or spec.min_request_interval_seconds < 0
    ):
        raise ValueError(f"Model spec {spec.label!r} min_request_interval_seconds must be >= 0.")
    return spec


def load_models(path: str | Path) -> ModelsConfig:
    raw = yaml.safe_load(Path(path).read_text()) or {}
    baseline = _spec_from("baseline", raw["baseline"])
    judge = _spec_from("judge", raw["judge"])
    candidates = {
        name: _spec_from(name, data) for name, data in (raw.get("candidates") or {}).items()
    }
    captioner_raw = raw.get("image_captioner")
    image_captioner = (
        _spec_from("image-captioner", captioner_raw) if captioner_raw is not None else None
    )
    if image_captioner is not None and not image_captioner.supports_images():
        raise ValueError("image_captioner must declare the image_input capability")
    return ModelsConfig(
        baseline=baseline,
        candidates=candidates,
        judge=judge,
        image_captioner=image_captioner,
    )


def eval_provider_config(spec: ModelSpec, *, api_key: str) -> ProviderConfig:
    """Build a ProviderConfig directly from an evaluation model specification."""
    if spec.provider_name == "codex":
        kwargs: dict[str, Any] = {}
        if spec.reasoning_effort:
            kwargs["codex_reasoning_effort"] = spec.reasoning_effort
        return ProviderConfig(
            provider_name=spec.provider_name,
            api_key=api_key,
            base_url=spec.base_url,
            model=spec.model,
            openai_timeout_seconds=spec.timeout_seconds or 900.0,
            **kwargs,
        )
    effort_kwargs: dict[str, Any] = {}
    if spec.reasoning_effort:
        if spec.provider_name == "anthropic_compat":
            effort_kwargs["anthropic_effort"] = spec.reasoning_effort
        elif spec.provider_name == "openai_compat":
            effort_kwargs["openai_reasoning_effort"] = spec.reasoning_effort
    if spec.provider_name == "openai_compat" and spec.request_id_header:
        effort_kwargs["openai_request_id_header"] = spec.request_id_header
    return ProviderConfig(
        provider_name=spec.provider_name,
        api_key=api_key,
        base_url=spec.base_url,
        model=spec.model,
        openai_timeout_seconds=spec.timeout_seconds or 900.0,
        **effort_kwargs,
    )


def resolve_api_key(env_name: str, *, env_file: Path = _REPO_ENV_FILE) -> str:
    """Shell environment first, then the repo .env (nothing else loads it here,
    because the bot's Settings reads .env itself but never exports to
    os.environ)."""
    if not env_name:
        return ""
    value = os.environ.get(env_name, "")
    if value:
        return value
    try:
        return str(dotenv_values(env_file).get(env_name) or "")
    except OSError:
        return ""


def build_eval_provider(
    spec: ModelSpec,
    *,
    _create: Callable[[ProviderConfig], LLMProvider] = create_provider,
) -> LLMProvider:
    api_key = resolve_api_key(spec.api_key_env)
    provider = _create(eval_provider_config(spec, api_key=api_key))
    effective = getattr(provider, "model", "")
    if effective and effective != spec.model:
        raise ValueError(
            f"Provider for {spec.label!r} reports effective model {effective!r} "
            f"but the spec requested {spec.model!r}; refusing to run an invalid eval."
        )
    return provider
