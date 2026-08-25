from __future__ import annotations

from dataclasses import dataclass, field

from moderation.types import Direction, ModerationDecision, ModerationVerdict


# (blocks_input, blocks_output) per moderation category. Input is what a member
# sent; output is what the bot is about to say. The asymmetry is deliberate.
# Output-only entries let a member raise a topic that the bot still must not
# produce. The self-harm pair blocks neither direction, so a member in crisis
# reaches the bot and gets a reply instead of a refusal; only
# "self-harm/instructions" is withheld, so methods are never supplied. These are
# policy decisions rather than thresholds: flipping a False to True converts a
# supported conversation into a refusal.
_CATEGORY_POLICY: dict[str, tuple[bool, bool]] = {
    "sexual/minors": (True, True),
    "sexual": (True, True),
    "violence/graphic": (True, True),
    "hate/threatening": (True, True),
    "harassment/threatening": (True, True),
    "illicit/violent": (True, True),
    "self-harm/instructions": (False, True),
    "self-harm": (False, False),
    "self-harm/intent": (False, False),
    "hate": (False, True),
    "harassment": (False, True),
    "illicit": (False, True),
    "violence": (False, True),
}


@dataclass(frozen=True)
class ModerationPolicy:
    score_thresholds: dict[str, float] = field(default_factory=dict)

    def decide(
        self,
        verdict: ModerationVerdict,
        direction: Direction,
    ) -> ModerationDecision:
        matched: list[str] = []
        direction_index = 0 if direction is Direction.INPUT else 1
        for category, blocks in _CATEGORY_POLICY.items():
            if not blocks[direction_index]:
                continue
            if self._category_applies(verdict, category):
                matched.append(category)
        return ModerationDecision(
            blocked=bool(matched),
            matched_categories=matched,
            category_scores=dict(verdict.category_scores),
        )

    def _category_applies(self, verdict: ModerationVerdict, category: str) -> bool:
        if category in self.score_thresholds:
            score = verdict.category_scores.get(category, 0.0)
            return score >= self.score_thresholds[category]
        return bool(verdict.categories.get(category, False))
