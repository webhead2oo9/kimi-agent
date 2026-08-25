from __future__ import annotations

from typing import Any

from moderation.backends.openai_omni import OpenAIOmniModerationBackend
from moderation.types import ModerationItem


class FakeModerations:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "results": [
                {
                    "categories": {
                        "sexual": False,
                        "harassment/threatening": True,
                    },
                    "category_scores": {
                        "sexual": 0.1,
                        "harassment/threatening": 0.97,
                    },
                }
            ]
        }


class FakeClient:
    def __init__(self) -> None:
        self.moderations = FakeModerations()
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def test_openai_omni_backend_maps_multimodal_request_and_response() -> None:
    import asyncio

    client = FakeClient()
    backend = OpenAIOmniModerationBackend(
        api_key="test",
        model="omni-moderation-latest",
        client=client,
    )

    verdict = asyncio.run(
        backend.moderate(
            [
                ModerationItem.from_text("hello"),
                ModerationItem.from_image_url("data:image/png;base64,aW1n"),
            ]
        )
    )

    assert client.moderations.calls == [
        {
            "model": "omni-moderation-latest",
            "input": [
                {"type": "text", "text": "hello"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,aW1n"},
                },
            ],
        }
    ]
    assert verdict.categories == {
        "sexual": False,
        "harassment/threatening": True,
    }
    assert verdict.category_scores["harassment/threatening"] == 0.97
