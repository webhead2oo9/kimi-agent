"""Real decoder fixtures, plus a required-CI test of the full isolation boundary."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
import shutil
import subprocess
import wave

import pytest

from sandbox.runner import SandboxConfig, SandboxResult, sandbox_available
from tests.sandbox_gate import sandbox_unavailable
from video_understanding.inspection import FRAME_BYTES, sample_times
from video_understanding.local import Crop, LocalVideoBackend


@pytest.fixture
def media_bins() -> tuple[str, str]:
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        sandbox_unavailable("FFmpeg is required for video inspection fixture tests")
    return ffmpeg, ffprobe


def make_fixture(root: Path, ffmpeg: str, *, offset: int = 0) -> None:
    # A six-second VFR clip: red, then blue, with a silent 250 ms green
    # event at 4s that uniform storyboard sampling misses. Generated content
    # only: no downloaded/user media ever runs outside the sandbox in tests.
    subprocess.run(
        [
            ffmpeg,
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=160x90:r=4:d=6",
            "-vf",
            "drawbox=color=red:t=fill:enable='lt(t,3)',drawbox=color=lime:t=fill:enable='between(t,4,4.24)',select='not(eq(mod(n,5),2))'",
            "-fps_mode",
            "vfr",
            "-c:v",
            "ffv1",
            "-output_ts_offset",
            str(offset),
            "-f",
            "matroska",
            str(root / "source.bin"),
        ],
        check=True,
        capture_output=True,
        timeout=20,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("offset", [0, 7])
async def test_real_decoder_preserves_vfr_playback_clock_and_finds_brief_event(
    tmp_path: Path, media_bins: tuple[str, str], monkeypatch: pytest.MonkeyPatch, offset: int
) -> None:
    ffmpeg, ffprobe = media_bins
    await asyncio.to_thread(make_fixture, tmp_path, ffmpeg, offset=offset)

    async def run_generated_fixture(
        config: SandboxConfig, root: Path, args: list[str]
    ) -> SandboxResult:
        assert config.network_mode == "none"
        assert args[args.index("-protocol_whitelist") + 1] == "file"
        actual = [arg.replace("/work/", f"{root}/") for arg in args]
        process = await asyncio.create_subprocess_exec(
            *actual, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        out, err = await asyncio.wait_for(process.communicate(), timeout=20)
        return SandboxResult(process.returncode, out.decode(), err.decode(), False, 0)

    monkeypatch.setattr("video_understanding.local.run_command_in_sandbox", run_generated_fixture)
    backend = LocalVideoBackend(SandboxConfig(), ffmpeg=ffmpeg, ffprobe=ffprobe)
    metadata = await backend.probe(tmp_path)
    assert metadata.duration == pytest.approx(6)
    assert not metadata.has_audio
    first = await backend.frame(tmp_path, 0.1, stream_index=metadata.video_stream)
    assert first.seconds == pytest.approx(0.25)
    assert first.rgb[0] > 240 and first.rgb[1] < 10 and first.rgb[2] < 10
    overview = [
        await backend.frame(tmp_path, at, stream_index=metadata.video_stream)
        for at in sample_times(0, 6, 6, 6)
    ]
    assert all(frame.rgb[1] < 10 for frame in overview)
    detailed = await backend.frame(
        tmp_path, 4, stream_index=metadata.video_stream, crop=Crop(100, 100, 800, 800)
    )
    assert detailed.seconds == pytest.approx(4)
    assert len(detailed.rgb) == FRAME_BYTES
    assert detailed.rgb[1] > 240 and detailed.rgb[0] < 10


@pytest.mark.asyncio
async def test_live_offline_video_decoder(tmp_path: Path, media_bins: tuple[str, str]) -> None:
    ffmpeg, ffprobe = media_bins
    config = SandboxConfig(
        max_memory_mb=8192,
        max_total_memory_mb=2048,
        max_output_bytes=64 * 1024,
        wall_timeout_seconds=60,
        max_cpu_seconds=45,
        extra_ro_binds=(ffmpeg, ffprobe),
    )
    if not await asyncio.to_thread(sandbox_available, config):
        sandbox_unavailable("offline video sandbox is unavailable on this host")
    await asyncio.to_thread(make_fixture, tmp_path, ffmpeg, offset=7)
    backend = LocalVideoBackend(config, ffmpeg=ffmpeg, ffprobe=ffprobe)
    metadata = await backend.probe(tmp_path)
    assert metadata.duration == pytest.approx(6)
    frame = await backend.frame(tmp_path, 4, stream_index=metadata.video_stream)
    assert frame.seconds == pytest.approx(4)
    assert frame.rgb[1] > 240
    with pytest.raises(ValueError, match="offline"):
        LocalVideoBackend(replace(config, network_mode="host"), ffmpeg=ffmpeg, ffprobe=ffprobe)


@pytest.mark.asyncio
async def test_transcription_pads_delayed_audio_and_removes_scratch(
    tmp_path: Path, media_bins: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ffmpeg, ffprobe = media_bins

    def generate() -> None:
        subprocess.run(
            [
                ffmpeg,
                "-nostdin",
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=blue:s=160x90:r=4:d=6",
                "-itsoffset",
                "2",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=2",
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-c:v",
                "ffv1",
                "-c:a",
                "pcm_s16le",
                "-output_ts_offset",
                "7",
                "-f",
                "matroska",
                str(tmp_path / "source.bin"),
            ],
            check=True,
            capture_output=True,
            timeout=20,
        )

    await asyncio.to_thread(generate)

    def simulate_asr() -> None:
        with wave.open(str(tmp_path / "audio.wav"), "rb") as audio:
            assert audio.getframerate() == 16000
            assert audio.getnchannels() == 1
            # The original track starts two seconds after the playback origin.
            assert set(audio.readframes(16000)) == {0}
            audio.setpos(32000)
            assert any(audio.readframes(1600))
        (tmp_path / "transcript.srt").write_text(
            "1\n00:00:02,000 --> 00:00:04,000\nTest cue after leading silence.\n",
            encoding="utf-8",
        )

    async def run_fixture(config: SandboxConfig, root: Path, args: list[str]) -> SandboxResult:
        assert config.network_mode == "none"
        if args[0] == "/fixture/whisper-cli":
            assert "-osrt" in args and "-ng" in args
            await asyncio.to_thread(simulate_asr)
            return SandboxResult(0, "", "", False, 0)
        actual = [arg.replace("/work/", f"{root}/") for arg in args]
        process = await asyncio.create_subprocess_exec(
            *actual, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        out, err = await asyncio.wait_for(process.communicate(), timeout=20)
        return SandboxResult(process.returncode, out.decode(), err.decode(), False, 0)

    monkeypatch.setattr("video_understanding.local.run_command_in_sandbox", run_fixture)
    backend = LocalVideoBackend(
        SandboxConfig(),
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        whisper_bin="/fixture/whisper-cli",
        whisper_model="/fixture/model.bin",
    )
    metadata = await backend.probe(tmp_path)
    assert metadata.has_audio
    assert metadata.audio_stream is not None
    assert metadata.duration == pytest.approx(6)
    [cue] = await backend.transcribe(
        tmp_path, metadata.duration, stream_index=metadata.audio_stream
    )
    assert (cue.start, cue.end) == (2, 4)
    assert not (tmp_path / "audio.wav").exists()


@pytest.mark.asyncio
async def test_probe_selects_default_tracks_and_ignores_cover_art(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    async def probe_fixture(config: SandboxConfig, root: Path, args: list[str]) -> SandboxResult:
        payload = {
            "format": {"start_time": "0", "duration": "90"},
            "streams": [
                {
                    "index": 0,
                    "codec_type": "video",
                    "duration": "90",
                    "disposition": {"attached_pic": 1, "default": 1},
                },
                {"index": 1, "codec_type": "video", "duration": "20"},
                {"index": 2, "codec_type": "audio"},
                {
                    "index": 3,
                    "codec_type": "video",
                    "duration": "30",
                    "disposition": {"default": 1},
                },
                {"index": 4, "codec_type": "audio", "disposition": {"default": 1}},
            ],
        }
        return SandboxResult(0, json.dumps(payload), "", False, 0)

    monkeypatch.setattr("video_understanding.local.run_command_in_sandbox", probe_fixture)
    backend = LocalVideoBackend(
        SandboxConfig(), ffmpeg="/fixture/ffmpeg", ffprobe="/fixture/ffprobe"
    )
    metadata = await backend.probe(tmp_path)
    assert metadata.video_stream == 3
    assert metadata.audio_stream == 4
    assert metadata.duration == 30
