"""Generate a small, deterministic clip for manual video-tool comparisons.

Run from bot/: .venv/bin/python -m evals.video_inspection_fixtures /tmp/video-eval
No model calls, credentials, downloaded media, or changes to live bot state.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess


def generate(destination: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise SystemExit("Install FFmpeg to generate the fixture")
    destination.mkdir(parents=True, exist_ok=True)
    video = destination / "brief-event.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-nostdin",
            "-v",
            "error",
            "-n",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=640x360:r=20:d=12",
            "-vf",
            "drawbox=color=red:t=fill:enable='lt(t,3)',drawbox=color=lime:t=fill:enable='between(t,4,4.19)',drawbox=x=400:y=100:w=120:h=80:color=yellow:t=fill:enable='gte(t,8)'",
            "-c:v",
            "mpeg4",
            "-q:v",
            "2",
            str(video),
        ],
        check=True,
        timeout=30,
    )
    (destination / "brief-event.srt").write_text(
        "1\n00:00:01,000 --> 00:00:02,000\nA red screen opens the demonstration.\n\n"
        "2\n00:00:06,000 --> 00:00:07,000\nA green screen flashed a moment ago.\n\n"
        "3\n00:00:10,000 --> 00:00:11,000\nA yellow rectangle is now visible.\n",
        encoding="utf-8",
    )
    (destination / "expected.json").write_text(
        json.dumps(
            {
                "duration_seconds": 12,
                "audio": False,
                "transcript": "Synthetic supplied subtitles, deliberately delayed; not ASR output.",
                "cases": [
                    {
                        "question": "What colour opens the clip?",
                        "expected": "red",
                        "interval": [0, 3],
                    },
                    {
                        "question": "Did a green screen flash, and when?",
                        "expected": "green at 4.00–4.20s; storyboard alone misses it",
                        "inspect": {"start": 3.9, "end": 4.1, "count": 1},
                    },
                    {
                        "question": "What appears near the right side later?",
                        "expected": "yellow rectangle from 8s",
                        "inspect": {
                            "start": 9,
                            "end": 10,
                            "count": 1,
                            "crop": {"left": 600, "top": 200, "width": 300, "height": 400},
                        },
                    },
                    {
                        "question": "What does the speaker sound like?",
                        "expected": "cannot establish: this clip has no audio",
                    },
                ],
                "measure": [
                    "correctness",
                    "actual timestamp error",
                    "missed events",
                    "unsupported claims",
                    "tool calls",
                    "sampled frames",
                    "latency",
                    "chat input tokens",
                    "total reported cost",
                ],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"Created {video}, brief-event.srt, and expected.json")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    generate(parser.parse_args().output)


if __name__ == "__main__":
    main()
