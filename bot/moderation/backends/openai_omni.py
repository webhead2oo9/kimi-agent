from __future__ import annotations

import inspect
from collections.abc import Mapping, Sequence
from typing import Any, cast

from openai import AsyncOpenAI

from moderation.backends.base import ModerationBackend
from moderation.types import ModerationError, ModerationItem, ModerationVerdict


class OpenAIOmniModerationBackend(ModerationBackend):
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = "",
        client: Any | None = None,
    ) -> None:
        kwargs: dict[str, Any] = {"api_key": api_key}
        if base_url.strip():
            kwargs["base_url"] = base_url.strip()
        self._client = client or AsyncOpenAI(**kwargs)
        self._model = model

    async def moderate(self, items: Sequence[ModerationItem]) -> ModerationVerdict:
        if not items:
            return ModerationVerdict()
        try:
            response = await self._client.moderations.create(
                model=self._model,
                input=cast(Any, [_item_to_openai(item) for item in items]),
            )
        except Exception as exc:
            raise ModerationError("OpenAI moderation request failed") from exc
        return _parse_response(response)

    async def close(self) -> None:
        close = getattr(self._client, "close", None)
        if close is None:
            return
        result = close()
        if inspect.isawaitable(result):
            await result


def _item_to_openai(item: ModerationItem) -> dict[str, Any]:
    if item.type == "text":
        return {"type": "text", "text": item.text or ""}
    return {"type": "image_url", "image_url": {"url": item.image_url or ""}}


def _parse_response(response: Any) -> ModerationVerdict:
    data = _as_mapping(response)
    results = data.get("results")
    if results is None and hasattr(response, "results"):
        results = response.results
    if not results:
        raise ModerationError("OpenAI moderation response had no results")
    first = results[0]
    result = _as_mapping(first)
    categories = _as_mapping(result.get("categories", getattr(first, "categories", {})))
    scores = _as_mapping(result.get("category_scores", getattr(first, "category_scores", {})))
    return ModerationVerdict(
        categories={str(key): bool(value) for key, value in categories.items()},
        category_scores={str(key): float(value) for key, value in scores.items()},
    )


def _as_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump()
        if isinstance(dumped, Mapping):
            return dumped
    if hasattr(value, "__dict__"):
        return vars(value)
    return {}
