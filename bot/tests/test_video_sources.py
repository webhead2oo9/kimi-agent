from __future__ import annotations

import asyncio
import os
from pathlib import Path
import threading

import pytest

from tools import video_sources
from tools.workspace.common import UserLocks
from workspace import WorkspaceManager, workspace_owner_key


@pytest.mark.asyncio
async def test_workspace_video_fifo_fails_without_blocking_a_reader_thread(tmp_path: Path) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFOs are not supported on this platform")
    manager = WorkspaceManager(tmp_path)
    key = workspace_owner_key("123", "guild")
    manager.ensure(key)
    fifo = manager.user_files_dir(key) / "blocked.mp4"
    os.mkfifo(fifo)
    try:
        with pytest.raises(ValueError, match="regular file"):
            await asyncio.wait_for(
                video_sources.workspace_source(manager, UserLocks(), key, "blocked.mp4"),
                timeout=1,
            )
    finally:
        # Unblock a regressed implementation so a failed assertion cannot leave
        # pytest's default executor stuck at event-loop shutdown.
        descriptor = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
        os.close(descriptor)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["validation", "stream"])
async def test_cancelled_workspace_open_finishes_and_closes_its_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    manager = WorkspaceManager(tmp_path)
    key = workspace_owner_key("123", "guild")
    locks = UserLocks()
    manager.ensure(key)
    (manager.user_files_dir(key) / "clip.mp4").write_bytes(b"video")
    source = await video_sources.workspace_source(manager, locks, key, "clip.mp4")
    original = video_sources._open_workspace_video
    opened = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    descriptors = []

    def slow_open(*args):
        result = original(*args)
        descriptors.append(result[0])
        loop.call_soon_threadsafe(opened.set)
        release.wait(timeout=3)
        return result

    monkeypatch.setattr(video_sources, "_open_workspace_video", slow_open)

    async def exercise() -> None:
        if phase == "validation":
            await video_sources.workspace_source(manager, locks, key, "clip.mp4")
        else:
            await anext(source.bytes.__aiter__())

    task = asyncio.create_task(exercise())
    try:
        await asyncio.wait_for(opened.wait(), timeout=1)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)
        assert descriptors
        for descriptor in descriptors:
            with pytest.raises(OSError):
                os.fstat(descriptor)
        # Cancellation must also release the workspace lock.
        await asyncio.wait_for(locks.for_user(key).acquire(), timeout=1)
        locks.for_user(key).release()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
