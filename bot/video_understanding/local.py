"""Local video decoding and optional whisper.cpp, always inside the offline sandbox."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
import json
import math
import os
from pathlib import Path
import re
import stat

from sandbox.runner import SandboxConfig, SandboxResult, run_command_in_sandbox
from video_understanding.inspection import (
    FRAME_BYTES,
    MAX_DURATION_SECONDS,
    MAX_TRANSCRIPT_BYTES,
    TranscriptSegment,
    VideoFrame,
    VideoMetadata,
    parse_subtitles,
    seconds,
)


@dataclass(frozen=True)
class Crop:
    left: int
    top: int
    width: int
    height: int

    def __post_init__(self) -> None:
        values = (self.left, self.top, self.width, self.height)
        if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
            raise ValueError("Crop coordinates must be integers on a 0–1000 grid")
        if not (
            0 <= self.left < 1000
            and 0 <= self.top < 1000
            and 1 <= self.width <= 1000 - self.left
            and 1 <= self.height <= 1000 - self.top
        ):
            raise ValueError("Crop must fit inside the video on a 0–1000 grid")


def read_private_file(path: Path, maximum: int, *, exact: int | None = None) -> bytes:
    """Read a bounded regular output after the sandbox has stopped."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum:
            raise ValueError("Video output is not a bounded regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            payload = handle.read(maximum + 1)
        if len(payload) > maximum or (exact is not None and len(payload) != exact):
            raise ValueError("Video output has an invalid size")
        return payload
    finally:
        os.close(descriptor)


class LocalVideoBackend:
    def __init__(
        self,
        config: SandboxConfig,
        *,
        ffmpeg: str,
        ffprobe: str,
        whisper_bin: str = "",
        whisper_model: str = "",
    ) -> None:
        if config.network_mode != "none":
            raise ValueError("Video inspection requires the offline sandbox")
        # Debian/Ubuntu BLAS/LAPACK libraries use /etc/alternatives symlinks;
        # the loader cache also resolves libraries in distro-specific directories.
        # These system lookup paths are read-only and specific to this profile.
        self.config = replace(
            config,
            extra_ro_binds=tuple(
                dict.fromkeys((*config.extra_ro_binds, "/etc/alternatives", "/etc/ld.so.cache"))
            ),
        )
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self.whisper_bin = whisper_bin
        self.whisper_model = whisper_model

    @property
    def can_transcribe(self) -> bool:
        return bool(self.whisper_bin and self.whisper_model)

    async def _run(
        self, root: Path, args: list[str], *, transcribing: bool = False
    ) -> SandboxResult:
        config = self.config
        if transcribing:
            config = replace(config, wall_timeout_seconds=900, max_cpu_seconds=600)
        result = await run_command_in_sandbox(config, root, args)
        if result.timed_out:
            raise ValueError("Local video processing timed out; try a shorter clip")
        if result.quota_exceeded or result.exit_code != 0:
            # Decoder stderr may contain source content, host paths, or arbitrary
            # container metadata. It is never a model-facing error message.
            raise ValueError("Local video processing failed or exceeded its resource limits")
        return result

    async def probe(self, root: Path) -> VideoMetadata:
        result = await self._run(
            root,
            [
                self.ffprobe,
                "-v",
                "error",
                "-protocol_whitelist",
                "file",
                "-show_entries",
                "format=duration,start_time:stream=index,codec_type,duration,start_time:stream_tags=DURATION:stream_disposition=default,attached_pic",
                "-of",
                "json",
                "/work/source.bin",
            ],
        )
        try:
            payload = json.loads(result.stdout)
            streams = payload["streams"]

            def primary(kind: str) -> dict | None:
                candidates = [
                    stream
                    for stream in streams
                    if stream["codec_type"] == kind
                    and not stream.get("disposition", {}).get("attached_pic", 0)
                ]
                return next(
                    (
                        stream
                        for stream in candidates
                        if stream.get("disposition", {}).get("default", 0)
                    ),
                    candidates[0] if candidates else None,
                )

            video, audio = primary("video"), primary("audio")
            if video is None:
                raise ValueError("No video stream")
            video_index = int(video["index"])
            audio_index = int(audio["index"]) if audio is not None else None
            if video_index < 0 or (audio_index is not None and audio_index < 0):
                raise ValueError("Invalid stream index")
            origin = float(payload["format"].get("start_time", 0))
            start = float(video.get("start_time", origin))
            if not math.isfinite(origin) or not math.isfinite(start):
                raise ValueError("Invalid stream origin")
            if "duration" in video:
                duration = seconds(float(video["duration"]), "duration") + max(0, start - origin)
            elif "DURATION" in video.get("tags", {}):
                # Matroska's DURATION tag is an absolute end timestamp, whereas
                # stream.duration is a length. Offset muxes can report an
                # inflated format.duration; prefer the first video stream.
                hours, minutes, secs = video["tags"]["DURATION"].split(":")
                duration = int(hours) * 3600 + int(minutes) * 60 + float(secs) - origin
            elif abs(origin) < 0.001:
                duration = seconds(float(payload["format"]["duration"]), "duration")
            else:
                raise ValueError("Offset video has no usable stream duration")
            duration = seconds(duration, "duration")
        except (KeyError, TypeError, ValueError, OverflowError, StopIteration) as exc:
            raise ValueError("Video has no usable duration or stream metadata") from exc
        if not 0 < duration <= MAX_DURATION_SECONDS:
            raise ValueError("Choose a video no longer than one hour")
        return VideoMetadata(duration, audio_index is not None, video_index, audio_index)

    async def frame(
        self, root: Path, at: float, *, stream_index: int, crop: Crop | None = None
    ) -> VideoFrame:
        filters = []
        if crop is not None:
            filters.append(
                f"crop=iw*{crop.width}/1000:ih*{crop.height}/1000:"
                f"iw*{crop.left}/1000:ih*{crop.top}/1000"
            )
        filters.extend(
            (
                "scale=640:360:force_original_aspect_ratio=decrease",
                "pad=640:360:(ow-iw)/2:(oh-ih)/2",
                "setsar=1",
                "showinfo",
            )
        )
        output = root / "frame.rgb"
        await asyncio.to_thread(output.unlink, missing_ok=True)
        result = await self._run(
            root,
            [
                self.ffmpeg,
                "-nostdin",
                "-hide_banner",
                "-y",
                "-threads",
                "2",
                "-filter_threads",
                "1",
                "-protocol_whitelist",
                "file",
                "-copyts",
                "-start_at_zero",
                "-ss",
                f"{at:.6f}",
                "-i",
                "/work/source.bin",
                "-map",
                f"0:{stream_index}",
                "-an",
                "-sn",
                "-dn",
                "-frames:v",
                "1",
                "-vf",
                ",".join(filters),
                "-fps_mode",
                "passthrough",
                "-threads",
                "1",
                "-pix_fmt",
                "rgb24",
                "-f",
                "rawvideo",
                "/work/frame.rgb",
            ],
        )
        # showinfo preserves the actual selected frame PTS on the source's
        # zero-based playback clock, even for VFR and non-zero container starts.
        match = re.search(r"\bn:\s*0\s+pts:.*?pts_time:([\d.eE+-]+)", result.stderr)
        if match is None:
            raise ValueError("No timestamped frame was decoded at that position")
        actual = seconds(float(match[1]), "decoded frame timestamp")
        payload = await asyncio.to_thread(read_private_file, output, FRAME_BYTES, exact=FRAME_BYTES)
        return VideoFrame(at, actual, payload)

    async def transcribe(
        self, root: Path, duration: float, *, stream_index: int
    ) -> tuple[TranscriptSegment, ...]:
        if not self.can_transcribe:
            raise ValueError("Supply an SRT/VTT transcript or configure local whisper.cpp")
        await self._run(
            root,
            [
                self.ffmpeg,
                "-nostdin",
                "-v",
                "error",
                "-y",
                "-threads",
                "2",
                "-protocol_whitelist",
                "file",
                "-i",
                "/work/source.bin",
                "-map",
                f"0:{stream_index}",
                "-vn",
                "-sn",
                "-dn",
                "-af",
                "aresample=async=1:first_pts=0",
                "-t",
                f"{duration:.6f}",
                "-ar",
                "16000",
                "-ac",
                "1",
                "-c:a",
                "pcm_s16le",
                "/work/audio.wav",
            ],
        )
        try:
            await self._run(
                root,
                [
                    self.whisper_bin,
                    "-m",
                    self.whisper_model,
                    "-f",
                    "/work/audio.wav",
                    "-of",
                    "/work/transcript",
                    "-osrt",
                    "-l",
                    "auto",
                    "-t",
                    "2",
                    "-ng",
                ],
                transcribing=True,
            )
            payload = await asyncio.to_thread(
                read_private_file, root / "transcript.srt", MAX_TRANSCRIPT_BYTES
            )
            return parse_subtitles(payload, duration)
        finally:
            await asyncio.to_thread((root / "audio.wav").unlink, missing_ok=True)
