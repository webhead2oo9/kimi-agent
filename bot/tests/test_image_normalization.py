from __future__ import annotations

import asyncio
import base64
import json
import threading
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image

from agent.attachments import AttachmentStore, cleanup_attachment_paths, collect_turn_images
from agent.image_normalization import ImageNormalizationResult
from agent import image_normalization as normalization_module


def _image_bytes(
    image_format: str,
    size: tuple[int, int],
    *,
    mode: str = "RGB",
    color: Any = (32, 96, 160),
    orientation: int | None = None,
) -> bytes:
    image = Image.new(mode, size, color)
    output = BytesIO()
    kwargs: dict[str, object] = {}
    if orientation is not None:
        exif = Image.Exif()
        exif[274] = orientation
        kwargs["exif"] = exif
    image.save(output, format=image_format, **kwargs)
    return output.getvalue()


def _animated_gif() -> bytes:
    output = BytesIO()
    first = Image.new("RGB", (24, 16), "red")
    second = Image.new("RGB", (24, 16), "blue")
    first.save(output, format="GIF", save_all=True, append_images=[second], duration=50, loop=0)
    return output.getvalue()


class StreamingAttachment:
    def __init__(
        self,
        payload: bytes,
        *,
        filename: str = "image.png",
        content_type: str | None = "image/png",
        declared_size: int | None = None,
        chunk_size: int = 64 * 1024,
    ) -> None:
        self._payload = payload
        self.filename = filename
        self.content_type = content_type
        self.size = len(payload) if declared_size is None else declared_size
        self.chunk_size = chunk_size
        self.read_called = False

    async def iter_chunks(self):
        for offset in range(0, len(self._payload), self.chunk_size):
            await asyncio.sleep(0)
            yield self._payload[offset : offset + self.chunk_size]

    async def read(self) -> bytes:
        self.read_called = True
        raise AssertionError("the image collector must stream instead of calling read()")


def _message(*attachments: object, message_id: int = 1) -> SimpleNamespace:
    return SimpleNamespace(
        id=message_id,
        attachments=list(attachments),
        reference=None,
        channel=SimpleNamespace(id=10),
        author=SimpleNamespace(id=20, bot=False, display_name="Alice"),
    )


class _Channel:
    def __init__(self, history_messages: list[object] | None = None) -> None:
        self.id = 10
        self._history = history_messages or []

    def history(self, *, limit: int, before: object):
        del before

        async def generate():
            for item in self._history[:limit]:
                yield item

        return generate()


async def _collect(
    tmp_path: Path,
    *attachments: object,
    processed_cap: int = 8 * 1024 * 1024,
    source_cap: int = 32 * 1024 * 1024,
    aggregate_cap: int = 32 * 1024 * 1024,
):
    store = AttachmentStore(
        base_dir=tmp_path,
        max_bytes=processed_cap,
        source_max_bytes=source_cap,
        max_total_bytes=aggregate_cap,
        normalization_timeout_seconds=10,
        normalization_max_concurrency=2,
    )
    return await collect_turn_images(
        _message(*attachments),
        store=store,
        conversation_key="guild:channel",
        detail="auto",
        images_supported=True,
        history_hashes=set(),
        lookback=0,
        max_images=10,
    )


def _part_payload(result) -> bytes:
    url = result.vision_parts[0].image_url
    assert url is not None
    return base64.b64decode(url.partition(",")[2], validate=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("source_size", [9_001_473, 12_551_938])
async def test_large_source_sizes_are_normalized_below_processed_target(
    tmp_path: Path, source_size: int
) -> None:
    payload = _image_bytes("PNG", (1200, 800))
    payload += b"\0" * (source_size - len(payload))

    result = await _collect(tmp_path, StreamingAttachment(payload))

    processed = _part_payload(result)
    assert len(processed) <= 4 * 1024 * 1024
    assert result.current_images[0].source_dimensions == (1200, 800)
    assert result.current_images[0].processed_dimensions == (1200, 800)
    assert result.current_images[0].normalized is True
    assert "1200x800 -> 1200x800" in result.normalization_notice
    assert result.current_images[0].part.media_type == "image/png"
    assert result.current_images[0].filename not in result.normalization_notice
    assert result.current_images[0].cleanup_path.exists()
    assert result.current_images[0].derivative_path is not None
    assert result.current_images[0].derivative_path.exists()
    await cleanup_attachment_paths(result.cleanup_paths)
    assert not list(tmp_path.rglob("*.*"))


@pytest.mark.asyncio
async def test_over_4k_small_image_downscales_to_2560(tmp_path: Path) -> None:
    payload = _image_bytes("PNG", (5000, 500), color=(1, 2, 3))

    result = await _collect(tmp_path, StreamingAttachment(payload))

    assert result.current_images[0].processed_dimensions == (2560, 256)
    with Image.open(BytesIO(_part_payload(result))) as image:
        assert image.size == (2560, 256)


@pytest.mark.asyncio
async def test_underlimit_image_passes_through_byte_for_byte(tmp_path: Path) -> None:
    payload = _image_bytes("JPEG", (320, 200))

    result = await _collect(
        tmp_path,
        StreamingAttachment(payload, filename="ordinary.jpg", content_type="image/jpeg"),
    )

    assert _part_payload(result) == payload
    assert result.current_images[0].normalized is False
    assert result.normalization_notice == ""


@pytest.mark.asyncio
async def test_exif_orientation_is_applied_and_stripped(tmp_path: Path) -> None:
    payload = _image_bytes("JPEG", (40, 20), orientation=6)

    result = await _collect(
        tmp_path,
        StreamingAttachment(payload, filename="rotated.jpg", content_type="image/jpeg"),
    )

    image = Image.open(BytesIO(_part_payload(result)))
    assert image.size == (20, 40)
    assert image.getexif().get(274, 1) == 1
    assert result.current_images[0].source_dimensions == (40, 20)
    assert result.current_images[0].processed_dimensions == (20, 40)


@pytest.mark.asyncio
async def test_transparency_is_preserved(tmp_path: Path) -> None:
    payload = _image_bytes("PNG", (4200, 420), mode="RGBA", color=(20, 40, 60, 0))

    result = await _collect(tmp_path, StreamingAttachment(payload))

    with Image.open(BytesIO(_part_payload(result))) as image:
        assert "A" in image.getbands()
        pixel = image.getpixel((0, 0))
        assert isinstance(pixel, tuple)
        assert pixel[3] == 0
    assert result.current_images[0].part.media_type in {"image/png", "image/webp"}


@pytest.mark.asyncio
async def test_malformed_image_is_explicitly_unavailable(tmp_path: Path) -> None:
    result = await _collect(tmp_path, StreamingAttachment(b"\x89PNG\r\n\x1a\nnot-an-image"))

    assert result.vision_parts == []
    assert result.current_image_unavailable is True
    assert "could not be decoded" in result.user_feedback
    assert not list(tmp_path.rglob("*.*"))


@pytest.mark.asyncio
async def test_decoded_pixel_ceiling_rejects_decompression_bomb(tmp_path: Path) -> None:
    payload = _image_bytes("PNG", (8193, 8192), color=(0, 0, 0))

    result = await _collect(tmp_path, StreamingAttachment(payload), source_cap=len(payload) + 1)

    assert result.vision_parts == []
    assert result.current_image_unavailable is True
    assert "64 megapixel" in result.user_feedback


@pytest.mark.asyncio
async def test_animation_uses_first_frame_and_reports_policy(tmp_path: Path) -> None:
    result = await _collect(
        tmp_path,
        StreamingAttachment(_animated_gif(), filename="moving.gif", content_type="image/gif"),
    )

    with Image.open(BytesIO(_part_payload(result))) as image:
        assert getattr(image, "n_frames", 1) == 1
        pixel = image.convert("RGB").getpixel((0, 0))
        assert isinstance(pixel, tuple)
        assert pixel[0] > 200
    assert result.current_images[0].animation_first_frame is True
    assert "first frame" in result.normalization_notice


@pytest.mark.asyncio
async def test_streamed_overflow_is_stopped_without_attachment_read(tmp_path: Path) -> None:
    attachment = StreamingAttachment(
        _image_bytes("PNG", (64, 64)) + b"x" * 2048,
        declared_size=16,
        chunk_size=128,
    )

    result = await _collect(tmp_path, attachment, source_cap=1024, aggregate_cap=1024)

    assert attachment.read_called is False
    assert result.vision_parts == []
    assert "source download limit" in result.user_feedback
    assert not list(tmp_path.rglob("*.*"))


@pytest.mark.asyncio
async def test_aggregate_source_budget_returns_partial_batch_with_feedback(tmp_path: Path) -> None:
    first_payload = _image_bytes("PNG", (32, 32), color="green")
    second_payload = _image_bytes("PNG", (32, 32), color="blue")
    aggregate = len(first_payload) + len(second_payload) - 1
    first = StreamingAttachment(first_payload, filename="first.png")
    second = StreamingAttachment(second_payload, filename="second.png")

    result = await _collect(tmp_path, first, second, aggregate_cap=aggregate)

    assert len(result.vision_parts) == 1
    assert "aggregate source download budget" in result.user_feedback
    assert second.read_called is False
    assert first.filename not in result.user_feedback
    assert second.filename not in result.user_feedback


@pytest.mark.asyncio
async def test_processing_failure_returns_other_images_with_feedback(tmp_path: Path) -> None:
    valid = StreamingAttachment(_image_bytes("PNG", (32, 32)), filename="valid.png")
    malformed = StreamingAttachment(b"\x89PNG\r\n\x1a\ninvalid", filename="invalid.png")

    result = await _collect(tmp_path, valid, malformed)

    assert len(result.vision_parts) == 1
    assert result.current_image_unavailable is False
    assert "could not be decoded safely" in result.user_feedback
    assert valid.filename not in result.user_feedback
    assert malformed.filename not in result.user_feedback


@pytest.mark.asyncio
async def test_cancellation_cleans_source_and_derivative_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = asyncio.Event()

    async def blocked_normalize(*args, **kwargs):
        started.set()
        await asyncio.Future()

    monkeypatch.setattr("agent.attachments.normalize_image_file", blocked_normalize)
    task = asyncio.create_task(
        _collect(tmp_path, StreamingAttachment(_image_bytes("PNG", (5000, 500))))
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not list(tmp_path.rglob("*.*"))


@pytest.mark.asyncio
async def test_normalizer_cancellation_after_worker_success_cleans_derivative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.png"
    output = tmp_path / "normalized.png"
    source.write_bytes(_image_bytes("PNG", (32, 32)))
    post_worker_cleanup = asyncio.Event()

    class Process:
        pid = 123
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            output.write_bytes(_image_bytes("PNG", (16, 16)))
            return (
                json.dumps(
                    {
                        "source_width": 32,
                        "source_height": 32,
                        "processed_width": 16,
                        "processed_height": 16,
                        "media_type": "image/png",
                        "normalized": True,
                    }
                ).encode(),
                b"",
            )

    async def create_process(*args: object, **kwargs: object) -> Process:
        del args, kwargs
        return Process()

    original_to_thread = normalization_module.asyncio.to_thread
    held_once = False

    async def hold_first_post_worker_await(function, *args):
        nonlocal held_once
        if function is normalization_module._unlink_if_present and not held_once:
            held_once = True
            post_worker_cleanup.set()
            await asyncio.Future()
        return await original_to_thread(function, *args)

    monkeypatch.setattr(normalization_module.asyncio, "create_subprocess_exec", create_process)
    monkeypatch.setattr(normalization_module.asyncio, "to_thread", hold_first_post_worker_await)
    task = asyncio.create_task(
        normalization_module.normalize_image_file(
            source,
            output,
            source_size=source.stat().st_size,
            processed_max_bytes=1024 * 1024,
            timeout_seconds=10,
            semaphore=asyncio.Semaphore(1),
        )
    )

    await asyncio.wait_for(post_worker_cleanup.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not output.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("postprocessor", ["read", "payload"])
async def test_cancellation_during_collector_postprocessing_cleans_derivative(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    postprocessor: str,
) -> None:
    entered = threading.Event()
    release = threading.Event()

    async def normalized(source_path: Path, output_path: Path, **kwargs: object):
        del source_path, kwargs
        output_path.write_bytes(_image_bytes("PNG", (16, 16)))
        return ImageNormalizationResult(
            source_dimensions=(32, 32),
            processed_dimensions=(16, 16),
            media_type="image/png",
            normalized=True,
            output_path=output_path,
        )

    monkeypatch.setattr("agent.attachments.normalize_image_file", normalized)
    if postprocessor == "read":
        original = __import__("agent.attachments", fromlist=["_read_bounded_file_sync"])
        original_function = original._read_bounded_file_sync

        def blocked_read(path: Path, max_bytes: int) -> bytes:
            entered.set()
            release.wait(timeout=2)
            return original_function(path, max_bytes)

        monkeypatch.setattr("agent.attachments._read_bounded_file_sync", blocked_read)
    else:
        original = __import__("agent.attachments", fromlist=["_prepare_image_payload"])
        original_function = original._prepare_image_payload

        def blocked_payload(payload: bytes) -> tuple[str | None, str, str]:
            entered.set()
            release.wait(timeout=2)
            return original_function(payload)

        monkeypatch.setattr("agent.attachments._prepare_image_payload", blocked_payload)

    task = asyncio.create_task(
        _collect(tmp_path, StreamingAttachment(_image_bytes("PNG", (32, 32))))
    )
    assert await asyncio.to_thread(entered.wait, 1)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not list(tmp_path.rglob("*.*"))


@pytest.mark.asyncio
async def test_reply_image_uses_same_normalized_pixels_for_vision_and_editing(
    tmp_path: Path,
) -> None:
    attachment = StreamingAttachment(_image_bytes("PNG", (5000, 500)))
    referenced = _message(attachment, message_id=2)
    channel = _Channel()
    trigger = _message(message_id=3)
    trigger.channel = channel
    trigger.reference = SimpleNamespace(channel_id=10, resolved=referenced, message_id=2)
    store = AttachmentStore(base_dir=tmp_path, max_bytes=8 * 1024 * 1024)

    result = await collect_turn_images(
        trigger,
        store=store,
        conversation_key="reply",
        detail="auto",
        images_supported=True,
        history_hashes=set(),
        lookback=10,
        max_images=2,
    )

    assert len(result.vision_parts) == 1
    assert result.edit_target == result.vision_parts[0]
    assert result.reply_images[0].processed_dimensions == (2560, 256)
    assert "reply image 5000x500 -> 2560x256" in result.normalization_notice


@pytest.mark.asyncio
async def test_history_image_is_normalized_before_becoming_edit_target(tmp_path: Path) -> None:
    attachment = StreamingAttachment(_image_bytes("PNG", (5000, 500)))
    history_message = _message(attachment, message_id=2)
    trigger = _message(message_id=3)
    trigger.channel = _Channel([history_message])
    store = AttachmentStore(base_dir=tmp_path, max_bytes=8 * 1024 * 1024)

    result = await collect_turn_images(
        trigger,
        store=store,
        conversation_key="history",
        detail="auto",
        images_supported=True,
        history_hashes=set(),
        lookback=10,
        max_images=2,
    )

    assert result.edit_target == result.vision_parts[0]
    assert "history image 5000x500 -> 2560x256" in result.normalization_notice


@pytest.mark.asyncio
async def test_processing_deadline_failure_cleans_temporary_files(tmp_path: Path) -> None:
    attachment = StreamingAttachment(_image_bytes("PNG", (5000, 500)))
    store = AttachmentStore(
        base_dir=tmp_path,
        max_bytes=8 * 1024 * 1024,
        normalization_timeout_seconds=0.001,
    )

    result = await collect_turn_images(
        _message(attachment),
        store=store,
        conversation_key="timeout",
        detail="auto",
        images_supported=True,
        history_hashes=set(),
        lookback=0,
        max_images=1,
    )

    assert result.current_image_unavailable is True
    assert "normalized safely" in result.user_feedback
    assert not list(tmp_path.rglob("*.*"))
