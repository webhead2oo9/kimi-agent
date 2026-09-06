# Experimental local video inspection

`video_inspect` is a separate, opt-in experiment for comparing transcript-first
frame inspection with the existing [Gemini video tool](video-understanding.md).
The chat model reads speech text and requests a bounded set of images itself.
No second model summarizes the frames on its behalf.

## Enable on a development instance

Follow [development setup](development.md) for isolated credentials and state.
Install FFmpeg (`ffmpeg` and `ffprobe` on PATH) and satisfy the offline Linux
[sandbox prerequisites](code-exec.md). The tool fails closed if its sandbox
startup probe fails. Enabling `CODE_EXEC_ENABLED` is not required; the inspector
uses the same sandbox implementation with a separate, always-offline profile.

FFmpeg is a host dependency, not a Python package. On the supported Ubuntu
deployment, install the distribution package; it provides both required
executables and their shared libraries:

```bash
sudo apt-get update
sudo apt-get install --yes --no-install-recommends ffmpeg
command -v ffmpeg
command -v ffprobe
ffmpeg -version
ffprobe -version
```

Both `command -v` checks must print absolute paths. Run the decoder test below
as the bot service user after satisfying the sandbox prerequisites; a missing
binary, shared library, user systemd bus, or workspace causes the tool to stay
unregistered at startup.

```dotenv
VIDEO_INSPECTION_ENABLED=true
```

Automatic speech transcription is optional. Install a CPU-capable
[whisper.cpp CLI](https://github.com/ggml-org/whisper.cpp/tree/master/examples/cli)
and a compatible GGML model, then set both absolute paths:

```dotenv
VIDEO_INSPECTION_WHISPER_BIN=/usr/local/bin/whisper-cli
VIDEO_INSPECTION_WHISPER_MODEL=/opt/whisper/ggml-base.en.bin
```

The binary, model, and system library lookup paths (`/etc/alternatives` and
`/etc/ld.so.cache`, when present) are mounted read-only. Install required shared libraries
under the normal system library paths. No models or binaries are downloaded at
runtime. Start with a small CPU model; the process tree has a 2 GiB memory cap.
With both paths blank, supplied UTF-8 SRT/WebVTT files and visual-only inspection
still work. A request to transcribe speech explains that local transcription
is unavailable. Both paths are environment-only; the feature flag may also be
set in the restart-required operator settings overlay.

Restart and confirm startup reports `local video inspection (experimental)`.
Select an image-capable chat model for frame inspection; this tool does not
switch models. `MAX_TURN_IMAGES=0` also disables video frame output. Text-only
chat models can still use the transcript operations.
The existing `video` tool can remain enabled for comparison, or be blocked in
the test channel to make the experiment the only available video tool.

## Model workflow

Load `video_inspect` through `browse_tools`, then open one source:

```json
{"action":"start","attachment":"clip.mp4"}
```

```json
{"action":"start","path":"imports/clip.mp4","transcript_path":"imports/clip.srt"}
```

`start` returns the duration, audio availability, and the first page of any
supplied subtitles. It sends **no images** and does not automatically transcribe
audio. Sources are exact current-message Discord attachments or safe workspace
video paths. This experiment does not download YouTube or arbitrary video URLs;
the Gemini tool remains available for YouTube. The current video format allowlist
and 500 MiB source limit apply, with a one-hour duration limit. Containers without
usable timeline metadata fail explicitly. The default video and audio tracks are
selected when present; otherwise the first tracks are used. Attached cover art
is excluded from video selection.

Find speech without spending the visual budget:

```json
{"action":"transcript","query":"error"}
```

The first transcript request runs local transcription if needed; subsequent
requests reuse it within this turn. Search is a case-insensitive substring
match. Omit `query` to page all cues, use `offset` from `next_offset`, or narrow
with `start`/`end`. Each page contains at most 20 cues and 6,000 text characters.
Results suggest visual windows extending five seconds before and after each
cue. Supplied subtitles are not verified against the audio; speech text alone
does not establish that a visual event happened.
HLS WebVTT containing `X-TIMESTAMP-MAP` is rejected rather than silently treating
its local cue times as playback times. Convert those subtitles to the video's
zero-based playback clock before supplying them.

Request visual orientation only when useful:

```json
{"action":"storyboard"}
```

This samples six evenly spaced interval centres and returns one numbered
storyboard, at most 960 × 408 pixels. It deliberately makes no claim that these
six frames represent every event in a long video.

Inspect a promising interval, or crop a detail:

```json
{"action":"frames","start":129,"end":139,"count":3}
```

```json
{"action":"frames","start":134,"end":134.2,"count":1,"crop":{"left":250,"top":200,"width":500,"height":400}}
```

Detailed requests cover at most 120 seconds and return one to four images,
each letterboxed to 640 × 360. `count` defaults to three. Crops use a 0–1000
grid on the displayed video before letterboxing. Frames are sampled at the
centres of equal bins; a one-frame request around 134.0 seconds should bracket
that time, for example 133.9–134.1. Results distinguish requested times from
actual decoded timestamps. Variable frame rates can shift the selected frame.

## Keeping image context bounded

- One video source and eight tool calls per outer turn.
- Twelve sampled frames per turn, including the six storyboard tiles. Failed
  decoder attempts also consume their reserved frame allowance.
- Near-identical images within a batch are omitted, with the original requested
  and actual timestamps mapped to the retained image. Tiny changes can be lost
  by this heuristic; request a single frame or a crop to inspect them.
- Each batch is limited to 2 MiB of encoded images. A storyboard is one image,
  but still consumes six sampled-frame units.
- A new batch removes previous video image payloads from the next model request.
  Timestamp mappings and the model's written observations remain. Unrelated
  user/workspace images are preserved.
- The final batch is also removed from the in-memory conversation history at
  turn completion. Video images are not saved as Discord transcript attachments.

The model should write concise observations with timestamps before requesting
another batch. Replacement does not create an automatic visual summary. Images
already sent to the configured chat provider remain subject to that provider's
retention policy; local replacement is not a provider-side deletion request.

This first version runs in foreground chat only. Sources expire at turn end;
to revisit a workspace source in a later turn, start it again. An attachment
must be attached to that later triggering message again.

## Processing and cleanup

The inspector copies the source into a private job directory in the actor's
workspace. FFprobe, FFmpeg, and optional whisper.cpp run under the existing
systemd/bubblewrap/seccomp sandbox with no network, no credentials, and only
that job directory writable. Fixed commands limit decoding threads, output
sizes, memory, CPU, and wall time. Host-side image layout reads fixed-size raw
RGB buffers rather than decoding the original container.

Two source sessions may exist at once; a third waits at most 30 seconds. Each
job has a 650 MiB disk allowance and a 2 GiB process-tree memory cap. Decoder
calls have a 60-second wall limit; whisper.cpp has a 15-minute wall limit and
10 CPU minutes. Ordinary turn deadlines still apply. Long clips may exceed
these limits even when their source size is accepted.

Turn finalization deletes the private source, extracted audio, transcript, and
frame files, including cancellation/error paths. A process crash can leave job
files behind; normal workspace TTL sweeping and full user-data deletion cover
those files. This path creates no Google Files or Interactions resources.
Selected frames and returned transcript pages go to the configured chat
provider as untrusted context, and its normal token accounting applies.

## Test protocol

Generate a deterministic clip with a brief silent event and delayed supplied
subtitles:

```bash
cd bot
.venv/bin/python -m evals.video_inspection_fixtures /tmp/video-eval
```

The directory contains `brief-event.mp4`, `brief-event.srt`, and `expected.json`.
The clip has **no speech audio**: the subtitle file is a controlled alignment
fixture, not an automatic transcription test. Import the subtitles into the
test user's workspace and attach the MP4, or import both files. The expected
answers include a 200 ms green flash missed by the storyboard, a later cropped
yellow rectangle, and a question whose audio answer must be unknown.

Compare Gemini and local inspection in separate fresh conversations with the
same chat model and questions. Record correctness, timestamp error, missed
events, unsupported claims, tool calls, sampled frames, latency, chat input
tokens, and total reported cost. Include the cost of the Gemini specialist in
that arm. The fixture does not prove real-video answer quality.

Then repeat with human-annotated clips covering speech-leading visuals,
speech-lagging visuals, screen recordings with tiny text, silent fast action,
repeated scenes, portrait video, ambiguous audio, and long talks. Test automatic
transcription separately with known speech and check timing as well as words.
Do not treat a transcript match as proof of an event or a missing storyboard
frame as proof of absence.

Automated checks:

```bash
.venv/bin/python -m pytest tests/test_video_inspection.py tests/test_video_inspection_local.py tests/test_core_smoke.py -q
```

To require the real offline FFmpeg sandbox test instead of allowing a local
prerequisite skip, run:

```bash
KIMI_REQUIRE_SANDBOX_TESTS=1 .venv/bin/python -m pytest tests/test_video_inspection_local.py -q
```

Real FFmpeg fixtures cover variable frame rates, nonzero start timestamps,
near-duplicate suppression, and targeted recovery of a missed brief event.
Tool tests cover transcript paging, limits, scope, cancellation cleanup, and
image replacement. The CI sandbox job installs FFmpeg and requires the live
offline decoding test rather than silently skipping it. Local machines without
the sandbox prerequisites skip that one live test explicitly.
