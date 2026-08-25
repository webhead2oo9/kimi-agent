from __future__ import annotations

from pathlib import Path

import pytest

from storage.db import Database
from storage.model_selection import ModelSelectionStore


@pytest.mark.asyncio
async def test_model_selection_persists_and_clears_across_restarts(tmp_path: Path) -> None:
    path = tmp_path / "bot.db"
    db = Database(path)
    await db.connect()
    try:
        store = ModelSelectionStore(db)
        assert await store.get() is None
        await store.set("alternate")
        assert await store.get() == "alternate"
    finally:
        await db.close()

    reopened = Database(path)
    await reopened.connect()
    try:
        store = ModelSelectionStore(reopened)
        assert await store.get() == "alternate"
        await store.set(None)
        assert await store.get() is None
    finally:
        await reopened.close()
