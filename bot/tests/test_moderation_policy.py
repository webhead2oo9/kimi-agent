from __future__ import annotations

from moderation.policy import ModerationPolicy
from moderation.types import Direction, ModerationVerdict


def _verdict(**categories: bool) -> ModerationVerdict:
    scores = dict.fromkeys(categories, 0.0)
    return ModerationVerdict(categories=categories, category_scores=scores)


def test_policy_allows_self_harm_disclosures_on_input_but_blocks_instructions_on_output() -> None:
    policy = ModerationPolicy()

    disclosure = _verdict(**{"self-harm": True, "self-harm/intent": True})
    instructions = _verdict(**{"self-harm/instructions": True})

    assert not policy.decide(disclosure, Direction.INPUT).blocked
    assert not policy.decide(instructions, Direction.INPUT).blocked
    assert policy.decide(instructions, Direction.OUTPUT).blocked


def test_policy_uses_direction_specific_table() -> None:
    policy = ModerationPolicy()
    verdict = _verdict(hate=True, harassment=True, illicit=True, violence=True)

    assert not policy.decide(verdict, Direction.INPUT).blocked
    decision = policy.decide(verdict, Direction.OUTPUT)
    assert decision.blocked
    assert decision.matched_categories == ["hate", "harassment", "illicit", "violence"]


def test_policy_score_threshold_override_replaces_category_boolean() -> None:
    policy = ModerationPolicy(score_thresholds={"sexual": 0.8})
    verdict = ModerationVerdict(
        categories={"sexual": True},
        category_scores={"sexual": 0.2},
    )

    assert not policy.decide(verdict, Direction.INPUT).blocked

    high_score = ModerationVerdict(
        categories={"sexual": False},
        category_scores={"sexual": 0.9},
    )
    assert policy.decide(high_score, Direction.INPUT).blocked
