import asyncio
import base64
import hashlib
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent import attachments as attachments_module
from agent.attachments import (
    AttachmentRef,
    AttachmentStore,
    cleanup_attachment_paths,
    collect_reply_context,
    collect_turn_attachments,
    collect_turn_images,
    format_attachments_context,
    image_byte_hashes,
    message_has_image_attachment,
    turn_has_image_input,
)
from providers.types import ContentPart, ConversationMessage

_PNG_HEADER = b"\x89PNG\r\n\x1a\n"


class FakeAttachment:
    """discord.Attachment stand-in: filename, content_type, size, and an async read()."""

    def __init__(self, *, filename: str, content_type: str | None, payload: bytes) -> None:
        self.filename = filename
        self.content_type: str | None = content_type
        self.size = len(payload)
        self.url = f"https://cdn.discordapp.com/attachments/1/2/{filename}"
        self._payload = payload

    async def read(self) -> bytes:
        return self._payload


class CountingAttachment(FakeAttachment):
    """FakeAttachment that counts read() calls, for tests asserting no re-fetch."""

    def __init__(self, *, filename: str, content_type: str | None, payload: bytes) -> None:
        super().__init__(filename=filename, content_type=content_type, payload=payload)
        self.read_count = 0

    async def read(self) -> bytes:
        self.read_count += 1
        return await super().read()


def _collect_current(store: AttachmentStore, message: SimpleNamespace) -> list[ContentPart]:
    # The current-message images are the baseline of collect_turn_images' vision_parts.
    result = asyncio.run(
        collect_turn_images(
            message,
            store=store,
            conversation_key="guild:chan",
            detail="auto",
            images_supported=True,
            history_hashes=set(),
            lookback=1,
            max_images=1,
        )
    )
    return result.vision_parts


def test_collect_turn_images_stores_current_message_images(tmp_path: Path) -> None:
    store = AttachmentStore(base_dir=tmp_path, max_bytes=1024)
    message = SimpleNamespace(
        id=55,
        attachments=[
            FakeAttachment(
                filename="cat.png",
                content_type="image/png",
                payload=_PNG_HEADER,
            )
        ],
    )

    parts = _collect_current(store, message)

    assert len(parts) == 1
    assert parts[0].media_type == "image/png"
    assert parts[0].image_url == "data:image/png;base64,iVBORw0KGgo="
    assert list(tmp_path.rglob("cat.png"))


def test_collect_turn_images_uses_filename_when_content_type_missing(
    tmp_path: Path,
) -> None:
    store = AttachmentStore(base_dir=tmp_path, max_bytes=1024)
    message = SimpleNamespace(
        id=55,
        attachments=[
            FakeAttachment(
                filename="cat.png",
                content_type=None,
                payload=_PNG_HEADER,
            )
        ],
    )

    parts = _collect_current(store, message)

    assert len(parts) == 1
    assert parts[0].media_type == "image/png"
    assert parts[0].image_url == "data:image/png;base64,iVBORw0KGgo="
    assert list(tmp_path.rglob("cat.png"))


def test_collect_turn_images_uses_filename_when_content_type_is_generic(
    tmp_path: Path,
) -> None:
    store = AttachmentStore(base_dir=tmp_path, max_bytes=1024)
    message = SimpleNamespace(
        id=55,
        attachments=[
            FakeAttachment(
                filename="cat.png",
                content_type="application/octet-stream",
                payload=_PNG_HEADER,
            )
        ],
    )

    parts = _collect_current(store, message)

    assert len(parts) == 1
    assert parts[0].media_type == "image/png"
    assert parts[0].image_url == "data:image/png;base64,iVBORw0KGgo="
    assert list(tmp_path.rglob("cat.png"))


def test_collect_turn_images_tracks_validated_current_attachment_identity(
    tmp_path: Path,
) -> None:
    store = AttachmentStore(base_dir=tmp_path, max_bytes=1024)
    attachment = FakeAttachment(
        filename="cat.png",
        content_type="application/octet-stream",
        payload=_PNG_HEADER,
    )
    message = SimpleNamespace(id=55, attachments=[attachment])

    result = asyncio.run(
        collect_turn_images(
            message,
            store=store,
            conversation_key="guild:chan",
            detail="auto",
            images_supported=True,
            history_hashes=set(),
            lookback=1,
            max_images=1,
        )
    )

    assert result.current_attachment_source_ids == frozenset({id(attachment)})


def test_collect_turn_images_sniffs_actual_media_type(tmp_path: Path) -> None:
    store = AttachmentStore(base_dir=tmp_path, max_bytes=1024)
    message = SimpleNamespace(
        id=55,
        attachments=[
            FakeAttachment(
                filename="cat.webp",
                content_type="image/webp",
                payload=_PNG_HEADER,
            )
        ],
    )

    parts = _collect_current(store, message)

    assert len(parts) == 1
    assert parts[0].media_type == "image/png"
    assert parts[0].image_url == "data:image/png;base64,iVBORw0KGgo="


@pytest.mark.asyncio
async def test_attachment_store_rechecks_payload_size_after_read(tmp_path: Path) -> None:
    store = AttachmentStore(base_dir=tmp_path, max_bytes=4)
    attachment = FakeAttachment(
        filename="a.png",
        content_type="image/png",
        payload=b"toolong",
    )
    attachment.size = 1

    with pytest.raises(ValueError, match="Attachment exceeds"):
        await store.save(
            conversation_key="guild:chan",
            message_id=55,
            attachment=attachment,
        )

    assert not list(tmp_path.rglob("a.png"))


@pytest.mark.asyncio
async def test_attachment_store_stages_without_blocking_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agent.attachments as attachments_module

    original_stage = attachments_module._stage_payload_sync
    stage_started = threading.Event()
    release_stage = threading.Event()

    def blocking_stage(path: Path, payload: bytes) -> None:
        stage_started.set()
        assert release_stage.wait(timeout=2)
        original_stage(path, payload)

    monkeypatch.setattr(attachments_module, "_stage_payload_sync", blocking_stage)
    store = AttachmentStore(base_dir=tmp_path, max_bytes=1024)
    attachment = FakeAttachment(
        filename="a.png",
        content_type="image/png",
        payload=_PNG_HEADER,
    )

    save_task = asyncio.create_task(
        store.save(
            conversation_key="guild:chan",
            message_id=55,
            attachment=attachment,
        )
    )
    assert await asyncio.to_thread(stage_started.wait, 1)
    # This coroutine can run while the filesystem operation is blocked in its
    # worker thread. A synchronous write on the loop would deadlock here.
    await asyncio.sleep(0)
    release_stage.set()
    await save_task


@pytest.mark.asyncio
async def test_attachment_store_cancellation_during_stage_removes_atomic_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agent.attachments as attachments_module

    original_stage = attachments_module._stage_payload_sync
    stage_started = threading.Event()
    release_stage = threading.Event()

    def blocking_stage(path: Path, payload: bytes) -> None:
        stage_started.set()
        assert release_stage.wait(timeout=2)
        original_stage(path, payload)

    monkeypatch.setattr(attachments_module, "_stage_payload_sync", blocking_stage)
    store = AttachmentStore(base_dir=tmp_path, max_bytes=1024)
    task = asyncio.create_task(
        store.save(
            conversation_key="guild:chan",
            message_id=55,
            attachment=FakeAttachment(
                filename="cancel.png",
                content_type="image/png",
                payload=_PNG_HEADER,
            ),
        )
    )
    assert await asyncio.to_thread(stage_started.wait, 1)
    task.cancel()
    release_stage.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not list(tmp_path.rglob("cancel.png"))
    assert not list(tmp_path.rglob(".cancel.png.*"))


@pytest.mark.asyncio
async def test_attachment_store_repeated_cancellation_still_removes_atomic_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agent.attachments as attachments_module

    original_stage = attachments_module._stage_payload_sync
    stage_started = threading.Event()
    release_stage = threading.Event()

    def blocking_stage(path: Path, payload: bytes) -> None:
        stage_started.set()
        assert release_stage.wait(timeout=2)
        original_stage(path, payload)

    monkeypatch.setattr(attachments_module, "_stage_payload_sync", blocking_stage)
    store = AttachmentStore(base_dir=tmp_path, max_bytes=1024)
    task = asyncio.create_task(
        store.save(
            conversation_key="guild:chan",
            message_id=55,
            attachment=FakeAttachment(
                filename="double-cancel.png",
                content_type="image/png",
                payload=_PNG_HEADER,
            ),
        )
    )
    assert await asyncio.to_thread(stage_started.wait, 1)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    release_stage.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not list(tmp_path.rglob("double-cancel.png"))
    assert not list(tmp_path.rglob(".double-cancel.png.*"))


@pytest.mark.asyncio
async def test_attachment_store_removes_partial_stage_when_replace_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agent.attachments as attachments_module

    def fail_replace(source: str | Path, destination: str | Path) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(attachments_module.os, "replace", fail_replace)
    store = AttachmentStore(base_dir=tmp_path, max_bytes=1024)
    with pytest.raises(OSError, match="simulated"):
        await store.save(
            conversation_key="guild:chan",
            message_id=55,
            attachment=FakeAttachment(
                filename="partial.png",
                content_type="image/png",
                payload=_PNG_HEADER,
            ),
        )

    assert not list(tmp_path.rglob("partial.png"))
    assert not list(tmp_path.rglob(".partial.png.*"))


@pytest.mark.asyncio
async def test_overwritten_orphan_is_turn_owned_and_cleanup_prunes_directories(
    tmp_path: Path,
) -> None:
    orphan = tmp_path / "k" / "1" / "a.png"
    orphan.parent.mkdir(parents=True)
    orphan.write_bytes(b"old orphan")
    store = AttachmentStore(base_dir=tmp_path, max_bytes=1024)

    result = await collect_turn_images(
        SimpleNamespace(
            id=1,
            attachments=[
                FakeAttachment(
                    filename="a.png",
                    content_type="image/png",
                    payload=_PNG_HEADER,
                )
            ],
        ),
        store=store,
        conversation_key="k",
        detail="auto",
        images_supported=True,
        history_hashes=set(),
        lookback=0,
        max_images=1,
    )

    assert result.cleanup_paths == [orphan]
    assert orphan.read_bytes() == _PNG_HEADER
    await cleanup_attachment_paths(result.cleanup_paths)
    assert not orphan.exists()
    assert not orphan.parent.exists()
    assert not orphan.parent.parent.exists()


@pytest.mark.asyncio
async def test_attachment_orphan_sweep_is_age_and_work_bounded(tmp_path: Path) -> None:
    store = AttachmentStore(base_dir=tmp_path, max_bytes=1024)
    old_files = [tmp_path / "conversation" / "1" / f"old-{index}.png" for index in range(2)]
    for path in old_files:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_PNG_HEADER)
        old_timestamp = time.time() - 3600
        os.utime(path, (old_timestamp, old_timestamp))

    # Work bound: one bounded sweep removes exactly one aged file. Only aged
    # files exist at this point, so the assertion holds regardless of the
    # filesystem's directory iteration order.
    assert await store.sweep_orphans(max_age_seconds=60, max_files=1) == 1
    assert sum(path.exists() for path in old_files) == 1

    # Age gate: a fresh file survives while the remaining aged file is removed.
    fresh = tmp_path / "conversation" / "2" / "fresh.png"
    fresh.parent.mkdir(parents=True)
    fresh.write_bytes(_PNG_HEADER)
    assert await store.sweep_orphans(max_age_seconds=60, max_files=10) == 1
    assert not any(path.exists() for path in old_files)
    assert fresh.exists()


@pytest.mark.asyncio
async def test_attachment_orphan_sweep_does_not_follow_symlinks(tmp_path: Path) -> None:
    store_root = tmp_path / "store"
    outside = tmp_path / "outside"
    outside_file = outside / "1" / "outside.png"
    outside_file.parent.mkdir(parents=True)
    outside_file.write_bytes(_PNG_HEADER)
    old_timestamp = time.time() - 3600
    os.utime(outside_file, (old_timestamp, old_timestamp))
    store_root.mkdir()
    try:
        (store_root / "linked-conversation").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable on this platform")

    store = AttachmentStore(base_dir=store_root, max_bytes=1024)
    assert await store.sweep_orphans(max_age_seconds=0, max_files=10) == 0
    assert outside_file.exists()


@pytest.mark.asyncio
async def test_collect_turn_images_cancellation_removes_partial_staged_files(
    tmp_path: Path,
) -> None:
    read_started = asyncio.Event()

    class HangingAttachment(FakeAttachment):
        async def read(self) -> bytes:
            read_started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    store = AttachmentStore(base_dir=tmp_path, max_bytes=1024)
    message = SimpleNamespace(
        id=55,
        attachments=[
            FakeAttachment(
                filename="first.png",
                content_type="image/png",
                payload=_PNG_HEADER,
            ),
            HangingAttachment(
                filename="second.png",
                content_type="image/png",
                payload=_PNG_HEADER,
            ),
        ],
    )
    task = asyncio.create_task(
        collect_turn_images(
            message,
            store=store,
            conversation_key="guild:chan",
            detail="auto",
            images_supported=True,
            history_hashes=set(),
            lookback=1,
            max_images=2,
        )
    )
    await read_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not list(tmp_path.rglob("*.png"))


def test_collect_turn_images_skips_non_images(tmp_path: Path) -> None:
    store = AttachmentStore(base_dir=tmp_path, max_bytes=1024)
    message = SimpleNamespace(
        id=55,
        attachments=[
            FakeAttachment(
                filename="notes.txt",
                content_type="text/plain",
                payload=b"hello",
            )
        ],
    )

    parts = _collect_current(store, message)

    assert parts == []


@pytest.mark.asyncio
async def test_collect_turn_images_marks_declared_image_with_invalid_bytes_unavailable(
    tmp_path: Path,
) -> None:
    store = AttachmentStore(base_dir=tmp_path, max_bytes=1024)
    message = SimpleNamespace(
        id=55,
        attachments=[
            FakeAttachment(
                filename="fake.png",
                content_type="image/png",
                payload=b"not an image",
            )
        ],
    )

    result = await collect_turn_images(
        message,
        store=store,
        conversation_key="guild:chan",
        detail="auto",
        images_supported=True,
        history_hashes=set(),
        lookback=0,
        max_images=1,
    )

    assert result.vision_parts == []
    assert result.current_image_unavailable is True
    assert not list(tmp_path.rglob("fake.png"))


def _img_part(payload: bytes, media_type: str = "image/png") -> ContentPart:
    b64 = base64.b64encode(payload).decode("ascii")
    return ContentPart.from_image_url(url=f"data:{media_type};base64,{b64}", media_type=media_type)


def test_image_byte_hashes_hashes_history_image_parts_only() -> None:
    payload = b"\x89PNG\r\n\x1a\nHELLO"
    history = [
        ConversationMessage(
            role="user",
            content=[
                ContentPart.from_text("hi"),
                _img_part(payload),
            ],
        ),
        ConversationMessage(role="assistant", content=[ContentPart.from_text("text only")]),
    ]
    hashes = image_byte_hashes(history)
    assert hashes == {hashlib.sha256(payload).hexdigest()}


def test_image_byte_hashes_ignores_malformed_data_urls() -> None:
    bad = ConversationMessage(
        role="user",
        content=[
            ContentPart.from_image_url(
                url="data:image/png;base64,!!notb64!!", media_type="image/png"
            ),
        ],
    )
    assert image_byte_hashes([bad]) == set()


class _FakeAttachment:
    def __init__(self, payload: bytes, content_type: str = "image/png", name: str = "a.png"):
        self._payload = payload if payload.startswith(_PNG_HEADER) else _PNG_HEADER + payload
        self.content_type: str | None = content_type
        self.filename = name
        self.size = len(self._payload)

    async def read(self) -> bytes:
        return self._payload


class _CountingImageAttachment(_FakeAttachment):
    def __init__(self, payload: bytes, content_type: str = "image/png", name: str = "a.png"):
        super().__init__(payload, content_type, name)
        self.read_count = 0

    async def read(self) -> bytes:
        self.read_count += 1
        return await super().read()


class _FakeRef:
    def __init__(self, *, resolved=None, message_id=None, channel_id=None):
        self.resolved = resolved
        self.message_id = message_id
        self.channel_id = channel_id


class _FakeChannel:
    def __init__(self, channel_id=10, history_messages=None, fetchable=None):
        self.id = channel_id
        self._history = history_messages or []
        self._fetchable = fetchable or {}

    def history(self, *, limit, before):
        msgs = self._history[:limit]

        async def _gen():
            for m in msgs:
                yield m

        return _gen()

    async def fetch_message(self, message_id):
        if message_id in self._fetchable:
            return self._fetchable[message_id]
        raise RuntimeError("not found")


class _FakeMessage:
    def __init__(
        self,
        *,
        msg_id=1,
        attachments=None,
        reference=None,
        channel=None,
        author_id=123,
        author_name="Alice",
        author_bot=False,
        content="",
    ):
        self.id = msg_id
        self.attachments = attachments or []
        self.reference = reference
        self.channel = channel or _FakeChannel()
        self.author = SimpleNamespace(
            id=author_id,
            display_name=author_name,
            bot=author_bot,
        )
        self.content = content


def _store(tmp_path):
    return AttachmentStore(base_dir=tmp_path, max_bytes=8 * 1024 * 1024)


@pytest.mark.asyncio
async def test_current_message_image_is_baseline_vision_and_no_edit_target(tmp_path):
    msg = _FakeMessage(attachments=[_FakeAttachment(b"CURRENT")])
    result = await collect_turn_images(
        msg,
        store=_store(tmp_path),
        conversation_key="k",
        detail="auto",
        images_supported=True,
        history_hashes=set(),
        lookback=10,
        max_images=4,
    )
    assert len(result.vision_parts) == 1
    assert result.edit_target is None  # current upload, no reply -> existing scan handles edit


@pytest.mark.asyncio
async def test_current_message_images_are_capped_by_max_images(tmp_path):
    msg = _FakeMessage(
        attachments=[
            _FakeAttachment(b"ONE", name="one.png"),
            _FakeAttachment(b"TWO", name="two.png"),
            _FakeAttachment(b"THREE", name="three.png"),
        ]
    )
    result = await collect_turn_images(
        msg,
        store=_store(tmp_path),
        conversation_key="k",
        detail="auto",
        images_supported=True,
        history_hashes=set(),
        lookback=10,
        max_images=2,
    )
    assert len(result.vision_parts) == 2


@pytest.mark.asyncio
async def test_current_image_cap_prevents_excess_attachment_reads(tmp_path: Path) -> None:
    attachments = [
        _CountingImageAttachment(b"ONE", name="one.png"),
        _CountingImageAttachment(b"TWO", name="two.png"),
        _CountingImageAttachment(b"THREE", name="three.png"),
    ]
    result = await collect_turn_images(
        _FakeMessage(attachments=attachments),
        store=_store(tmp_path),
        conversation_key="k",
        detail="auto",
        images_supported=True,
        history_hashes=set(),
        lookback=0,
        max_images=2,
    )

    assert len(result.vision_parts) == 2
    assert [attachment.read_count for attachment in attachments] == [1, 1, 0]


@pytest.mark.asyncio
async def test_turn_aggregate_byte_budget_prevents_later_read(tmp_path: Path) -> None:
    first = _CountingImageAttachment(b"123456", name="first.png")
    second = _CountingImageAttachment(b"abcdef", name="second.png")
    assert first.size == second.size == 14
    store = AttachmentStore(base_dir=tmp_path, max_bytes=20, max_total_bytes=20)

    result = await collect_turn_images(
        _FakeMessage(attachments=[first, second]),
        store=store,
        conversation_key="k",
        detail="auto",
        images_supported=True,
        history_hashes=set(),
        lookback=0,
        max_images=2,
    )

    assert len(result.vision_parts) == 1
    assert (first.read_count, second.read_count) == (1, 0)


@pytest.mark.asyncio
async def test_dishonest_attachment_size_exhausts_turn_byte_budget(tmp_path: Path) -> None:
    dishonest = _CountingImageAttachment(b"X" * 100, name="dishonest.png")
    dishonest.size = 1
    second = _CountingImageAttachment(b"OK", name="second.png")
    store = AttachmentStore(base_dir=tmp_path, max_bytes=200, max_total_bytes=20)

    result = await collect_turn_images(
        _FakeMessage(attachments=[dishonest, second]),
        store=store,
        conversation_key="k",
        detail="auto",
        images_supported=True,
        history_hashes=set(),
        lookback=0,
        max_images=2,
    )

    assert result.vision_parts == []
    assert result.current_image_unavailable is True
    assert (dishonest.read_count, second.read_count) == (1, 0)
    assert not list(tmp_path.rglob("*.png"))


@pytest.mark.asyncio
async def test_staging_failure_still_spends_declared_turn_byte_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agent.attachments as attachments_module

    first = _CountingImageAttachment(b"123456", name="first.png")
    second = _CountingImageAttachment(b"abcdef", name="second.png")
    assert first.size == second.size == 14

    def fail_stage(path: Path, payload: bytes) -> None:
        del path, payload
        raise OSError("simulated staging failure")

    monkeypatch.setattr(attachments_module, "_stage_payload_sync", fail_stage)
    result = await collect_turn_images(
        _FakeMessage(attachments=[first, second]),
        store=AttachmentStore(base_dir=tmp_path, max_bytes=20, max_total_bytes=20),
        conversation_key="k",
        detail="auto",
        images_supported=True,
        history_hashes=set(),
        lookback=0,
        max_images=2,
    )

    assert result.vision_parts == []
    assert result.current_image_unavailable is True
    assert (first.read_count, second.read_count) == (1, 0)
    assert not list(tmp_path.rglob("*.png"))


@pytest.mark.asyncio
async def test_turn_byte_budget_is_shared_with_reply_edit_target(tmp_path: Path) -> None:
    current = _CountingImageAttachment(b"123456", name="current.png")
    reply = _CountingImageAttachment(b"abcdef", name="reply.png")
    referenced = _FakeMessage(msg_id=2, attachments=[reply])
    message = _FakeMessage(
        attachments=[current],
        reference=_FakeRef(resolved=referenced, channel_id=10),
        channel=_FakeChannel(channel_id=10),
    )

    result = await collect_turn_images(
        message,
        store=AttachmentStore(base_dir=tmp_path, max_bytes=20, max_total_bytes=20),
        conversation_key="k",
        detail="auto",
        images_supported=True,
        history_hashes=set(),
        lookback=10,
        max_images=2,
    )

    assert len(result.vision_parts) == 1
    assert result.edit_target is None
    assert (current.read_count, reply.read_count) == (1, 0)


@pytest.mark.asyncio
async def test_zero_image_budget_reads_no_attachments(tmp_path: Path) -> None:
    current = _CountingImageAttachment(b"CURRENT", name="current.png")
    reply_first = _CountingImageAttachment(b"REPLY1", name="reply1.png")
    reply_second = _CountingImageAttachment(b"REPLY2", name="reply2.png")
    referenced = _FakeMessage(msg_id=2, attachments=[reply_first, reply_second])
    message = _FakeMessage(
        attachments=[current],
        reference=_FakeRef(resolved=referenced, channel_id=10),
        channel=_FakeChannel(channel_id=10),
    )

    result = await collect_turn_images(
        message,
        store=_store(tmp_path),
        conversation_key="k",
        detail="auto",
        images_supported=True,
        history_hashes=set(),
        lookback=10,
        max_images=0,
    )

    assert result.vision_parts == []
    assert result.edit_target is None
    assert (current.read_count, reply_first.read_count, reply_second.read_count) == (0, 0, 0)


@pytest.mark.asyncio
async def test_zero_vision_budget_does_not_replace_current_image_with_history(
    tmp_path: Path,
) -> None:
    current = _CountingImageAttachment(b"CURRENT", name="current.png")
    old = _CountingImageAttachment(b"OLD", name="old.png")
    history_message = _FakeMessage(msg_id=2, attachments=[old], author_id=123)
    message = _FakeMessage(
        attachments=[current],
        channel=_FakeChannel(channel_id=10, history_messages=[history_message]),
        author_id=123,
    )

    result = await collect_turn_images(
        message,
        store=_store(tmp_path),
        conversation_key="k",
        detail="auto",
        images_supported=True,
        history_hashes=set(),
        lookback=10,
        max_images=0,
    )

    assert result.vision_parts == []
    assert result.edit_target is None
    assert (current.read_count, old.read_count) == (0, 0)


@pytest.mark.asyncio
async def test_reply_image_becomes_vision_part_and_edit_target(tmp_path):
    referenced = _FakeMessage(msg_id=2, attachments=[_FakeAttachment(b"REPLYIMG")])
    channel = _FakeChannel(channel_id=10)
    ref = _FakeRef(resolved=referenced, channel_id=10)
    msg = _FakeMessage(attachments=[], reference=ref, channel=channel)
    result = await collect_turn_images(
        msg,
        store=_store(tmp_path),
        conversation_key="k",
        detail="auto",
        images_supported=True,
        history_hashes=set(),
        lookback=10,
        max_images=4,
    )
    assert len(result.vision_parts) == 1
    assert result.edit_target is not None
    assert result.edit_target.image_url == result.vision_parts[0].image_url


@pytest.mark.asyncio
async def test_reply_image_from_current_bot_requires_explicit_permission(tmp_path: Path) -> None:
    attachment = _CountingImageAttachment(b"BOTREPLY")
    referenced = _FakeMessage(
        msg_id=2,
        attachments=[attachment],
        author_id=999,
        author_bot=True,
    )
    message = _FakeMessage(
        reference=_FakeRef(resolved=referenced, channel_id=10),
        channel=_FakeChannel(channel_id=10),
    )
    bot_user = SimpleNamespace(id=999)

    excluded = await collect_turn_images(
        message,
        bot_user=bot_user,
        store=_store(tmp_path),
        conversation_key="excluded",
        detail="auto",
        images_supported=True,
        history_hashes=set(),
        lookback=0,
        max_images=1,
    )
    allowed = await collect_turn_images(
        message,
        bot_user=bot_user,
        allow_bot_authored=True,
        store=_store(tmp_path),
        conversation_key="allowed",
        detail="auto",
        images_supported=True,
        history_hashes=set(),
        lookback=0,
        max_images=1,
    )

    assert excluded.reply_images == ()
    assert excluded.edit_target is None
    assert len(allowed.reply_images) == 1
    assert allowed.edit_target is not None
    assert attachment.read_count == 1
    await cleanup_attachment_paths(allowed.cleanup_paths)


@pytest.mark.asyncio
async def test_reply_image_from_other_bot_remains_excluded_when_explicit(tmp_path: Path) -> None:
    attachment = _CountingImageAttachment(b"OTHERBOT")
    referenced = _FakeMessage(
        msg_id=2,
        attachments=[attachment],
        author_id=888,
        author_bot=True,
    )
    message = _FakeMessage(
        reference=_FakeRef(resolved=referenced, channel_id=10),
        channel=_FakeChannel(channel_id=10),
    )

    result = await collect_turn_images(
        message,
        bot_user=SimpleNamespace(id=999),
        allow_bot_authored=True,
        store=_store(tmp_path),
        conversation_key="k",
        detail="auto",
        images_supported=True,
        history_hashes=set(),
        lookback=0,
        max_images=1,
    )

    assert result.reply_images == ()
    assert result.edit_target is None
    assert attachment.read_count == 0


@pytest.mark.asyncio
async def test_collect_turn_images_cancellation_in_reply_rolls_back_current_phase(
    tmp_path: Path,
) -> None:
    reply_read_started = asyncio.Event()

    class _HangingReplyAttachment(_FakeAttachment):
        async def read(self) -> bytes:
            reply_read_started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    referenced = _FakeMessage(
        msg_id=2,
        attachments=[_HangingReplyAttachment(b"REPLY", name="reply.png")],
    )
    ref = _FakeRef(resolved=referenced, channel_id=10)
    message = _FakeMessage(
        attachments=[_FakeAttachment(b"CURRENT", name="current.png")],
        reference=ref,
        channel=_FakeChannel(channel_id=10),
    )
    task = asyncio.create_task(
        collect_turn_images(
            message,
            store=_store(tmp_path),
            conversation_key="k",
            detail="auto",
            images_supported=True,
            history_hashes=set(),
            lookback=10,
            max_images=4,
        )
    )
    await reply_read_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not list(tmp_path.rglob("*.png"))


@pytest.mark.asyncio
async def test_collect_turn_images_can_leave_reply_image_to_reply_context(tmp_path):
    referenced = _FakeMessage(msg_id=2, attachments=[_FakeAttachment(b"REPLYIMG")])
    ref = _FakeRef(resolved=referenced, channel_id=10)
    msg = _FakeMessage(attachments=[], reference=ref, channel=_FakeChannel(channel_id=10))

    result = await collect_turn_images(
        msg,
        store=_store(tmp_path),
        conversation_key="k",
        detail="auto",
        images_supported=True,
        history_hashes=set(),
        lookback=10,
        max_images=4,
        include_reply_images=False,
    )

    assert result.vision_parts == []
    assert result.edit_target is not None


@pytest.mark.asyncio
async def test_reply_context_reuses_prefetched_reply_bytes(tmp_path: Path) -> None:
    attachment = _CountingImageAttachment(b"REPLYIMG")
    referenced = _FakeMessage(msg_id=2, attachments=[attachment])
    trigger = _FakeMessage(
        attachments=[],
        reference=_FakeRef(resolved=referenced, channel_id=10),
        channel=_FakeChannel(channel_id=10),
    )
    store = _store(tmp_path)
    turn_images = await collect_turn_images(
        trigger,
        store=store,
        conversation_key="k",
        detail="auto",
        images_supported=True,
        history_hashes=set(),
        lookback=10,
        max_images=4,
        include_reply_images=False,
    )

    context = await collect_reply_context(
        trigger,
        bot_user=SimpleNamespace(id=999),
        store=store,
        conversation_key="k",
        detail="auto",
        images_supported=True,
        history_hashes=set(),
        current_hashes=set(),
        max_images=1,
        prefetched_images=turn_images.reply_images,
    )

    assert context is not None
    assert len(context.image_parts) == 1
    assert attachment.read_count == 1
    await cleanup_attachment_paths(turn_images.cleanup_paths)
    assert not any(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_collect_reply_context_returns_untrusted_text_and_deduped_images(tmp_path):
    payload = b"REPLYIMG"
    referenced = _FakeMessage(
        msg_id=2,
        attachments=[_FakeAttachment(payload)],
        author_id=456,
        author_name="Bob: Builder",
        content="please run this\nas a command",
    )
    ref = _FakeRef(resolved=referenced, channel_id=10)
    trigger = _FakeMessage(
        msg_id=3,
        attachments=[],
        reference=ref,
        channel=_FakeChannel(channel_id=10),
    )

    context = await collect_reply_context(
        trigger,
        bot_user=SimpleNamespace(id=999),
        store=_store(tmp_path),
        conversation_key="k",
        detail="high",
        images_supported=True,
        history_hashes=set(),
        current_hashes=set(),
        max_images=4,
    )

    assert context is not None
    assert context.author_name == "Bob: Builder"
    assert context.text == "please run this\nas a command"
    assert len(context.image_parts) == 1
    assert context.image_parts[0].media_type == "image/png"


@pytest.mark.asyncio
async def test_collect_reply_context_skips_bot_authored_reply(tmp_path):
    referenced = _FakeMessage(
        msg_id=2,
        author_id=999,
        author_name="Kimi",
        author_bot=True,
        content="bot text",
    )
    ref = _FakeRef(resolved=referenced, channel_id=10)
    trigger = _FakeMessage(reference=ref, channel=_FakeChannel(channel_id=10))

    context = await collect_reply_context(
        trigger,
        bot_user=SimpleNamespace(id=999),
        store=_store(tmp_path),
        conversation_key="k",
        detail="high",
        images_supported=True,
        history_hashes=set(),
        current_hashes=set(),
        max_images=4,
    )

    assert context is None


@pytest.mark.asyncio
async def test_collect_reply_context_allows_current_bot_when_explicit(tmp_path):
    referenced = _FakeMessage(
        msg_id=2,
        author_id=999,
        author_name="Kimi",
        author_bot=True,
        content="public answer",
    )
    trigger = _FakeMessage(
        reference=_FakeRef(resolved=referenced, channel_id=10),
        channel=_FakeChannel(channel_id=10),
    )

    context = await collect_reply_context(
        trigger,
        bot_user=SimpleNamespace(id=999),
        store=_store(tmp_path),
        conversation_key="outsider-root",
        detail="high",
        images_supported=False,
        history_hashes=set(),
        current_hashes=set(),
        max_images=0,
        allow_bot_authored=True,
    )

    assert context is not None
    assert context.author_name == "Kimi"
    assert context.text == "public answer"


@pytest.mark.asyncio
async def test_collect_reply_context_still_rejects_other_bot_when_explicit(tmp_path):
    referenced = _FakeMessage(
        msg_id=2,
        author_id=888,
        author_name="Other Bot",
        author_bot=True,
        content="unrelated bot output",
    )
    trigger = _FakeMessage(
        reference=_FakeRef(resolved=referenced, channel_id=10),
        channel=_FakeChannel(channel_id=10),
    )

    context = await collect_reply_context(
        trigger,
        bot_user=SimpleNamespace(id=999),
        store=_store(tmp_path),
        conversation_key="outsider-root",
        detail="high",
        images_supported=False,
        history_hashes=set(),
        current_hashes=set(),
        max_images=0,
        allow_bot_authored=True,
    )

    assert context is None


def test_message_has_image_attachment_detects_image():
    msg = _FakeMessage(attachments=[_FakeAttachment(b"X", content_type="image/png")])
    assert message_has_image_attachment(msg) is True


def test_message_has_image_attachment_detects_image_filename_without_content_type():
    msg = _FakeMessage(
        attachments=[
            _FakeAttachment(b"X", name="photo.png", content_type=None),
        ]
    )
    assert message_has_image_attachment(msg) is True


def test_message_has_image_attachment_detects_image_filename_with_generic_content_type():
    msg = _FakeMessage(
        attachments=[
            _FakeAttachment(
                b"X",
                name="photo.png",
                content_type="application/octet-stream",
            ),
        ]
    )
    assert message_has_image_attachment(msg) is True


def test_message_has_image_attachment_false_for_non_image():
    msg = _FakeMessage(attachments=[_FakeAttachment(b"X", content_type="text/plain")])
    assert message_has_image_attachment(msg) is False


def test_message_has_image_attachment_false_when_no_attachments():
    assert message_has_image_attachment(SimpleNamespace()) is False


class _AlwaysImageChannel:
    """Channel whose history yields an image regardless of the requested limit."""

    def __init__(self, channel_id, image_message):
        self.id = channel_id
        self._image_message = image_message
        self.history_called = False

    def history(self, *, limit, before):
        self.history_called = True

        async def _gen():
            yield self._image_message

        return _gen()


@pytest.mark.asyncio
async def test_history_images_skipped_when_lookback_not_positive(tmp_path):
    older = _FakeMessage(msg_id=5, attachments=[_FakeAttachment(b"HISTIMG")])
    channel = _AlwaysImageChannel(10, older)
    msg = _FakeMessage(attachments=[], reference=None, channel=channel)
    result = await collect_turn_images(
        msg,
        store=_store(tmp_path),
        conversation_key="k",
        detail="auto",
        images_supported=True,
        history_hashes=set(),
        lookback=0,
        max_images=4,
    )
    assert result.vision_parts == []
    assert result.edit_target is None
    assert channel.history_called is False


@pytest.mark.asyncio
async def test_newest_from_history_when_no_current_or_reply(tmp_path):
    older = _FakeMessage(msg_id=5, attachments=[_FakeAttachment(b"HISTIMG")])
    channel = _FakeChannel(channel_id=10, history_messages=[older])
    msg = _FakeMessage(attachments=[], reference=None, channel=channel)
    result = await collect_turn_images(
        msg,
        store=_store(tmp_path),
        conversation_key="k",
        detail="auto",
        images_supported=True,
        history_hashes=set(),
        lookback=10,
        max_images=4,
    )
    assert result.edit_target is not None
    assert len(result.vision_parts) == 1


@pytest.mark.asyncio
async def test_newest_history_image_from_other_author_is_ignored(tmp_path):
    older = _FakeMessage(
        msg_id=5,
        attachments=[_FakeAttachment(b"HISTIMG")],
        author_id=999,
    )
    channel = _FakeChannel(channel_id=10, history_messages=[older])
    msg = _FakeMessage(attachments=[], reference=None, channel=channel, author_id=123)
    result = await collect_turn_images(
        msg,
        store=_store(tmp_path),
        conversation_key="k",
        detail="auto",
        images_supported=True,
        history_hashes=set(),
        lookback=10,
        max_images=4,
    )
    assert result.edit_target is None
    assert result.vision_parts == []


@pytest.mark.asyncio
async def test_reply_image_already_in_history_is_not_re_added_but_is_edit_target(tmp_path):
    payload = b"DUPIMG"
    referenced = _FakeMessage(msg_id=2, attachments=[_FakeAttachment(payload)])
    channel = _FakeChannel(channel_id=10)
    ref = _FakeRef(resolved=referenced, channel_id=10)
    msg = _FakeMessage(attachments=[], reference=ref, channel=channel)
    result = await collect_turn_images(
        msg,
        store=_store(tmp_path),
        conversation_key="k",
        detail="auto",
        images_supported=True,
        history_hashes={hashlib.sha256(_PNG_HEADER + payload).hexdigest()},
        lookback=10,
        max_images=4,
    )
    assert result.vision_parts == []  # deduped against history
    assert result.edit_target is not None  # still materialized for editing


@pytest.mark.asyncio
async def test_non_vision_provider_keeps_only_current_images(tmp_path):
    referenced = _FakeMessage(msg_id=2, attachments=[_FakeAttachment(b"REPLYIMG")])
    ref = _FakeRef(resolved=referenced, channel_id=10)
    msg = _FakeMessage(
        attachments=[_FakeAttachment(b"CURRENT")],
        reference=ref,
        channel=_FakeChannel(channel_id=10),
    )
    result = await collect_turn_images(
        msg,
        store=_store(tmp_path),
        conversation_key="k",
        detail="auto",
        images_supported=False,
        history_hashes=set(),
        lookback=10,
        max_images=4,
    )
    assert len(result.vision_parts) == 1  # only current; reply not added
    assert result.edit_target is not None  # reply still the edit target


@pytest.mark.asyncio
async def test_cross_channel_reference_yields_no_reply_image(tmp_path):
    referenced = _FakeMessage(msg_id=2, attachments=[_FakeAttachment(b"OTHER")])
    ref = _FakeRef(resolved=referenced, channel_id=999)  # different channel
    msg = _FakeMessage(attachments=[], reference=ref, channel=_FakeChannel(channel_id=10))
    result = await collect_turn_images(
        msg,
        store=_store(tmp_path),
        conversation_key="k",
        detail="auto",
        images_supported=True,
        history_hashes=set(),
        lookback=10,
        max_images=4,
    )
    assert result.vision_parts == []
    assert result.edit_target is None


@pytest.mark.asyncio
async def test_deleted_reference_without_attachments_does_not_raise(tmp_path):
    class _Deleted:  # like discord.DeletedReferencedMessage: no .attachments
        pass

    ref = _FakeRef(resolved=_Deleted(), message_id=None, channel_id=10)
    msg = _FakeMessage(attachments=[], reference=ref, channel=_FakeChannel(channel_id=10))
    result = await collect_turn_images(
        msg,
        store=_store(tmp_path),
        conversation_key="k",
        detail="auto",
        images_supported=True,
        history_hashes=set(),
        lookback=10,
        max_images=4,
    )
    assert result.vision_parts == []
    assert result.edit_target is None


@pytest.mark.asyncio
async def test_history_called_with_before_message(tmp_path):
    calls = {}
    older = _FakeMessage(msg_id=5, attachments=[_FakeAttachment(b"HISTIMG")])

    class _RecordingChannel(_FakeChannel):
        def history(self, *, limit, before):
            calls["limit"] = limit
            calls["before"] = before
            return super().history(limit=limit, before=before)

    channel = _RecordingChannel(channel_id=10, history_messages=[older])
    msg = _FakeMessage(attachments=[], reference=None, channel=channel)
    await collect_turn_images(
        msg,
        store=_store(tmp_path),
        conversation_key="k",
        detail="auto",
        images_supported=True,
        history_hashes=set(),
        lookback=7,
        max_images=4,
    )
    assert calls["limit"] == 7
    assert calls["before"] is msg


@pytest.mark.asyncio
async def test_max_images_caps_added_reply_and_newest(tmp_path):
    # Reply has an image; history also has a (different) image; cap=1 -> only one added.
    referenced = _FakeMessage(msg_id=2, attachments=[_FakeAttachment(b"REPLYIMG")])
    older = _FakeMessage(msg_id=5, attachments=[_FakeAttachment(b"HISTIMG")])
    channel = _FakeChannel(channel_id=10, history_messages=[older])
    ref = _FakeRef(resolved=referenced, channel_id=10)
    msg = _FakeMessage(attachments=[], reference=ref, channel=channel)
    result = await collect_turn_images(
        msg,
        store=_store(tmp_path),
        conversation_key="k",
        detail="auto",
        images_supported=True,
        history_hashes=set(),
        lookback=10,
        max_images=1,
    )
    # current is empty, so newest would normally also be eligible; cap limits added to 1.
    assert len(result.vision_parts) == 1


@pytest.mark.asyncio
async def test_non_vision_provider_still_sets_history_edit_target(tmp_path):
    older = _FakeMessage(msg_id=5, attachments=[_FakeAttachment(b"HISTIMG")])
    channel = _FakeChannel(channel_id=10, history_messages=[older])
    msg = _FakeMessage(attachments=[], reference=None, channel=channel)
    result = await collect_turn_images(
        msg,
        store=_store(tmp_path),
        conversation_key="k",
        detail="auto",
        images_supported=False,
        history_hashes=set(),
        lookback=10,
        max_images=4,
    )
    assert result.vision_parts == []  # no current image, vision additions gated off
    assert result.edit_target is not None  # newest-from-history still materialized as edit target


@pytest.mark.asyncio
async def test_reply_adds_only_primary_image_to_vision(tmp_path):
    referenced = _FakeMessage(
        msg_id=2,
        attachments=[_FakeAttachment(b"IMG1"), _FakeAttachment(b"IMG2")],
    )
    ref = _FakeRef(resolved=referenced, channel_id=10)
    msg = _FakeMessage(attachments=[], reference=ref, channel=_FakeChannel(channel_id=10))
    result = await collect_turn_images(
        msg,
        store=_store(tmp_path),
        conversation_key="k",
        detail="auto",
        images_supported=True,
        history_hashes=set(),
        lookback=10,
        max_images=4,
    )
    assert len(result.vision_parts) == 1  # primary only, not both
    assert result.edit_target is not None


@pytest.mark.asyncio
async def test_reference_without_channel_id_yields_no_reply_image(tmp_path):
    referenced = _FakeMessage(msg_id=2, attachments=[_FakeAttachment(b"IMG")])
    ref = _FakeRef(resolved=referenced, channel_id=None)  # cannot confirm same channel
    msg = _FakeMessage(attachments=[], reference=ref, channel=_FakeChannel(channel_id=10))
    result = await collect_turn_images(
        msg,
        store=_store(tmp_path),
        conversation_key="k",
        detail="auto",
        images_supported=True,
        history_hashes=set(),
        lookback=10,
        max_images=4,
    )
    assert result.vision_parts == []
    assert result.edit_target is None


class _AttSrc:
    def __init__(self, *, filename: str, content_type: str | None, payload: bytes) -> None:
        self.filename = filename
        self.content_type = content_type
        self.size = len(payload)
        self._payload = payload

    async def read(self) -> bytes:
        return self._payload


def _att_message(attachments: list[object]) -> SimpleNamespace:
    return SimpleNamespace(attachments=attachments)


def test_collect_turn_attachments_skips_images() -> None:
    msg = _att_message(
        [
            _AttSrc(filename="pic.png", content_type="image/png", payload=b"x"),
            _AttSrc(filename="data.zip", content_type="application/zip", payload=b"zip"),
        ]
    )
    refs = collect_turn_attachments(msg)
    assert [r.filename for r in refs] == ["data.zip"]
    assert refs[0].size == 3
    assert refs[0].content_type == "application/zip"


def test_collect_turn_attachments_keeps_unvalidated_image_filename_candidate() -> None:
    # This synchronous collector cannot sniff the bytes. Turn preparation
    # removes photo.png after the vision collector validates it successfully.
    msg = _att_message(
        [
            _AttSrc(filename="photo.png", content_type=None, payload=b"x"),
            _AttSrc(filename="notes.bin", content_type=None, payload=b"data"),
        ]
    )
    refs = collect_turn_attachments(msg)
    assert [r.filename for r in refs] == ["photo.png", "notes.bin"]


def test_collect_turn_attachments_keeps_image_filename_with_generic_content_type() -> None:
    """A generic-typed file with an image name is only a vision candidate. If
    its bytes are not an image, this ref is the only way it stays reachable."""

    msg = _att_message(
        [
            _AttSrc(
                filename="photo.png",
                content_type="application/octet-stream",
                payload=b"x",
            ),
            _AttSrc(filename="notes.bin", content_type=None, payload=b"data"),
        ]
    )
    refs = collect_turn_attachments(msg)
    assert [r.filename for r in refs] == ["photo.png", "notes.bin"]


@pytest.mark.asyncio
async def test_attachment_ref_reads_through_source() -> None:
    src = _AttSrc(filename="a.txt", content_type="text/plain", payload=b"hello")
    [ref] = collect_turn_attachments(_att_message([src]))
    assert await ref.read() == b"hello"


def test_collect_turn_attachments_exposes_narrow_video_stream() -> None:
    source = FakeAttachment(
        filename="clip.mp4",
        content_type="video/mp4",
        payload=b"video",
    )

    [ref] = collect_turn_attachments(_att_message([source]))

    assert ref.video_stream_url == source.url
    assert "video tool" in format_attachments_context([ref])


@pytest.mark.asyncio
async def test_video_stream_reads_discord_source_in_bounded_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Content:
        async def iter_chunked(self, chunk_size: int):
            assert chunk_size == 2
            yield b"ab"
            yield b"cde"

    class Response:
        status = 200
        content_length = 5
        content = Content()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        def get(self, url: str, *, allow_redirects: bool):
            assert url.startswith("https://cdn.discordapp.com/attachments/")
            assert allow_redirects is False
            return Response()

    monkeypatch.setattr(
        attachments_module.aiohttp,
        "ClientSession",
        lambda **kwargs: Session(),
    )
    ref = AttachmentRef(
        filename="clip.mp4",
        size=5,
        content_type="video/mp4",
        source=None,
        video_stream_url="https://cdn.discordapp.com/attachments/1/2/clip.mp4",
    )

    chunks = [chunk async for chunk in ref.iter_video_chunks(chunk_size=2, max_bytes=10)]

    assert chunks == [b"ab", b"cde"]


@pytest.mark.asyncio
async def test_video_stream_rejects_non_discord_source_before_network() -> None:
    ref = AttachmentRef(
        filename="clip.mp4",
        size=5,
        content_type="video/mp4",
        source=None,
        video_stream_url="https://example.com/clip.mp4",
    )

    with pytest.raises(ValueError, match="safe Discord"):
        async for _chunk in ref.iter_video_chunks(chunk_size=2, max_bytes=10):
            pass


def test_format_attachments_context_empty() -> None:
    assert format_attachments_context([]) == ""


def test_format_attachments_context_lists_names() -> None:
    refs = [
        AttachmentRef(
            filename="data.zip", size=4 * 1024 * 1024, content_type="application/zip", source=None
        ),
        AttachmentRef(filename="notes.txt", size=2048, content_type="text/plain", source=None),
    ]
    text = format_attachments_context(refs)
    assert "import_attachment" in text
    assert "data.zip" in text
    assert "notes.txt" in text


def test_format_attachments_context_sanitizes_newlines() -> None:
    refs = [AttachmentRef(filename="a\nb\rc.txt", size=1, content_type=None, source=None)]
    text = format_attachments_context(refs)
    assert "\n" not in text.split(":", 1)[1]  # no raw newline in the listed filenames


@pytest.mark.asyncio
async def test_turn_has_image_input_true_when_trigger_has_image() -> None:
    msg = _FakeMessage(attachments=[_FakeAttachment(b"IMG")])
    assert await turn_has_image_input(msg) is True


@pytest.mark.asyncio
async def test_turn_has_image_input_true_when_reply_target_has_image() -> None:
    referenced = _FakeMessage(msg_id=2, attachments=[_FakeAttachment(b"REPLYIMG")])
    ref = _FakeRef(resolved=referenced, channel_id=10)
    channel = _FakeChannel(channel_id=10)
    msg = _FakeMessage(attachments=[], reference=ref, channel=channel)
    assert await turn_has_image_input(msg, bot_user=SimpleNamespace(id=999)) is True


@pytest.mark.asyncio
async def test_turn_has_image_input_false_when_no_images() -> None:
    msg = _FakeMessage(attachments=[])
    assert await turn_has_image_input(msg) is False


@pytest.mark.asyncio
async def test_turn_has_image_input_false_when_no_reference() -> None:
    msg = _FakeMessage(attachments=[], reference=None)
    assert await turn_has_image_input(msg) is False


@pytest.mark.asyncio
async def test_turn_has_image_input_ignores_bot_reply_target() -> None:
    # A bot-authored referenced message is excluded (mirrors reply-context
    # collection), so it does not trigger image routing.
    referenced = _FakeMessage(msg_id=2, attachments=[_FakeAttachment(b"BOTIMG")], author_bot=True)
    ref = _FakeRef(resolved=referenced, channel_id=10)
    channel = _FakeChannel(channel_id=10)
    msg = _FakeMessage(attachments=[], reference=ref, channel=channel)
    assert await turn_has_image_input(msg, bot_user=SimpleNamespace(id=999)) is False


@pytest.mark.asyncio
async def test_turn_has_image_input_allows_current_bot_reply_when_explicit() -> None:
    referenced = _FakeMessage(
        msg_id=2,
        attachments=[_FakeAttachment(b"BOTIMG")],
        author_id=999,
        author_bot=True,
    )
    ref = _FakeRef(resolved=referenced, channel_id=10)
    channel = _FakeChannel(channel_id=10)
    msg = _FakeMessage(attachments=[], reference=ref, channel=channel)

    assert (
        await turn_has_image_input(
            msg,
            bot_user=SimpleNamespace(id=999),
            allow_bot_authored=True,
        )
        is True
    )


@pytest.mark.asyncio
async def test_turn_has_image_input_ignores_cross_channel_reference() -> None:
    referenced = _FakeMessage(msg_id=2, attachments=[_FakeAttachment(b"OTHER")])
    # Reference points at a different channel than the trigger's channel.
    ref = _FakeRef(resolved=referenced, channel_id=999)
    channel = _FakeChannel(channel_id=10)
    msg = _FakeMessage(attachments=[], reference=ref, channel=channel)
    assert await turn_has_image_input(msg, bot_user=SimpleNamespace(id=999)) is False


@pytest.mark.asyncio
async def test_collect_turn_images_skips_oversized_declared_image_without_refusing_the_turn(
    tmp_path: Path,
) -> None:
    """An image above the attachment cap is never read, so it must not flag the
    turn as unable to read the image; the user's text still gets answered."""

    store = AttachmentStore(base_dir=tmp_path, max_bytes=16)
    message = SimpleNamespace(
        id=56,
        attachments=[
            FakeAttachment(
                filename="huge.png",
                content_type="image/png",
                payload=b"x" * 64,
            )
        ],
    )

    result = await collect_turn_images(
        message,
        store=store,
        conversation_key="guild:chan",
        detail="auto",
        images_supported=True,
        history_hashes=set(),
        lookback=0,
        max_images=1,
    )

    assert result.vision_parts == []
    assert result.current_image_unavailable is False
