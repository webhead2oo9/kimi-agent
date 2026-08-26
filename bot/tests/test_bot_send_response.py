from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import discord
import pytest

import discord_adapter.io as discord_io
from agent.activity import ActivityUpdate
from discord_adapter.io import send_response, suppress_link_previews
from tools.embeds import EmbedSpec


class RecordingChannel:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.fail_next_file_upload = True

    async def send(self, content: str, **kwargs: object) -> None:
        self.calls.append({"content": content, **kwargs})
        if kwargs.get("files") and self.fail_next_file_upload:
            self.fail_next_file_upload = False
            response = cast(Any, SimpleNamespace(status=413, reason="Payload Too Large"))
            raise discord.HTTPException(response=response, message="file too large")


class RecordingStatusMessage:
    def __init__(self, content: str, message_id: int = 0) -> None:
        self.id = message_id
        self.content = content
        self.edits: list[str] = []
        self.deleted = False
        self.delete_delays: list[float | None] = []

    async def edit(self, *, content: str) -> None:
        self.content = content
        self.edits.append(content)

    async def delete(self, *, delay: float | None = None) -> None:
        self.delete_delays.append(delay)
        self.deleted = True


class RecordingStatusChannel:
    def __init__(self) -> None:
        self.messages: list[RecordingStatusMessage] = []
        self.send_kwargs: list[dict[str, object]] = []
        self._next_id = 1000

    async def send(self, content: str, **kwargs: object) -> RecordingStatusMessage:
        message = RecordingStatusMessage(content, self._next_id)
        self._next_id += 1
        self.messages.append(message)
        self.send_kwargs.append(kwargs)
        return message


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_suppress_link_previews_wraps_standalone_urls_and_keeps_punctuation() -> None:
    content = "Visit https://example.com/a_(b), then HTTPS://example.org?q=1."

    assert suppress_link_previews(content) == (
        "Visit <https://example.com/a_(b)>, then <HTTPS://example.org?q=1>."
    )


def test_suppress_link_previews_preserves_markdown_and_code_urls() -> None:
    content = (
        "`https://inline.example`\n"
        "```text\nhttps://fenced.example\n```\n"
        "<https://already.example>\n"
        "[docs](https://markdown.example/a_(b))\n"
        "![image](https://image.example/p.png)\n"
        "https://bare.example"
    )

    assert suppress_link_previews(content) == content.replace(
        "https://bare.example", "<https://bare.example>"
    )


@pytest.mark.asyncio
async def test_url_wrapping_happens_before_discord_chunking() -> None:
    channel = RecordingChannel()
    channel.fail_next_file_upload = False
    url = "https://example.com"
    content = f"{'a' * (discord_io.DISCORD_MAX_LENGTH - len(url) - 1)} {url}"
    assert len(content) == discord_io.DISCORD_MAX_LENGTH

    await send_response(cast(discord.abc.Messageable, channel), content)

    assert len(channel.calls) == 2
    assert f"<{url}>" in channel.calls[1]["content"]


@pytest.mark.asyncio
async def test_send_response_marks_forbidden_retry_as_permanent() -> None:
    response = cast(Any, SimpleNamespace(status=403, reason="Forbidden"))

    class ForbiddenChannel:
        calls = 0

        async def send(self, _content: str, **_kwargs: object) -> None:
            self.calls += 1
            raise discord.Forbidden(response, "missing permissions")

    channel = ForbiddenChannel()

    sent = await send_response(cast(discord.abc.Messageable, channel), "hello")

    assert sent.delivery_failed is True
    assert sent.delivery_permanent is True
    assert sent.delivery_error == "Forbidden"
    assert channel.calls == 2


@pytest.mark.asyncio
async def test_send_response_retry_preserves_file_upload(tmp_path: Path) -> None:
    output = tmp_path / "artifact.txt"
    output.write_text("artifact", encoding="utf-8")
    channel = RecordingChannel()

    await send_response(
        cast(discord.abc.Messageable, channel),
        "Here is the artifact.",
        output_files=[str(output)],
        allowed_file_roots=[tmp_path],
    )

    assert len(channel.calls) == 2
    assert channel.calls[0]["files"]
    assert channel.calls[1]["content"] == "Here is the artifact."
    assert channel.calls[1]["files"]


@pytest.mark.asyncio
async def test_send_response_omits_only_files_over_guild_limit(tmp_path: Path) -> None:
    fitting = tmp_path / "fits.zip"
    fitting.write_bytes(b"1234")
    oversized = tmp_path / "too-large.zip"
    oversized.write_bytes(b"12345")
    channel = RecordingChannel()
    channel.guild = SimpleNamespace(filesize_limit=4)
    channel.fail_next_file_upload = False

    sent = await send_response(
        cast(discord.abc.Messageable, channel),
        "The files are attached.",
        output_files=[str(fitting), str(oversized)],
        allowed_file_roots=[tmp_path],
    )

    assert channel.calls[0]["content"].startswith("Delivery notice:")
    assert "too-large.zip: 5 bytes" in channel.calls[0]["content"]
    assert "Ignore any claim below that an omitted file was attached." in channel.calls[0][
        "content"
    ]
    assert "**" not in channel.calls[0]["content"]
    assert "`" not in channel.calls[0]["content"]
    assert channel.calls[0]["content"].endswith("The files are attached.")
    uploaded = channel.calls[0]["files"]
    assert [Path(file.fp.name).name for file in uploaded] == ["fits.zip"]
    assert sent.attachment_plan is not None
    assert [item.filename for item in sent.attachment_plan.omitted] == ["too-large.zip"]


@pytest.mark.asyncio
async def test_send_response_uses_default_limit_without_guild(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "artifact.zip"
    output.write_bytes(b"12345")
    channel = RecordingChannel()
    channel.fail_next_file_upload = False
    monkeypatch.setattr(discord_io, "DISCORD_DEFAULT_FILE_SIZE_LIMIT_BYTES", 4)

    await send_response(
        cast(discord.abc.Messageable, channel),
        "Done.",
        output_files=[str(output)],
        allowed_file_roots=[tmp_path],
    )

    assert "4 bytes per-file limit" in channel.calls[0]["content"]
    assert not channel.calls[0].get("files")


@pytest.mark.asyncio
async def test_send_response_notice_keeps_exact_sizes_after_human_rounding(
    tmp_path: Path,
) -> None:
    output = tmp_path / "artifact.zip"
    output.write_bytes(b"x" * 36_332)
    channel = RecordingChannel()
    channel.guild = SimpleNamespace(filesize_limit=36_331)
    channel.fail_next_file_upload = False

    await send_response(
        cast(discord.abc.Messageable, channel),
        "Done.",
        output_files=[str(output)],
        allowed_file_roots=[tmp_path],
    )

    notice = channel.calls[0]["content"]
    assert "35.5 KiB (36,331 bytes) per-file limit" in notice
    assert "artifact.zip: 35.5 KiB (36,332 bytes)" in notice


@pytest.mark.asyncio
async def test_send_response_notice_neutralizes_filename_formatting_and_mentions(
    tmp_path: Path,
) -> None:
    output = tmp_path / "# @everyone__`report`.zip"
    output.write_bytes(b"12345")
    channel = RecordingChannel()
    channel.guild = SimpleNamespace(filesize_limit=4)
    channel.fail_next_file_upload = False

    await send_response(
        cast(discord.abc.Messageable, channel),
        "Done.",
        output_files=[str(output)],
        allowed_file_roots=[tmp_path],
    )

    notice = channel.calls[0]["content"]
    assert "@everyone" not in notice
    assert "__" not in notice
    assert "`" not in notice
    assert "\n# " not in notice
    assert "File # ＠everyone＿＿｀report｀.zip" in notice
    allowed_mentions = channel.calls[0]["allowed_mentions"]
    assert allowed_mentions.everyone is False
    assert allowed_mentions.users is False
    assert allowed_mentions.roles is False


@pytest.mark.asyncio
async def test_send_response_puts_oversize_notice_in_first_long_chunk(tmp_path: Path) -> None:
    output = tmp_path / "artifact.zip"
    output.write_bytes(b"12345")
    channel = RecordingChannel()
    channel.guild = SimpleNamespace(filesize_limit=4)
    channel.fail_next_file_upload = False

    await send_response(
        cast(discord.abc.Messageable, channel),
        "response-start\n" + ("detail " * 1000),
        output_files=[str(output)],
        allowed_file_roots=[tmp_path],
    )

    assert len(channel.calls) > 1
    assert channel.calls[0]["content"].startswith("Delivery notice:")
    assert "response-start" in channel.calls[0]["content"]
    assert all(not call.get("files") for call in channel.calls)


@pytest.mark.asyncio
async def test_send_response_skips_embed_when_owned_attachment_is_oversized(
    tmp_path: Path,
) -> None:
    image = tmp_path / "preview.png"
    image.write_bytes(b"12345")
    other = tmp_path / "report.txt"
    other.write_bytes(b"1234")
    channel = RecordingChannel()
    channel.guild = SimpleNamespace(filesize_limit=4)
    channel.fail_next_file_upload = False

    await send_response(
        cast(discord.abc.Messageable, channel),
        "Report ready.",
        output_files=[str(image), str(other)],
        allowed_file_roots=[tmp_path],
        embed=EmbedSpec(title="Preview", image="attachment://preview.png"),
    )

    assert "embeds" not in channel.calls[0]
    uploaded = channel.calls[0]["files"]
    assert [Path(file.fp.name).name for file in uploaded] == ["report.txt"]


@pytest.mark.asyncio
async def test_link_suppression_preserves_uploaded_image_preview(tmp_path: Path) -> None:
    output = tmp_path / "preview.png"
    output.write_bytes(b"\x89PNG\r\n")
    channel = RecordingChannel()
    channel.fail_next_file_upload = False

    await send_response(
        cast(discord.abc.Messageable, channel),
        "See https://example.com",
        output_files=[str(output)],
        allowed_file_roots=[tmp_path],
    )

    assert channel.calls[0]["content"] == "See <https://example.com>"
    assert channel.calls[0]["files"]
    assert "suppress_embeds" not in channel.calls[0]


@pytest.mark.asyncio
async def test_send_response_drops_files_when_no_allowed_roots(tmp_path: Path) -> None:
    # With no allowed roots specified, output files are not attached (fail-closed)
    # rather than bypassing the containment check.
    output = tmp_path / "artifact.txt"
    output.write_text("artifact", encoding="utf-8")
    channel = RecordingChannel()
    channel.fail_next_file_upload = False

    await send_response(
        cast(discord.abc.Messageable, channel),
        "Here is the artifact.",
        output_files=[str(output)],
    )

    assert not channel.calls[0].get("files")


@pytest.mark.asyncio
async def test_send_response_filters_output_files_outside_allowed_roots(tmp_path: Path) -> None:
    allowed_root = tmp_path / "workspace" / "files"
    allowed_root.mkdir(parents=True)
    allowed = allowed_root / "artifact.txt"
    allowed.write_text("artifact", encoding="utf-8")

    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")

    channel = RecordingChannel()
    channel.fail_next_file_upload = False

    await send_response(
        cast(discord.abc.Messageable, channel),
        "Here are the artifacts.",
        output_files=[str(allowed), str(outside)],
        allowed_file_roots=[allowed_root],
    )

    files = channel.calls[0]["files"]
    assert len(files) == 1
    assert Path(files[0].fp.name) == allowed


@pytest.mark.asyncio
async def test_send_response_skips_output_file_that_disappears_before_upload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "artifact.txt"
    output.write_text("artifact", encoding="utf-8")
    channel = RecordingChannel()
    channel.fail_next_file_upload = False

    def missing_file(*args: Any, **kwargs: Any) -> discord.File:
        raise FileNotFoundError(args[0])

    monkeypatch.setattr(discord_io.discord, "File", missing_file)

    await send_response(
        cast(discord.abc.Messageable, channel),
        "Here is the artifact.",
        output_files=[str(output)],
        allowed_file_roots=[tmp_path],
    )

    assert channel.calls == [{"content": "Here is the artifact."}]


@pytest.mark.asyncio
async def test_send_response_mention_author_controls_reply_ping() -> None:
    channel = RecordingChannel()
    channel.fail_next_file_upload = False
    reference = object()

    await send_response(
        cast(discord.abc.Messageable, channel),
        "hello",
        reference=cast(discord.Message, reference),
        mention_author=True,
    )
    await send_response(
        cast(discord.abc.Messageable, channel),
        "world",
        reference=cast(discord.Message, reference),
    )

    assert channel.calls[0]["mention_author"] is True
    assert channel.calls[1]["mention_author"] is False


@pytest.mark.asyncio
async def test_activity_reporter_edits_status_at_most_once_per_second() -> None:
    channel = RecordingStatusChannel()
    clock = FakeClock()
    reference = object()
    reporter = discord_io.DiscordActivityReporter(
        cast(discord.abc.Messageable, channel),
        reference=cast(discord.Message, reference),
        clock=clock,
    )

    await reporter(ActivityUpdate(label="Thinking..."))
    await reporter(ActivityUpdate(label="Using internet_search..."))
    clock.advance(0.99)
    await reporter(ActivityUpdate(label="Publishing a page..."))
    clock.advance(0.01)
    await reporter(ActivityUpdate(label="Publishing a page..."))
    await reporter.finish()

    assert [message.content for message in channel.messages] == ["Publishing a page..."]
    assert channel.messages[0].edits == ["Publishing a page..."]
    assert channel.messages[0].deleted is True
    assert channel.send_kwargs == [{"reference": reference, "mention_author": False}]


@pytest.mark.asyncio
async def test_activity_reporter_flushes_latest_throttled_status() -> None:
    channel = RecordingStatusChannel()
    reporter = discord_io.DiscordActivityReporter(
        cast(discord.abc.Messageable, channel),
        min_interval_seconds=0.01,
    )

    await reporter(ActivityUpdate(label="Thinking..."))
    await reporter(ActivityUpdate(label="Using internet_search..."))
    await reporter(ActivityUpdate(label="Publishing a page..."))
    await asyncio.sleep(0.02)
    await reporter.finish()

    assert channel.messages[0].edits == ["Publishing a page..."]


def test_format_narration_step_uses_muted_subtext_for_tools() -> None:
    step = discord_io._format_narration_step("Pulling it up.", ["browse_tools"])
    assert step == "Pulling it up.\n-# Looking for the right tool"


def test_format_narration_step_footer_only_when_no_narration() -> None:
    step = discord_io._format_narration_step("", ["browse_tools", "edit_file"])
    assert step == "-# Looking for the right tool, Editing a file"


def test_format_narration_step_title_cases_unknown_tool() -> None:
    step = discord_io._format_narration_step("", ["my_custom_skill"])
    assert step == "-# My Custom Skill"


def test_format_narration_step_dedupes_repeated_tool() -> None:
    step = discord_io._format_narration_step(
        "", ["discord_text_search", "discord_text_search", "edit_file"]
    )
    assert step == "-# Searching this server, Editing a file"


def test_format_narration_step_text_only_when_no_tools() -> None:
    assert discord_io._format_narration_step("Just thinking.", []) == "Just thinking."


def test_format_narration_step_empty_when_nothing() -> None:
    assert discord_io._format_narration_step("", []) == ""


@pytest.mark.asyncio
async def test_activity_reporter_shows_only_latest_step_and_deletes_after_delay() -> None:
    # The status block does not accumulate: only the most recent step is shown,
    # replacing the previous one in place.
    channel = RecordingStatusChannel()
    reporter = discord_io.DiscordActivityReporter(
        cast(discord.abc.Messageable, channel),
        min_interval_seconds=0.0,
    )

    await reporter.commit_step("Let me pull it up.", ["browse_tools"])
    await reporter.commit_step("Enabled it.", ["make_chart"])
    await reporter.finish()

    message = channel.messages[0]
    assert message.deleted is True
    assert message.delete_delays == [3.0]
    assert message.content == "Enabled it.\n-# Make Chart"
    assert "Let me pull it up." not in message.content
    assert "Looking for the right tool" not in message.content


@pytest.mark.asyncio
async def test_activity_reporter_shows_still_thinking_after_idle() -> None:
    channel = RecordingStatusChannel()
    released = asyncio.Event()

    async def fake_sleep(delay: float) -> None:
        await released.wait()

    reporter = discord_io.DiscordActivityReporter(
        cast(discord.abc.Messageable, channel),
        min_interval_seconds=0.0,
        sleep=fake_sleep,
        idle_nudge_seconds=30.0,
        stale_heartbeat_seconds=0.0,
    )

    await reporter.update("Using publish_doc...")
    assert channel.messages[0].content == "Using publish_doc..."

    idle_task = reporter._idle_task
    assert idle_task is not None
    released.set()
    await idle_task

    assert channel.messages[0].content == "Still thinking… (30s)"
    await reporter.finish()


@pytest.mark.asyncio
async def test_activity_reporter_idle_nudge_keeps_narration() -> None:
    channel = RecordingStatusChannel()
    released = asyncio.Event()

    async def fake_sleep(delay: float) -> None:
        await released.wait()

    reporter = discord_io.DiscordActivityReporter(
        cast(discord.abc.Messageable, channel),
        min_interval_seconds=0.0,
        sleep=fake_sleep,
        idle_nudge_seconds=30.0,
        stale_heartbeat_seconds=0.0,
    )

    await reporter.commit_step("Let me pull that up.", ["browse_tools"])
    assert channel.messages[0].content == "Let me pull that up.\n-# Looking for the right tool"

    idle_task = reporter._idle_task
    assert idle_task is not None
    released.set()
    await idle_task

    assert channel.messages[0].content == "Let me pull that up.\n-# still thinking… (30s)"
    await reporter.finish()


@pytest.mark.asyncio
async def test_activity_reporter_stale_heartbeat_ticks_elapsed() -> None:
    channel = RecordingStatusChannel()
    # Handshake sleep: each call parks (sets `entered`) until the test sets `resume`.
    entered = asyncio.Event()
    resume = asyncio.Event()

    async def fake_sleep(delay: float) -> None:
        entered.set()
        await resume.wait()
        resume.clear()

    reporter = discord_io.DiscordActivityReporter(
        cast(discord.abc.Messageable, channel),
        min_interval_seconds=0.0,
        sleep=fake_sleep,
        idle_nudge_seconds=30.0,
        stale_heartbeat_seconds=15.0,
    )

    async def wait_parked() -> None:
        await entered.wait()
        entered.clear()

    await reporter.update("Using publish_doc...")

    # Watcher spawned: parked in the idle-nudge sleep.
    await wait_parked()
    # Release it -> first stale flip after 30s idle.
    resume.set()
    await wait_parked()
    assert channel.messages[0].content == "Still thinking… (30s)"

    # Release one heartbeat -> +15s.
    resume.set()
    await wait_parked()
    assert channel.messages[0].content == "Still thinking… (45s)"

    # And another, crossing the minute boundary.
    resume.set()
    await wait_parked()
    assert channel.messages[0].content == "Still thinking… (1m00s)"

    await reporter.finish()


@pytest.mark.asyncio
async def test_activity_reporter_commits_footer_only_step_without_narration() -> None:
    channel = RecordingStatusChannel()
    reporter = discord_io.DiscordActivityReporter(
        cast(discord.abc.Messageable, channel),
        min_interval_seconds=0.0,
    )

    await reporter.commit_step("", ["browse_tools"])
    await reporter.finish()

    assert channel.messages[0].content == "-# Looking for the right tool"
    assert channel.messages[0].deleted is True
    assert channel.messages[0].delete_delays == [3.0]


@pytest.mark.asyncio
async def test_activity_reporter_deletes_when_no_step_committed() -> None:
    channel = RecordingStatusChannel()
    reporter = discord_io.DiscordActivityReporter(
        cast(discord.abc.Messageable, channel),
        min_interval_seconds=0.0,
    )

    await reporter(ActivityUpdate(label="Thinking..."))
    await reporter.finish()

    assert channel.messages[0].deleted is True
    assert channel.messages[0].delete_delays == [None]


@pytest.mark.asyncio
async def test_activity_reporter_notifies_committed_message_once() -> None:
    channel = RecordingStatusChannel()
    seen: list[int] = []

    async def on_committed(message_id: int) -> None:
        seen.append(message_id)

    reporter = discord_io.DiscordActivityReporter(
        cast(discord.abc.Messageable, channel),
        min_interval_seconds=0.0,
        on_committed_message=on_committed,
    )

    await reporter.commit_step("a", ["t1"])
    await reporter.commit_step("b", ["t2"])
    await reporter.finish()

    assert seen == [channel.messages[0].id]
    assert reporter.committed_message_id == channel.messages[0].id


@pytest.mark.asyncio
async def test_activity_reporter_suppresses_status_line_while_narration_shown() -> None:
    # While the model's latest sentence is shown, the live status label is not
    # appended beneath it; the block stays a single clean step.
    channel = RecordingStatusChannel()
    reporter = discord_io.DiscordActivityReporter(
        cast(discord.abc.Messageable, channel),
        min_interval_seconds=0.0,
    )

    await reporter.commit_step("Working.", ["t1"])
    await reporter(ActivityUpdate(label="Thinking..."))
    assert channel.messages[0].content == "Working.\n-# T1"
    assert "Thinking..." not in channel.messages[0].content

    await reporter.finish()
    assert "Thinking..." not in channel.messages[0].content
    assert "Working." in channel.messages[0].content
    assert channel.messages[0].delete_delays == [3.0]


@pytest.mark.asyncio
async def test_activity_reporter_shows_latest_step_only_over_many_steps() -> None:
    channel = RecordingStatusChannel()
    reporter = discord_io.DiscordActivityReporter(
        cast(discord.abc.Messageable, channel),
        min_interval_seconds=0.0,
    )

    for i in range(40):
        await reporter.commit_step(f"step {i}", [f"tool{i}"])
    await reporter.finish()

    content = channel.messages[0].content
    assert len(content) <= discord_io.DISCORD_MAX_LENGTH
    assert content == "step 39\n-# Tool39"
    assert "step 0" not in content
    assert "(earlier steps trimmed)" not in content


@pytest.mark.asyncio
async def test_send_response_reply_reference_degrades_for_deleted_trigger() -> None:
    # A real discord.Message reference is converted to a MessageReference with
    # fail_if_not_exists=False, so a trigger deleted mid-turn degrades to a
    # plain message instead of failing the whole send with a 400.
    channel = RecordingChannel()
    channel.fail_next_file_upload = False
    converted = object()
    captured_kwargs: dict[str, object] = {}

    class FakeMessage:
        def to_reference(self, **kwargs: object) -> object:
            captured_kwargs.update(kwargs)
            return converted

    await send_response(
        cast(discord.abc.Messageable, channel),
        "hello",
        reference=cast(discord.Message, FakeMessage()),
    )

    assert captured_kwargs == {"fail_if_not_exists": False}
    assert channel.calls[0]["reference"] is converted


@pytest.mark.asyncio
async def test_activity_reporter_reference_degrades_for_deleted_trigger() -> None:
    channel = RecordingStatusChannel()
    converted = object()

    class FakeMessage:
        def to_reference(self, **kwargs: object) -> object:
            assert kwargs == {"fail_if_not_exists": False}
            return converted

    reporter = discord_io.DiscordActivityReporter(
        cast(discord.abc.Messageable, channel),
        reference=cast(discord.Message, FakeMessage()),
        min_interval_seconds=0.0,
    )

    await reporter(ActivityUpdate(label="Thinking..."))
    await reporter.finish()

    assert channel.send_kwargs[0]["reference"] is converted


# --- plan checklist rendering ----------------------------------------------------


def _plan_steps() -> list[dict[str, str]]:
    return [
        {"content": "fetch specs", "status": "completed"},
        {"content": "render diagram", "status": "in_progress"},
        {"content": "attach file", "status": "pending"},
    ]


_PLAN_LINES = "-# ✓ 1 done · fetch specs\n-# → render diagram\n-# ○ attach file"


def test_format_plan_block_renders_small_list() -> None:
    assert discord_io._format_plan_block(_plan_steps(), 2000) == _PLAN_LINES


def test_format_plan_block_folds_pending_overflow() -> None:
    steps = [{"content": f"step {i}", "status": "pending"} for i in range(6)]
    lines = discord_io._format_plan_block(steps, 2000).splitlines()
    assert lines[:3] == ["-# ○ step 0", "-# ○ step 1", "-# ○ step 2"]
    assert lines[3] == "-# +3 more"


def test_format_plan_block_degrades_within_budget() -> None:
    steps = (
        [{"content": "d" * 200, "status": "completed"} for _ in range(10)]
        + [{"content": "a" * 200, "status": "in_progress"}]
        + [{"content": "p" * 200, "status": "pending"} for _ in range(19)]
    )
    block = discord_io._format_plan_block(steps, 600)
    assert 0 < len(block) <= 600
    assert "✓ 10 done" in block
    assert "→" in block


def test_format_plan_block_empty_below_min_budget() -> None:
    assert discord_io._format_plan_block(_plan_steps(), 10) == ""


def test_format_plan_block_empty_for_no_steps() -> None:
    assert discord_io._format_plan_block([], 2000) == ""


@pytest.mark.asyncio
async def test_activity_reporter_renders_plan_below_status() -> None:
    channel = RecordingStatusChannel()
    reporter = discord_io.DiscordActivityReporter(
        cast(discord.abc.Messageable, channel),
        min_interval_seconds=0.0,
    )

    await reporter(ActivityUpdate(label="Thinking..."))
    await reporter.update_plan(_plan_steps())

    assert channel.messages[0].content == f"Thinking...\n{_PLAN_LINES}"
    await reporter.finish()


@pytest.mark.asyncio
async def test_activity_reporter_renders_plan_below_narration_step() -> None:
    channel = RecordingStatusChannel()
    reporter = discord_io.DiscordActivityReporter(
        cast(discord.abc.Messageable, channel),
        min_interval_seconds=0.0,
    )

    await reporter.commit_step("Working on it.", ["browse_tools"])
    await reporter.update_plan(_plan_steps())

    assert channel.messages[0].content == (
        f"Working on it.\n-# Looking for the right tool\n{_PLAN_LINES}"
    )
    await reporter.finish()


@pytest.mark.asyncio
async def test_activity_reporter_plan_survives_stale_render() -> None:
    channel = RecordingStatusChannel()
    released = asyncio.Event()

    async def fake_sleep(delay: float) -> None:
        await released.wait()

    reporter = discord_io.DiscordActivityReporter(
        cast(discord.abc.Messageable, channel),
        min_interval_seconds=0.0,
        sleep=fake_sleep,
        idle_nudge_seconds=30.0,
        stale_heartbeat_seconds=0.0,
    )

    await reporter.commit_step("Let me pull that up.", ["browse_tools"])
    await reporter.update_plan(_plan_steps())

    idle_task = reporter._idle_task
    assert idle_task is not None
    released.set()
    await idle_task

    assert channel.messages[0].content == (
        f"Let me pull that up.\n-# still thinking… (30s)\n{_PLAN_LINES}"
    )
    await reporter.finish()


@pytest.mark.asyncio
async def test_activity_reporter_update_plan_clears_stale() -> None:
    channel = RecordingStatusChannel()
    released = asyncio.Event()

    async def fake_sleep(delay: float) -> None:
        await released.wait()

    reporter = discord_io.DiscordActivityReporter(
        cast(discord.abc.Messageable, channel),
        min_interval_seconds=0.0,
        sleep=fake_sleep,
        idle_nudge_seconds=30.0,
        stale_heartbeat_seconds=0.0,
    )

    await reporter.commit_step("Working.", ["browse_tools"])
    idle_task = reporter._idle_task
    assert idle_task is not None
    released.set()
    await idle_task
    assert "still thinking" in channel.messages[0].content

    released.clear()
    await reporter.update_plan(_plan_steps())

    assert "still thinking" not in channel.messages[0].content
    assert "-# → render diagram" in channel.messages[0].content
    await reporter.finish()


@pytest.mark.asyncio
async def test_activity_reporter_finish_drops_plan_from_final_log() -> None:
    channel = RecordingStatusChannel()
    reporter = discord_io.DiscordActivityReporter(
        cast(discord.abc.Messageable, channel),
        min_interval_seconds=0.0,
    )

    await reporter.commit_step("Working on it.", ["browse_tools"])
    await reporter.update_plan(_plan_steps())
    await reporter.finish()

    message = channel.messages[0]
    assert message.content == "Working on it.\n-# Looking for the right tool"
    assert message.deleted is True
    assert message.delete_delays == [3.0]


@pytest.mark.asyncio
async def test_activity_reporter_plan_only_turn_deletes_immediately() -> None:
    # A plan alone never marks the surface committed: without a narration step the
    # throwaway message is deleted with no linger delay, same as status-only turns.
    channel = RecordingStatusChannel()
    reporter = discord_io.DiscordActivityReporter(
        cast(discord.abc.Messageable, channel),
        min_interval_seconds=0.0,
    )

    await reporter.update_plan(_plan_steps())
    assert channel.messages[0].content == _PLAN_LINES

    await reporter.finish()

    assert channel.messages[0].deleted is True
    assert channel.messages[0].delete_delays == [None]


@pytest.mark.asyncio
async def test_activity_reporter_update_plan_after_finish_noops() -> None:
    channel = RecordingStatusChannel()
    reporter = discord_io.DiscordActivityReporter(
        cast(discord.abc.Messageable, channel),
        min_interval_seconds=0.0,
    )

    await reporter.commit_step("Working.", ["browse_tools"])
    await reporter.finish()
    edits_before = list(channel.messages[0].edits)

    await reporter.update_plan(_plan_steps())

    assert channel.messages[0].edits == edits_before
