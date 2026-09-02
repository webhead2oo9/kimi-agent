from __future__ import annotations

import base64
import binascii
from io import BytesIO
import mimetypes
import warnings
import zlib

from PIL import Image

from kimi_agent_module_api.images import (
    SUPPORTED_IMAGE_MEDIA_TYPES,
    looks_like_image_attachment as looks_like_image_attachment,
    sniff_image_media_type as sniff_image_media_type,
)


# Canonical suffix per supported type. Indexing this directly is deliberate: a
# type that gains support without a suffix must fail loudly, not get an
# extension that misrepresents the bytes to whoever opens the file.
IMAGE_MEDIA_TYPE_SUFFIXES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}

_MAX_VALIDATED_IMAGE_PIXELS = 4096 * 4096
# Every frame costs at least this much of the pixel budget, so a tiny-frame
# animation cannot force an unbounded walk while ordinary animated GIFs and
# WebPs (hundreds of frames) still validate.
_MIN_FRAME_PIXEL_CHARGE = 64 * 64
_MAX_VALIDATED_IMAGE_FRAMES = _MAX_VALIDATED_IMAGE_PIXELS // _MIN_FRAME_PIXEL_CHARGE
_PILLOW_FORMAT_MEDIA_TYPES = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "GIF": "image/gif",
    "WEBP": "image/webp",
}


def decoded_image_media_type(payload: bytes) -> str | None:
    """Return the canonical type only after every permitted frame decodes.

    The allocation-free container checks reject obvious truncation before Pillow
    sees the bytes. Pillow then validates the compressed bitstream itself, with
    strict frame and cumulative-pixel budgets to bound CPU and memory use.
    """

    media_type = structurally_valid_image_media_type(payload)
    if media_type is None:
        return None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(payload)) as image:
                if _PILLOW_FORMAT_MEDIA_TYPES.get(image.format or "") != media_type:
                    return None
                decoded_pixels = 0
                decoded_frames = 0
                # Some Pillow plugins implement ``n_frames`` by scanning to EOF.
                # Probe one frame beyond our cap instead, so a tiny-frame GIF
                # cannot force an unbounded pre-validation walk.
                for frame_index in range(_MAX_VALIDATED_IMAGE_FRAMES + 1):
                    try:
                        image.seek(frame_index)
                    except EOFError:
                        break
                    if frame_index == _MAX_VALIDATED_IMAGE_FRAMES:
                        return None
                    width, height = image.size
                    decoded_pixels += max(width * height, _MIN_FRAME_PIXEL_CHARGE)
                    if (
                        not _reasonable_dimensions(width, height)
                        or decoded_pixels > _MAX_VALIDATED_IMAGE_PIXELS
                    ):
                        return None
                    image.load()
                    decoded_frames += 1
                if decoded_frames == 0:
                    return None
    except Exception:
        # Provider bytes are untrusted. Any decoder/plugin error makes the asset
        # ineligible rather than failing the otherwise usable text response.
        return None
    return media_type


def structurally_valid_image_media_type(payload: bytes) -> str | None:
    """Return the sniffed type only when its image container is complete.

    Magic bytes are an inexpensive candidate filter, but a signature alone is
    not an image. Provider-returned assets pass this stricter, allocation-free
    container check before they are sent to output moderation.
    """

    media_type = sniff_image_media_type(payload)
    if media_type is None:
        return None
    validator = _CONTAINER_VALIDATORS.get(media_type)
    if validator is None:
        # The sniffer learned a type this module has no container walk for
        # (SDK drift). Untrusted bytes fail closed rather than raising;
        # tests/test_provider_image_validation.py pins the two sets together.
        return None
    if not validator(payload):
        return None
    return media_type


def _reasonable_dimensions(width: int, height: int) -> bool:
    return width > 0 and height > 0 and width * height <= _MAX_VALIDATED_IMAGE_PIXELS


def _valid_png_container(payload: bytes) -> bool:
    if len(payload) < 45 or not payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return False
    offset = 8
    first = True
    saw_data = False
    while offset + 12 <= len(payload):
        length = int.from_bytes(payload[offset : offset + 4], "big")
        chunk_type = payload[offset + 4 : offset + 8]
        data_start = offset + 8
        data_end = data_start + length
        chunk_end = data_end + 4
        if chunk_end > len(payload):
            return False
        expected_crc = int.from_bytes(payload[data_end:chunk_end], "big")
        if zlib.crc32(payload[data_start:data_end], zlib.crc32(chunk_type)) != expected_crc:
            return False
        if first:
            if chunk_type != b"IHDR" or length != 13:
                return False
            width = int.from_bytes(payload[data_start : data_start + 4], "big")
            height = int.from_bytes(payload[data_start + 4 : data_start + 8], "big")
            bit_depth = payload[data_start + 8]
            color_type = payload[data_start + 9]
            valid_bit_depths = {
                0: {1, 2, 4, 8, 16},
                2: {8, 16},
                3: {1, 2, 4, 8},
                4: {8, 16},
                6: {8, 16},
            }
            if (
                not _reasonable_dimensions(width, height)
                or bit_depth not in valid_bit_depths.get(color_type, set())
                or payload[data_start + 10] != 0
                or payload[data_start + 11] != 0
                or payload[data_start + 12] not in {0, 1}
            ):
                return False
            first = False
        elif chunk_type == b"IHDR":
            return False
        elif chunk_type == b"IDAT":
            saw_data = True
        elif chunk_type == b"IEND":
            # Bytes after the terminator are ignored, as every decoder does;
            # requiring an exact end rejected padded provider output.
            return length == 0 and saw_data
        offset = chunk_end
    return False


# Pillow robustly decodes the ordinary Huffman-coded sequential/progressive
# modes generated by providers. Arithmetic, differential, and lossless modes
# need additional table/scan validation and are rejected rather than allowing
# Pillow's permissive parser to bless a coding-mode mismatch.
_JPEG_FRAME_MARKERS = frozenset({0xC0, 0xC1, 0xC2})


def _valid_jpeg_container(payload: bytes) -> bool:
    if len(payload) < 14 or not payload.startswith(b"\xff\xd8"):
        return False
    offset = 2
    saw_frame = False
    saw_scan = False
    while offset < len(payload):
        if payload[offset] != 0xFF:
            if not saw_scan:
                return False
            # Entropy-coded data: skip to the next marker candidate in C
            # rather than walking bytes in Python.
            next_marker = payload.find(b"\xff", offset)
            if next_marker < 0:
                return False
            offset = next_marker
            continue
        while offset < len(payload) and payload[offset] == 0xFF:
            offset += 1
        if offset >= len(payload):
            return False
        marker = payload[offset]
        offset += 1
        if marker == 0x00:
            if not saw_scan:
                return False
            continue
        if marker == 0xD9:
            return saw_frame and saw_scan
        if marker in range(0xD0, 0xD8) or marker == 0x01:
            continue
        if marker == 0xD8 or offset + 2 > len(payload):
            return False
        segment_length = int.from_bytes(payload[offset : offset + 2], "big")
        segment_end = offset + segment_length
        if segment_length < 2 or segment_end > len(payload):
            return False
        if marker in _JPEG_FRAME_MARKERS:
            if segment_length < 7:
                return False
            height = int.from_bytes(payload[offset + 3 : offset + 5], "big")
            width = int.from_bytes(payload[offset + 5 : offset + 7], "big")
            if not _reasonable_dimensions(width, height):
                return False
            saw_frame = True
        elif marker == 0xDA:
            saw_scan = True
        offset = segment_end
    return False


def _gif_sub_blocks_end(payload: bytes, offset: int) -> int | None:
    while offset < len(payload):
        size = payload[offset]
        offset += 1
        if size == 0:
            return offset
        offset += size
        if offset > len(payload):
            return None
    return None


def _valid_gif_container(payload: bytes) -> bool:
    if len(payload) < 14 or not payload.startswith((b"GIF87a", b"GIF89a")):
        return False
    screen_width = int.from_bytes(payload[6:8], "little")
    screen_height = int.from_bytes(payload[8:10], "little")
    if not _reasonable_dimensions(screen_width, screen_height):
        return False
    offset = 13
    has_global_color_table = bool(payload[10] & 0x80)
    if has_global_color_table:
        offset += 3 * (2 ** ((payload[10] & 0x07) + 1))
    saw_image = False
    while offset < len(payload):
        block_type = payload[offset]
        offset += 1
        if block_type == 0x3B:
            return saw_image
        if block_type == 0x21:
            if offset >= len(payload):
                return False
            offset = _gif_sub_blocks_end(payload, offset + 1) or -1
        elif block_type == 0x2C:
            if offset + 9 > len(payload):
                return False
            left = int.from_bytes(payload[offset : offset + 2], "little")
            top = int.from_bytes(payload[offset + 2 : offset + 4], "little")
            width = int.from_bytes(payload[offset + 4 : offset + 6], "little")
            height = int.from_bytes(payload[offset + 6 : offset + 8], "little")
            if (
                not _reasonable_dimensions(width, height)
                or left + width > screen_width
                or top + height > screen_height
            ):
                return False
            packed = payload[offset + 8]
            if not has_global_color_table and not packed & 0x80:
                return False
            offset += 9
            if packed & 0x80:
                offset += 3 * (2 ** ((packed & 0x07) + 1))
            if offset >= len(payload):
                return False
            if not 2 <= payload[offset] <= 8:
                return False
            offset = _gif_sub_blocks_end(payload, offset + 1) or -1
            saw_image = True
        else:
            return False
        if offset < 0 or offset > len(payload):
            return False
    return False


def _valid_webp_container(payload: bytes) -> bool:
    if len(payload) < 20 or payload[:4] != b"RIFF" or payload[8:12] != b"WEBP":
        return False
    # The RIFF size bounds the container; bytes past it are trailing padding.
    end = 8 + int.from_bytes(payload[4:8], "little")
    if end < 20 or end > len(payload):
        return False
    offset = 12
    saw_image = False
    while offset + 8 <= end:
        chunk_type = payload[offset : offset + 4]
        chunk_size = int.from_bytes(payload[offset + 4 : offset + 8], "little")
        data_start = offset + 8
        data_end = data_start + chunk_size
        if data_end > end:
            return False
        if chunk_type == b"VP8X":
            # The extended header, when present, must lead the chunk sequence.
            # Pillow accepts it after image data even though downstream decoders
            # reject that contradictory second canvas declaration.
            if offset != 12 or not _valid_webp_extended_header(payload[data_start:data_end]):
                return False
        elif chunk_type in {b"VP8 ", b"VP8L", b"ANMF"}:
            if not _valid_webp_image_chunk(chunk_type, payload[data_start:data_end]):
                return False
            saw_image = True
        offset = data_end + (chunk_size & 1)
    return saw_image and offset == end


def _valid_webp_extended_header(data: bytes) -> bool:
    if len(data) != 10 or data[0] & 0xC1 or data[1:4] != b"\x00\x00\x00":
        return False
    width = int.from_bytes(data[4:7], "little") + 1
    height = int.from_bytes(data[7:10], "little") + 1
    return _reasonable_dimensions(width, height)


def _valid_webp_image_chunk(chunk_type: bytes, data: bytes) -> bool:
    if chunk_type == b"VP8 ":
        if len(data) < 10 or data[3:6] != b"\x9d\x01\x2a":
            return False
        width = int.from_bytes(data[6:8], "little") & 0x3FFF
        height = int.from_bytes(data[8:10], "little") & 0x3FFF
    elif chunk_type == b"VP8L":
        if len(data) < 5 or data[0] != 0x2F:
            return False
        bits = int.from_bytes(data[1:5], "little")
        width = (bits & 0x3FFF) + 1
        height = ((bits >> 14) & 0x3FFF) + 1
    else:
        if len(data) < 16:
            return False
        width = int.from_bytes(data[6:9], "little") + 1
        height = int.from_bytes(data[9:12], "little") + 1
    return _reasonable_dimensions(width, height)


# Bound immediately after the validators it names, so the mapping exists
# for any module-level caller and cannot be rebuilt per call.
_CONTAINER_VALIDATORS = {
    "image/png": _valid_png_container,
    "image/jpeg": _valid_jpeg_container,
    "image/gif": _valid_gif_container,
    "image/webp": _valid_webp_container,
}


def supported_image_media_type(value: str | None) -> str | None:
    if not value:
        return None
    media_type = value.split(";", 1)[0].strip().lower()
    return media_type if media_type in SUPPORTED_IMAGE_MEDIA_TYPES else None


def image_media_type_from_filename(filename: str) -> str | None:
    guessed, _encoding = mimetypes.guess_type(filename)
    return supported_image_media_type(guessed)


def normalize_image_data_url(value: str, media_type: str | None = None) -> tuple[str, str | None]:
    """Correct a base64 image data URL's media type from its bytes when possible."""
    if not value.startswith("data:") or "," not in value:
        return value, supported_image_media_type(media_type) or media_type
    header, payload = value.split(",", 1)
    header_parts = header.split(";")
    if len(header_parts) < 2 or not any(part.lower() == "base64" for part in header_parts[1:]):
        return value, supported_image_media_type(media_type) or media_type
    try:
        raw = base64.b64decode(payload, validate=True)
    except ValueError, binascii.Error:
        return value, supported_image_media_type(media_type) or media_type
    sniffed = sniff_image_media_type(raw)
    if sniffed is None:
        return value, supported_image_media_type(media_type) or media_type
    return f"data:{sniffed};base64,{payload}", sniffed
