from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence

from moderation.types import ModerationItem, ModerationVerdict


class ModerationBackend(ABC):
    @abstractmethod
    async def moderate(self, items: Sequence[ModerationItem]) -> ModerationVerdict:
        raise NotImplementedError

    async def close(self) -> None:
        return None
