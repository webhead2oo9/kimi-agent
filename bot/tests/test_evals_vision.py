"""Image plumbing for the eval harness: fixtures in, content parts out."""

from __future__ import annotations

from pathlib import Path

import pytest

from evals.harness import FIXTURE_IMAGE_DIR, _reply_context, image_part
from evals.models import ModelSpec, load_models
from evals.scenario import Scenario, TurnSpec, load_scenarios, split_image_scenarios
from providers.types import ContentPartType
from trust.tiers import TrustTier


def test_image_part_matches_the_shape_discord_attachments_take() -> None:
    # Production hands the model base64 data URLs with a sniffed media type
    # (agent/attachments.py). A fixture delivered any other way would exercise a
    # path the bot never takes.
    part = image_part("bands-rgb.png")

    assert part.type == ContentPartType.IMAGE
    assert part.media_type == "image/png"
    assert (part.image_url or "").startswith("data:image/png;base64,")


def test_image_part_rejects_a_path_escaping_the_fixture_dir() -> None:
    with pytest.raises(ValueError, match="escapes"):
        image_part("../../.env")


def test_image_part_reports_a_missing_fixture_by_path() -> None:
    with pytest.raises(FileNotFoundError, match="not found"):
        image_part("no-such-image.png")


def test_reply_context_is_built_only_when_reply_images_exist() -> None:
    assert _reply_context(TurnSpec(text="hi")) is None
    assert _reply_context(TurnSpec(text="hi", images=("bands-rgb.png",))) is None

    reply = _reply_context(
        TurnSpec(text="hi", reply_images=("bands-rgb.png",), reply_author="Ana", reply_text="x")
    )

    assert reply is not None
    assert reply.author_name == "Ana"
    assert len(reply.image_parts) == 1


def test_split_image_scenarios_separates_both_rails_from_plain_ones() -> None:
    def _scenario(scenario_id: str, turn: TurnSpec) -> Scenario:
        return Scenario(
            id=scenario_id, category="vision", trust_tier=TrustTier.MEMBER, turns=[turn]
        )

    plain, visual = split_image_scenarios(
        [
            _scenario("plain", TurnSpec(text="hi")),
            _scenario("attached", TurnSpec(text="hi", images=("bands-rgb.png",))),
            _scenario("replied", TurnSpec(text="hi", reply_images=("bands-rgb.png",))),
        ]
    )

    assert [s.id for s in plain] == ["plain"]
    assert [s.id for s in visual] == ["attached", "replied"]


def test_supports_images_is_fail_closed_on_an_undeclared_spec() -> None:
    # Every provider class advertises IMAGE_INPUT because the transport carries
    # images; consulting it would make the guard vacuous and send pictures to
    # text-only models behind openai_compat.
    undeclared = ModelSpec(label="x", provider_name="openai_compat", model="m")
    text_only = ModelSpec(
        label="x", provider_name="openai_compat", model="m", capabilities=("text", "tool_calling")
    )
    visual = ModelSpec(
        label="x",
        provider_name="openai_compat",
        model="m",
        capabilities=("text", "tool_calling", "image_input"),
    )

    assert undeclared.supports_images() is False
    assert text_only.supports_images() is False
    assert visual.supports_images() is True


def test_shipped_eval_specs_declare_capabilities() -> None:
    # An arm with no declaration silently loses image coverage, which is safe but
    # invisible; the shipped entries should each say what they are.
    models = load_models(Path("evals/models.example.yaml"))

    for spec in (models.baseline, *models.candidates.values()):
        assert spec.capabilities, f"{spec.label} declares no capabilities"


def test_bundled_vision_scenarios_reference_fixtures_that_exist() -> None:
    # A typo'd filename would otherwise surface as a mid-run crash after the
    # earlier scenarios had already been paid for.
    scenarios = load_scenarios(Path("evals/scenarios"))
    referenced = {
        name
        for scenario in scenarios
        for turn in scenario.turns
        for name in (*turn.images, *turn.reply_images)
    }

    assert referenced, "expected at least one scenario to attach an image"
    for name in referenced:
        assert (FIXTURE_IMAGE_DIR / name).is_file(), f"missing fixture {name}"
