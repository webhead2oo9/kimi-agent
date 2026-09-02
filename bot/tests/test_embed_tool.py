from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from workspace import WorkspaceManager
from tools.embeds import (
    EmbedAttachment,
    EmbedSpec,
    build_embed_payload,
    embed_transcript_summary,
    init_embed_tool,
)
from tools.registry import MessageContext, ToolRegistry, TurnOutbox
from trust.tiers import TrustTier


def _wm(tmp_path: Path) -> WorkspaceManager:
    return WorkspaceManager(base_dir=tmp_path)


def _ctx(
    *,
    output_files: list[str] | None = None,
    activated: set[str] | None = None,
) -> MessageContext:
    return MessageContext(
        user_id="u1",
        user_name="Builder",
        guild_id="g1",
        channel_id="c1",
        thread_id=None,
        trust_tier=TrustTier.MEMBER,
        context_key="g1:c1:main",
        trigger_discord_message_id="m1",
        activated_tools=activated or set(),
        outbox=TurnOutbox(output_files=tuple(output_files or ())),
    )


def _payload(args: dict, tmp_path: Path, ctx: MessageContext | None = None):
    return build_embed_payload(args, ctx or _ctx(), _wm(tmp_path))


# ── registration ────────────────────────────────────────────────────────────


def test_tool_is_searchable_and_hidden_until_activated(tmp_path: Path) -> None:
    registry = ToolRegistry()
    init_embed_tool(registry, _wm(tmp_path))

    visible = [s["name"] for s in registry.get_tool_schemas(TrustTier.MEMBER)]
    assert "build_discord_embed" not in visible

    catalog = [entry.name for entry in registry.catalog(TrustTier.MEMBER)]
    assert "build_discord_embed" in catalog

    raw = asyncio.run(registry.dispatch("build_discord_embed", {"title": "Hi"}, _ctx()))
    assert "browse_tools" in json.loads(raw)["error"]


# ── happy path + spec shape ──────────────────────────────────────────────────


def test_full_valid_embed_builds_spec(tmp_path: Path) -> None:
    spec, attachment = _payload(
        {
            "title": "Patch Notes",
            "description": "What changed",
            "url": "https://example.com/notes",
            "color": "#5865F2",
            "author_name": "Kimi",
            "footer_text": "v1.2",
            "thumbnail_url": "https://example.com/thumb.png",
            "fields": [
                {"name": "Added", "value": "Embeds", "inline": True},
                {"name": "Fixed", "value": "Bugs"},
            ],
            "timestamp": True,
        },
        tmp_path,
    )
    assert attachment is None
    assert isinstance(spec, EmbedSpec)
    assert spec.title == "Patch Notes"
    assert spec.description == "What changed"
    assert spec.url == "https://example.com/notes"
    assert spec.color == 0x5865F2
    assert spec.author_name == "Kimi"
    assert spec.footer_text == "v1.2"
    assert spec.thumbnail_url == "https://example.com/thumb.png"
    assert spec.fields == (("Added", "Embeds", True), ("Fixed", "Bugs", False))
    assert spec.timestamp is True
    assert spec.image is None


# ── length / count limits ────────────────────────────────────────────────────


def test_title_too_long_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="title"):
        _payload({"title": "x" * 257}, tmp_path)


def test_description_too_long_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="description"):
        _payload({"description": "x" * 4097}, tmp_path)


def test_too_many_fields_rejected(tmp_path: Path) -> None:
    fields = [{"name": f"n{i}", "value": "v"} for i in range(26)]
    with pytest.raises(ValueError, match="25 fields"):
        _payload({"fields": fields}, tmp_path)


def test_field_value_required(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="value"):
        _payload({"fields": [{"name": "n", "value": "  "}]}, tmp_path)


def test_field_value_too_long_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="1024"):
        _payload({"fields": [{"name": "n", "value": "x" * 1025}]}, tmp_path)


def test_total_character_budget_enforced(tmp_path: Path) -> None:
    # title (256) + description (4096) + one field name(256)+value(1024) = 5632; push past 6000.
    with pytest.raises(ValueError, match="6000"):
        _payload(
            {
                "title": "t" * 256,
                "description": "d" * 4096,
                "fields": [
                    {"name": "n" * 256, "value": "v" * 1024},
                    {"name": "n" * 256, "value": "v" * 256},
                ],
            },
            tmp_path,
        )


# ── color parsing ────────────────────────────────────────────────────────────


def test_color_accepts_hex_0x_and_int(tmp_path: Path) -> None:
    assert _payload({"title": "a", "color": "#FF0000"}, tmp_path)[0].color == 0xFF0000
    assert _payload({"title": "a", "color": "0x00FF00"}, tmp_path)[0].color == 0x00FF00
    assert _payload({"title": "a", "color": 255}, tmp_path)[0].color == 255


def test_invalid_color_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="color"):
        _payload({"title": "a", "color": "notacolor"}, tmp_path)


def test_color_out_of_range_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="color"):
        _payload({"title": "a", "color": 0x1000000}, tmp_path)


# ── url rules ────────────────────────────────────────────────────────────────


def test_non_https_url_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="https"):
        _payload({"title": "a", "url": "http://example.com"}, tmp_path)


def test_url_length_capped(tmp_path: Path) -> None:
    long_url = "https://example.com/" + "a" * 2048
    with pytest.raises(ValueError, match="2048"):
        _payload({"title": "a", "image_url": long_url}, tmp_path)


# ── presence / image source rules ────────────────────────────────────────────


def test_empty_embed_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="at least"):
        _payload({}, tmp_path)


def test_image_url_alone_satisfies_presence(tmp_path: Path) -> None:
    spec, attachment = _payload({"image_url": "https://example.com/x.png"}, tmp_path)
    assert spec.image == "https://example.com/x.png"
    assert attachment is None


def test_both_image_sources_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="both"):
        _payload(
            {
                "image_url": "https://example.com/x.png",
                "image_workspace_path": "chart.png",
            },
            tmp_path,
        )


# ── workspace image ──────────────────────────────────────────────────────────


def _write_user_image(tmp_path: Path, ctx: MessageContext, name: str) -> None:
    wm = _wm(tmp_path)
    target = wm.user_files_dir(ctx.workspace_key) / name
    target.write_bytes(b"\x89PNG\r\n")


def test_workspace_image_becomes_attachment_reference(tmp_path: Path) -> None:
    ctx = _ctx()
    _write_user_image(tmp_path, ctx, "chart.png")
    spec, attachment = build_embed_payload(
        {"title": "Chart", "image_workspace_path": "chart.png"},
        ctx,
        _wm(tmp_path),
    )
    assert spec.image == "attachment://chart.png"
    assert isinstance(attachment, EmbedAttachment)
    assert attachment.filename == "chart.png"
    assert Path(attachment.path).name == "chart.png"
    assert Path(attachment.path).is_relative_to(Path(attachment.root))


def test_missing_workspace_image_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not found"):
        _payload({"title": "x", "image_workspace_path": "nope.png"}, tmp_path)


def test_basename_collision_with_queued_file_rejected(tmp_path: Path) -> None:
    ctx = _ctx()
    _write_user_image(tmp_path, ctx, "chart.png")
    queued = _wm(tmp_path).user_files_dir(ctx.workspace_key) / "other" / "chart.png"
    ctx.update_outbox(output_files=(str(queued),))
    with pytest.raises(ValueError, match="rename"):
        build_embed_payload(
            {"title": "x", "image_workspace_path": "chart.png"},
            ctx,
            _wm(tmp_path),
        )


# ── handler: ctx mutation, ack, replace, failure isolation ───────────────────


def _dispatch(registry: ToolRegistry, args: dict, ctx: MessageContext) -> dict:
    raw = asyncio.run(registry.dispatch("build_discord_embed", args, ctx))
    return json.loads(raw)


def test_handler_sets_ctx_embed_and_returns_ack(tmp_path: Path) -> None:
    registry = ToolRegistry()
    init_embed_tool(registry, _wm(tmp_path))
    ctx = _ctx(activated={"build_discord_embed"})

    ack = _dispatch(registry, {"title": "Hello", "color": "#000000"}, ctx)

    assert ack == {"queued": True, "image": None}
    assert ctx.outbox.embed is not None
    assert ctx.outbox.embed.title == "Hello"
    assert ctx.outbox.embed_attachment is None


def test_handler_ack_reports_attachment_image(tmp_path: Path) -> None:
    registry = ToolRegistry()
    init_embed_tool(registry, _wm(tmp_path))
    ctx = _ctx(activated={"build_discord_embed"})
    _write_user_image(tmp_path, ctx, "chart.png")

    ack = _dispatch(registry, {"image_workspace_path": "chart.png"}, ctx)

    assert ack == {"queued": True, "image": "attachment://chart.png"}
    assert ctx.outbox.embed_attachment is not None


def test_second_call_replaces_embed_and_attachment(tmp_path: Path) -> None:
    registry = ToolRegistry()
    init_embed_tool(registry, _wm(tmp_path))
    ctx = _ctx(activated={"build_discord_embed"})
    _write_user_image(tmp_path, ctx, "chart.png")

    _dispatch(registry, {"image_workspace_path": "chart.png"}, ctx)
    assert ctx.outbox.embed_attachment is not None

    _dispatch(registry, {"title": "Plain"}, ctx)
    assert ctx.outbox.embed is not None
    assert ctx.outbox.embed.title == "Plain"
    assert ctx.outbox.embed_attachment is None


def test_validation_failure_leaves_ctx_untouched(tmp_path: Path) -> None:
    registry = ToolRegistry()
    init_embed_tool(registry, _wm(tmp_path))
    ctx = _ctx(activated={"build_discord_embed"})

    _dispatch(registry, {"title": "Good"}, ctx)
    prior = ctx.outbox.embed

    err = _dispatch(registry, {"title": "x" * 300}, ctx)
    assert "error" in err
    assert ctx.outbox.embed is prior
    assert ctx.outbox.output_files == ()


# ── transcript summary (for embed-only replies) ──────────────────────────────


def test_summary_title_and_description() -> None:
    spec = EmbedSpec(title="Patch Notes", description="What changed\nmore detail")
    assert embed_transcript_summary(spec) == "[embed] Patch Notes: What changed"


def test_summary_title_only() -> None:
    assert embed_transcript_summary(EmbedSpec(title="Hello")) == "[embed] Hello"


def test_summary_falls_back_to_description_then_author_then_field() -> None:
    assert embed_transcript_summary(EmbedSpec(description="Body")) == "[embed] Body"
    assert embed_transcript_summary(EmbedSpec(author_name="Kimi")) == "[embed] Kimi"
    assert embed_transcript_summary(EmbedSpec(fields=(("Score", "9", False),))) == "[embed] Score"


def test_summary_image_only() -> None:
    assert embed_transcript_summary(EmbedSpec(image="https://e.com/x.png")) == "[embed] (image)"
