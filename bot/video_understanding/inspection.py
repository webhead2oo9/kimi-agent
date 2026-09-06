"""Bounded, provider-independent evidence for the experimental video inspector."""

from __future__ import annotations

from dataclasses import dataclass
import io
import math
import re

from PIL import Image, ImageDraw, ImageFont

MAX_DURATION_SECONDS = 3600
MAX_TRANSCRIPT_BYTES = 1024 * 1024
MAX_TRANSCRIPT_SEGMENTS = 10_000
FRAME_WIDTH = 640
FRAME_HEIGHT = 360
FRAME_BYTES = FRAME_WIDTH * FRAME_HEIGHT * 3


@dataclass(frozen=True)
class VideoMetadata:
    duration: float
    has_audio: bool
    video_stream: int = 0
    audio_stream: int | None = None


@dataclass(frozen=True)
class TranscriptSegment:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class VideoFrame:
    requested_seconds: float
    seconds: float
    rgb: bytes


def seconds(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{name} must be a finite number of seconds")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{name} must be a finite nonnegative number of seconds")
    return result


def sample_times(start: float, end: float, count: int, duration: float) -> tuple[float, ...]:
    if not 0 <= start < end <= duration or not 1 <= count <= 6:
        raise ValueError("Choose an interval inside the video and between one and six frames")
    # Bin centres cover the interval without seeking exactly to EOF. Short
    # videos and variable-frame-rate clips may yield the same decoded frame.
    return tuple(start + (index + 0.5) * (end - start) / count for index in range(count))


def timestamp(value: float) -> str:
    millis = round(value * 1000)
    whole, remainder = divmod(millis, 1000)
    hours, rest = divmod(whole, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours:02}:{minutes:02}:{secs:02}.{remainder:03}"


_CUE_TIME = re.compile(r"^(?:(\d{1,3}):)?(\d{2}):(\d{2})[,.](\d{3})$")


def _cue_seconds(value: str) -> float:
    match = _CUE_TIME.fullmatch(value)
    if match is None:
        raise ValueError("Transcript must use SRT or WebVTT timestamps")
    hours, minutes, secs, millis = (int(part or 0) for part in match.groups())
    if minutes >= 60 or secs >= 60:
        raise ValueError("Invalid subtitle timestamp")
    return hours * 3600 + minutes * 60 + secs + millis / 1000


def parse_subtitles(payload: bytes, duration: float) -> tuple[TranscriptSegment, ...]:
    if len(payload) > MAX_TRANSCRIPT_BYTES:
        raise ValueError("Transcript exceeds 1 MiB")
    try:
        text = payload.decode("utf-8-sig").replace("\r\n", "\n").strip()
    except UnicodeDecodeError as exc:
        raise ValueError("Transcript must be UTF-8 SRT or WebVTT") from exc
    if re.search(r"^X-TIMESTAMP-MAP\s*=", text, flags=re.MULTILINE | re.IGNORECASE):
        raise ValueError(
            "WebVTT X-TIMESTAMP-MAP timelines are unsupported; supply subtitles on the video's playback clock"
        )
    segments: list[TranscriptSegment] = []
    for block in re.split(r"\n\s*\n", text):
        lines = block.splitlines()
        if not lines or lines[0].startswith(("WEBVTT", "NOTE", "STYLE", "REGION")):
            continue
        timing_index = 0 if "-->" in lines[0] else 1
        if timing_index >= len(lines) or "-->" not in lines[timing_index]:
            raise ValueError("Transcript contains a malformed subtitle cue")
        left, right = lines[timing_index].split("-->", 1)
        end_parts = right.strip().split()
        if not end_parts:
            raise ValueError("Transcript cue has no end timestamp")
        start, end = _cue_seconds(left.strip()), _cue_seconds(end_parts[0])
        words = " ".join(lines[timing_index + 1 :]).strip()
        if not 0 <= start < duration or not start < end <= duration + 0.5:
            raise ValueError("Transcript timestamps fall outside this video")
        if not words or len(words) > 2000:
            raise ValueError("Transcript cues must contain between 1 and 2000 characters")
        segments.append(TranscriptSegment(start, min(end, duration), words))
        if len(segments) > MAX_TRANSCRIPT_SEGMENTS:
            raise ValueError("Transcript contains too many cues")
    return tuple(sorted(segments, key=lambda segment: (segment.start, segment.end)))


def transcript_page(
    segments: tuple[TranscriptSegment, ...],
    *,
    duration: float,
    query: str = "",
    start: float = 0,
    end: float | None = None,
    offset: int = 0,
) -> dict[str, object]:
    matching = [
        segment
        for segment in segments
        if segment.end >= start
        and (end is None or segment.start <= end)
        and query.casefold() in segment.text.casefold()
    ]
    rows: list[dict[str, object]] = []
    chars = 0
    for segment in matching[offset:]:
        if len(rows) == 20 or chars + len(segment.text) > 6000:
            break
        chars += len(segment.text)
        rows.append(
            {
                "start_seconds": segment.start,
                "end_seconds": segment.end,
                "text": segment.text,
                "suggested_visual_window": [
                    max(0, segment.start - 5),
                    min(duration, segment.end + 5),
                ],
            }
        )
    next_offset = offset + len(rows)
    return {
        "segments": rows,
        "matches": len(matching),
        "next_offset": next_offset if next_offset < len(matching) else None,
    }


def render_frames(
    frames: tuple[VideoFrame, ...], *, storyboard: bool
) -> tuple[tuple[bytes, ...], tuple[dict[str, object], ...]]:
    """Encode only fixed-size RGB from the decoder; never decode media on the host."""
    kept: list[Image.Image] = []
    thumbnails: list[bytes] = []
    rows: list[dict[str, object]] = []
    for frame in frames:
        if len(frame.rgb) != FRAME_BYTES:
            raise ValueError("Video decoder returned an invalid frame size")
        image = Image.frombytes("RGB", (FRAME_WIDTH, FRAME_HEIGHT), frame.rgb)
        small = image.resize((16, 9)).tobytes()
        duplicate = next(
            (
                index
                for index, previous in enumerate(thumbnails)
                if sum(abs(a - b) for a, b in zip(small, previous, strict=True)) / len(small) < 1
            ),
            None,
        )
        row: dict[str, object] = {
            "requested_seconds": frame.requested_seconds,
            "actual_seconds": frame.seconds,
            "timestamp": timestamp(frame.seconds),
        }
        if duplicate is not None:
            row["near_duplicate_of_image"] = duplicate + 1
        else:
            thumbnails.append(small)
            kept.append(image)
            row["image"] = len(kept)
        rows.append(row)

    def encode(image: Image.Image) -> bytes:
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=80)
        return output.getvalue()

    if storyboard:
        sheet = Image.new("RGB", (960, 204 * math.ceil(len(kept) / 3)), "#151515")
        draw = ImageDraw.Draw(sheet)
        font = ImageFont.load_default(size=16)
        for index, image in enumerate(kept):
            x, y = index % 3 * 320, index // 3 * 204
            sheet.paste(image.resize((320, 180)), (x, y))
            row = next(row for row in rows if row.get("image") == index + 1)
            draw.text((x + 5, y + 183), f"{index + 1}: {row['timestamp']}", fill="white", font=font)
        return (encode(sheet),), tuple(rows)
    return tuple(encode(image) for image in kept), tuple(rows)
