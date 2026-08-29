"""Regenerate the eval image fixtures.

The PNGs in ``images/`` are committed so runs are reproducible, but they are
generated rather than photographed: each one has content a grader can assert on
in plain words ("red, green, blue"), and the generator is here so the fixture is
reviewable as code instead of an opaque binary blob.

    .venv/bin/python evals/fixtures/make_images.py
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parent / "images"


def _png(width: int, height: int, pixel_at) -> bytes:
    raw = b"".join(
        b"\x00" + b"".join(bytes(pixel_at(x, y)) for x in range(width)) for y in range(height)
    )

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def bands_rgb(x: int, y: int) -> tuple[int, int, int]:
    """Three horizontal bands: red on top, green, then blue."""
    if y < 85:
        return (220, 30, 30)
    if y < 170:
        return (30, 200, 30)
    return (30, 60, 220)


def checker_yellow_black(x: int, y: int) -> tuple[int, int, int]:
    """A yellow/black checkerboard, visually unmistakable against the bands."""
    return (245, 220, 40) if ((x // 32) + (y // 32)) % 2 == 0 else (15, 15, 15)


FIXTURES = {
    "bands-rgb.png": (256, 256, bands_rgb),
    "checker-yellow.png": (256, 256, checker_yellow_black),
}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, (width, height, fn) in FIXTURES.items():
        payload = _png(width, height, fn)
        (OUT / name).write_bytes(payload)
        print(f"wrote {name}: {len(payload)} bytes ({width}x{height})")


if __name__ == "__main__":
    main()
