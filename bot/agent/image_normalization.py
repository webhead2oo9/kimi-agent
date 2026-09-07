"""Killable, resource-bounded normalization for untrusted vision images."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any


MAX_DECODED_PIXELS = 64_000_000
NORMALIZE_LONGEST_EDGE = 4096
TARGET_LONGEST_EDGE = 2560
TARGET_ENCODED_BYTES = 4 * 1024 * 1024
_WORKER_MEMORY_BYTES = 1536 * 1024 * 1024
_WORKER_PATH = os.path.realpath(__file__)
_FORMAT_MEDIA_TYPES = {
    "GIF": "image/gif",
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
}


@dataclass(frozen=True, slots=True)
class ImageNormalizationResult:
    source_dimensions: tuple[int, int]
    processed_dimensions: tuple[int, int]
    media_type: str
    normalized: bool
    animation_first_frame: bool = False
    output_path: Path | None = None


class ImageNormalizationError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


async def normalize_image_file(
    source_path: Path,
    output_path: Path,
    *,
    source_size: int,
    processed_max_bytes: int,
    timeout_seconds: float,
    semaphore: asyncio.Semaphore,
) -> ImageNormalizationResult:
    """Validate an image and, when required, return a normalized derivative.

    Pillow runs in a fresh subprocess. A timeout or cancellation kills that
    process, unlike a worker thread whose decoder would continue consuming
    memory after its turn had ended.
    """

    args = [
        sys.executable,
        _WORKER_PATH,
        "--worker",
        os.fspath(source_path),
        os.fspath(output_path),
        str(source_size),
        str(processed_max_bytes),
        str(max(1, math.ceil(timeout_seconds))),
    ]
    worker_temporary: Path | None = None
    try:
        async with semaphore:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={"PATH": os.defpath, "PYTHONIOENCODING": "utf-8"},
            )
            worker_temporary = output_path.with_name(f".{output_path.name}.{process.pid}.tmp")
            try:
                stdout, _stderr = await asyncio.wait_for(
                    process.communicate(), timeout=timeout_seconds
                )
            except BaseException:
                if process.returncode is None:
                    process.kill()
                    await _wait_for_killed_process(process)
                raise
            finally:
                await asyncio.to_thread(_unlink_if_present, worker_temporary)
        if process.returncode != 0:
            try:
                error = json.loads(stdout.decode("utf-8"))
                code = str(error.get("error", "processing_failed"))
            except UnicodeDecodeError, json.JSONDecodeError, AttributeError:
                code = "processing_failed"
            raise ImageNormalizationError(code)
        try:
            data = json.loads(stdout.decode("utf-8"))
            normalized = bool(data["normalized"])
            result = ImageNormalizationResult(
                source_dimensions=(int(data["source_width"]), int(data["source_height"])),
                processed_dimensions=(
                    int(data["processed_width"]),
                    int(data["processed_height"]),
                ),
                media_type=str(data["media_type"]),
                normalized=normalized,
                animation_first_frame=bool(data.get("animation_first_frame", False)),
                output_path=output_path if normalized else None,
            )
        except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ImageNormalizationError("processing_failed") from exc
        if normalized:
            try:
                output_size = await asyncio.to_thread(_file_size, output_path)
            except OSError as exc:
                raise ImageNormalizationError("processing_failed") from exc
            if output_size <= 0 or output_size > min(TARGET_ENCODED_BYTES, processed_max_bytes):
                raise ImageNormalizationError("processed_too_large")
        elif await asyncio.to_thread(output_path.exists):
            await asyncio.to_thread(_unlink_if_present, output_path)
        return result
    except BaseException:
        paths = (output_path,) if worker_temporary is None else (output_path, worker_temporary)
        await asyncio.to_thread(_unlink_paths, paths)
        raise


def _unlink_if_present(path: Path) -> None:
    path.unlink(missing_ok=True)


def _unlink_paths(paths: tuple[Path, ...]) -> None:
    for path in paths:
        path.unlink(missing_ok=True)


def _file_size(path: Path) -> int:
    return path.stat().st_size


async def _wait_for_killed_process(process: asyncio.subprocess.Process) -> None:
    wait_task = asyncio.create_task(process.wait())
    while not wait_task.done():
        try:
            await asyncio.shield(wait_task)
        except asyncio.CancelledError:
            continue
    wait_task.result()


def _set_worker_limits(cpu_seconds: int) -> None:
    if os.name != "posix":
        return
    import resource

    resource.setrlimit(resource.RLIMIT_AS, (_WORKER_MEMORY_BYTES, _WORKER_MEMORY_BYTES))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 1))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (TARGET_ENCODED_BYTES, TARGET_ENCODED_BYTES))


def _has_transparency(image: Any) -> bool:
    return "A" in image.getbands() or "transparency" in image.info


def _resize_to_longest_edge(image: Any, longest_edge: int) -> Any:
    from PIL import Image

    current = max(image.size)
    if current <= longest_edge:
        return image
    scale = longest_edge / current
    size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
    return image.resize(size, Image.Resampling.LANCZOS)


def _encoded(image: Any, image_format: str, **kwargs: Any) -> bytes:
    output = BytesIO()
    image.save(output, format=image_format, **kwargs)
    return output.getvalue()


def _encode_bounded(
    image: Any,
    *,
    transparency: bool,
    prefer_lossless: bool,
    target: int,
) -> tuple[bytes, str, tuple[int, int]]:
    """Try a fixed quality/dimension ladder, favoring crisp lossless output."""

    working = image
    for dimension_round in range(3):
        if prefer_lossless:
            payload = _encoded(working, "PNG", optimize=True, compress_level=9)
            if len(payload) <= target:
                return payload, "image/png", working.size
            payload = _encoded(working, "WEBP", lossless=True, method=6)
            if len(payload) <= target:
                return payload, "image/webp", working.size
        qualities = (92, 82, 70) if transparency else (92, 84, 74)
        for quality in qualities:
            if transparency:
                payload = _encoded(working, "WEBP", quality=quality, method=6)
                media_type = "image/webp"
            else:
                rgb = working.convert("RGB")
                payload = _encoded(
                    rgb,
                    "JPEG",
                    quality=quality,
                    optimize=True,
                    progressive=True,
                    subsampling=0 if quality >= 90 else 2,
                )
                media_type = "image/jpeg"
            if len(payload) <= target:
                return payload, media_type, working.size
        if dimension_round < 2:
            working = _resize_to_longest_edge(working, max(640, round(max(working.size) * 0.8)))
    raise ImageNormalizationError("processed_too_large")


def _normalize_sync(
    source_path: Path,
    output_path: Path,
    source_size: int,
    processed_max_bytes: int,
) -> dict[str, object]:
    from PIL import Image, ImageOps

    # We enforce the exact pixel ceiling ourselves before load(). Disabling
    # Pillow's global heuristic avoids rejecting legal 64 MP images under a
    # library-version-dependent warning threshold.
    Image.MAX_IMAGE_PIXELS = None
    try:
        with Image.open(source_path) as opened:
            media_type = _FORMAT_MEDIA_TYPES.get(opened.format or "")
            if media_type is None:
                raise ImageNormalizationError("malformed")
            source_dimensions = tuple(opened.size)
            width, height = source_dimensions
            if width <= 0 or height <= 0:
                raise ImageNormalizationError("malformed")
            if width * height > MAX_DECODED_PIXELS:
                raise ImageNormalizationError("pixel_limit")
            orientation = int(opened.getexif().get(274, 1) or 1)
            animation = bool(getattr(opened, "is_animated", False))
            opened.seek(0)
            opened.load()
            frame = ImageOps.exif_transpose(opened)
            if _has_transparency(opened):
                frame = frame.convert("RGBA")
            elif frame.mode not in {"RGB", "L", "P"}:
                frame = frame.convert("RGB")
            else:
                frame = frame.copy()
    except ImageNormalizationError:
        raise
    except Exception as exc:
        raise ImageNormalizationError("malformed") from exc

    needs_size_normalization = (
        source_size > processed_max_bytes or max(source_dimensions) > NORMALIZE_LONGEST_EDGE
    )
    normalized = needs_size_normalization or orientation not in {0, 1} or animation
    if not normalized:
        return {
            "source_width": source_dimensions[0],
            "source_height": source_dimensions[1],
            "processed_width": source_dimensions[0],
            "processed_height": source_dimensions[1],
            "media_type": media_type,
            "normalized": False,
            "animation_first_frame": False,
        }
    if needs_size_normalization:
        frame = _resize_to_longest_edge(frame, TARGET_LONGEST_EDGE)
    transparency = _has_transparency(frame)
    prefer_lossless = transparency or media_type in {"image/png", "image/gif"}
    payload, output_media_type, processed_dimensions = _encode_bounded(
        frame,
        transparency=transparency,
        prefer_lossless=prefer_lossless,
        target=min(TARGET_ENCODED_BYTES, processed_max_bytes),
    )
    if len(payload) > min(TARGET_ENCODED_BYTES, processed_max_bytes):
        raise ImageNormalizationError("processed_too_large")
    temporary = output_path.with_name(f".{output_path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("xb") as file:
            file.write(payload)
        os.chmod(temporary, 0o600)
        os.replace(temporary, output_path)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "source_width": source_dimensions[0],
        "source_height": source_dimensions[1],
        "processed_width": processed_dimensions[0],
        "processed_height": processed_dimensions[1],
        "media_type": output_media_type,
        "normalized": True,
        "animation_first_frame": animation,
    }


def _worker(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("source")
    parser.add_argument("output")
    parser.add_argument("source_size", type=int)
    parser.add_argument("processed_max_bytes", type=int)
    parser.add_argument("cpu_seconds", type=int)
    args = parser.parse_args(argv)
    try:
        _set_worker_limits(args.cpu_seconds)
        result = _normalize_sync(
            Path(args.source),
            Path(args.output),
            args.source_size,
            args.processed_max_bytes,
        )
    except ImageNormalizationError as exc:
        print(json.dumps({"error": exc.code}, separators=(",", ":")))
        return 2
    except BaseException:
        print(json.dumps({"error": "processing_failed"}, separators=(",", ":")))
        return 2
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(_worker(sys.argv[1:]))
