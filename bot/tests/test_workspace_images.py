"""view_image."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.workspace import (
    WorkspaceToolConfig,
)
from tools.registry import BudgetName

from tests.workspace_tool_helpers import (
    WS,
    _PNG_1X1,
    _image_ctx,
    _make_ctx,
    _register,
    _requires_symlinks,
)


@pytest.mark.asyncio
async def test_view_image_queues_image_part(tmp_path: Path) -> None:
    reg, mgr = _register(tmp_path)
    ctx = _image_ctx()
    (mgr.user_files_dir(WS) / "shot.png").write_bytes(_PNG_1X1)

    result = await reg.dispatch("view_image", {"path": "shot.png"}, ctx)
    body = json.loads(result)
    assert body == {
        "path": "shot.png",
        "media_type": "image/png",
        "size_bytes": len(_PNG_1X1),
        "viewing": True,
    }
    assert len(ctx.pending_view_images) == 1
    part = ctx.pending_view_images[0]
    assert part.media_type == "image/png"
    assert part.image_url is not None
    assert part.image_url.startswith("data:image/png;base64,")
    assert ctx.budget_used(BudgetName.VIEW_IMAGES) == 1


@pytest.mark.asyncio
async def test_view_image_refuses_when_unsupported(tmp_path: Path) -> None:
    reg, mgr = _register(tmp_path)
    ctx = _make_ctx()  # images_supported defaults False
    (mgr.user_files_dir(WS) / "shot.png").write_bytes(_PNG_1X1)

    result = await reg.dispatch("view_image", {"path": "shot.png"}, ctx)
    assert json.loads(result) == {"error": "The current model can't view images."}
    assert ctx.pending_view_images == []


@pytest.mark.asyncio
async def test_view_image_rejects_non_image(tmp_path: Path) -> None:
    reg, mgr = _register(tmp_path)
    ctx = _image_ctx()
    # Right extension, wrong bytes, so the sniff must not trust the name.
    (mgr.user_files_dir(WS) / "fake.png").write_text("not an image", encoding="utf-8")

    result = await reg.dispatch("view_image", {"path": "fake.png"}, ctx)
    assert "not a supported image" in json.loads(result)["error"]
    assert ctx.pending_view_images == []


@pytest.mark.asyncio
async def test_view_image_rejects_oversize(tmp_path: Path) -> None:
    reg, mgr = _register(tmp_path, WorkspaceToolConfig(view_image_max_bytes=10))
    ctx = _image_ctx()
    (mgr.user_files_dir(WS) / "shot.png").write_bytes(_PNG_1X1)

    result = await reg.dispatch("view_image", {"path": "shot.png"}, ctx)
    assert "over the 10 byte image view limit" in json.loads(result)["error"]
    assert ctx.pending_view_images == []


@pytest.mark.asyncio
async def test_view_image_enforces_per_turn_cap(tmp_path: Path) -> None:
    reg, mgr = _register(tmp_path, WorkspaceToolConfig(view_image_max_per_turn=1))
    ctx = _image_ctx(view_image_cap=1)
    (mgr.user_files_dir(WS) / "a.png").write_bytes(_PNG_1X1)
    (mgr.user_files_dir(WS) / "b.png").write_bytes(_PNG_1X1)

    first = await reg.dispatch("view_image", {"path": "a.png"}, ctx)
    assert json.loads(first)["viewing"] is True
    second = await reg.dispatch("view_image", {"path": "b.png"}, ctx)
    assert "at most 1 images per reply" in json.loads(second)["error"]
    assert len(ctx.pending_view_images) == 1


@pytest.mark.asyncio
@_requires_symlinks
async def test_view_image_rejects_symlink(tmp_path: Path) -> None:
    reg, mgr = _register(tmp_path)
    ctx = _image_ctx()
    root = mgr.user_files_dir(WS)
    real = root / "real.png"
    real.write_bytes(_PNG_1X1)
    (root / "link.png").symlink_to(real)

    result = await reg.dispatch("view_image", {"path": "link.png"}, ctx)
    assert "error" in json.loads(result)
    assert ctx.pending_view_images == []


def test_sniff_image_media_type_covers_all_supported() -> None:
    from tools.workspace.images import sniff_image_media_type

    assert sniff_image_media_type(_PNG_1X1) == "image/png"
    assert sniff_image_media_type(b"\xff\xd8\xff\xe0\x00\x10JFIF") == "image/jpeg"
    assert sniff_image_media_type(b"GIF89a\x01\x00") == "image/gif"
    assert sniff_image_media_type(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    assert sniff_image_media_type(b"%PDF-1.7") is None
    assert sniff_image_media_type(b"") is None
