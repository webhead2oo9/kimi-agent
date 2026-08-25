from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal


class Direction(str, Enum):
    INPUT = "input"
    OUTPUT = "output"


@dataclass(frozen=True)
class ModerationItem:
    type: Literal["text", "image_url"]
    text: str | None = None
    image_url: str | None = None

    @classmethod
    def from_text(cls, value: str) -> ModerationItem:
        return cls(type="text", text=value)

    @classmethod
    def from_image_url(cls, url: str) -> ModerationItem:
        return cls(type="image_url", image_url=url)


@dataclass(frozen=True)
class ModerationVerdict:
    categories: dict[str, bool] = field(default_factory=dict)
    category_scores: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class ModerationDecision:
    blocked: bool
    matched_categories: list[str]
    category_scores: dict[str, float] = field(default_factory=dict)
    checked_image_urls: tuple[str, ...] | None = None
    failed_image_urls: tuple[str, ...] = ()
    # True when the block came from the check not running (backend outage,
    # unreadable image set) rather than a category match. The two need
    # different user-facing text: one asks for a reword, the other a retry.
    error: bool = False


class ModerationError(Exception):
    """Raised when a moderation backend cannot return a usable verdict."""
