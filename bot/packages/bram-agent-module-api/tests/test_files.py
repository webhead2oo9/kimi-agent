from __future__ import annotations

import pytest

from bram_agent_module_api import FileAccessError, ToolAttachment, ToolFile, ToolFiles
from bram_agent_module_api.testing import FakeToolFiles


@pytest.mark.asyncio
async def test_fake_preserves_bytes_and_enforces_aggregate_budget() -> None:
    attachment = ToolAttachment("a", "image.png", 3, "image/png")
    file = ToolFile("image.png", "image/png", b"raw")
    fake = FakeToolFiles(
        (attachment,),
        attachment_files={"a": file},
        workspace_files={"saved.png": file},
        max_bytes=5,
    )
    port: ToolFiles = fake
    assert (await port.read_attachment("a", max_bytes=3)).data == b"raw"
    with pytest.raises(FileAccessError, match="limit"):
        await port.read_workspace("saved.png", max_bytes=3)
    assert "raw" not in repr(file)
    fake.close()
    with pytest.raises(FileAccessError) as error:
        await port.read_attachment("a", max_bytes=3)
    assert error.value.code == "expired"


@pytest.mark.asyncio
async def test_fake_does_not_read_unavailable_attachments() -> None:
    fake = FakeToolFiles(
        (ToolAttachment("a", "audio.wav", 3, "audio/wav", unavailable_reason="Blocked"),),
        attachment_files={"a": ToolFile("audio.wav", "audio/wav", b"raw")},
    )
    with pytest.raises(FileAccessError, match="Blocked"):
        await fake.read_attachment("a", max_bytes=3)
    with pytest.raises(FileAccessError) as error:
        await fake.read_attachment("other", max_bytes=3)
    assert error.value.code == "unknown_attachment"
    assert not fake.reads
