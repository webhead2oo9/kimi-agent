"""Validated first-party chart and Mermaid rendering tool."""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import shutil
import stat
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from tools._common import tool_error
from tools.output_queue import AttachmentLimitError, enqueue_output_file
from tools.registry import BudgetName, MessageContext, ToolBudgetSpec, ToolRegistry
from tools.workspace.common import UserLocks, workspace_activity
from trust.tiers import TrustTier
from web_browser.visual_service import (
    AxisScale,
    ChartType,
    OverlapMode,
    ScatterPoint,
    VisualRenderRequest,
    VisualSeries,
    VisualService,
    VisualServiceError,
)
from workspace import WorkspaceManager

CHART_TOOL_NAME = "render_chart"
DIAGRAM_TOOL_NAME = "render_diagram"
MAX_RENDERS_PER_TURN = 4
MAX_SERIES = 8
MAX_POINTS_PER_SERIES = 250
MAX_TOTAL_POINTS = 1000
MAX_BAR_CATEGORIES = 50
MAX_ABS_VALUE = 1_000_000_000_000_000.0
MAX_TEXT_CHARS = 200
MAX_AXIS_LABEL_CHARS = 100
MAX_ALT_TEXT_CHARS = 1000
MAX_MERMAID_CHARS = 12_000
MAX_MERMAID_LINES = 300
EXPECTED_WIDTH = 1200
EXPECTED_HEIGHT = 675

_CHART_FIELDS = frozenset(
    {
        "kind",
        "chart_type",
        "title",
        "x_label",
        "y_label",
        "x_scale",
        "y_scale",
        "overlap_mode",
        "alt_text",
        "categories",
        "series",
    }
)
_MERMAID_FIELDS = frozenset({"kind", "title", "alt_text", "source"})
_ALLOWED_CHART_TYPES = frozenset({"bar", "line", "scatter"})
_ALLOWED_AXIS_SCALES = frozenset({"linear", "symlog"})
_ALLOWED_OVERLAP_MODES = frozenset({"none", "count"})
_MERMAID_HEADER_RE = re.compile(
    r"^(?:(?:flowchart|graph)\s+(?:TB|TD|BT|RL|LR)|sequenceDiagram|stateDiagram-v2|"
    r"classDiagram|erDiagram)\s*$",
    re.IGNORECASE,
)
_MERMAID_FORBIDDEN_RE = re.compile(
    r"(?:%%\{|\bclick\b|\bhref\b|\b(?:https?|wss?|ftp)://|\b(?:data|file):|"
    r"\bwww\.|\burl\s*\(|<\s*/?\s*[a-z!]|@\{|\b(?:img|icon)\s*:|"
    r"^\s*(?:style|classDef|linkStyle|theme|font)\b|"
    r"\bfont-(?:family|size|style|weight)\b|\bcss\b|:::)",
    re.IGNORECASE | re.MULTILINE,
)


@dataclass(frozen=True, slots=True)
class VisualToolConfig:
    max_png_bytes: int = 8 * 1024 * 1024
    max_attachments: int = 5


def _unknown_fields(args: dict[str, Any], allowed: frozenset[str], scope: str) -> None:
    unknown = sorted(set(args) - allowed)
    if unknown:
        raise ValueError(f"unknown {scope} field(s): {', '.join(unknown)}")


def _without_neutral_fields(
    value: dict[str, Any], neutral_fields: dict[str, object]
) -> dict[str, Any]:
    """Drop harmless placeholders emitted for inactive flat-schema branches."""
    normalized = dict(value)
    for name, neutral in neutral_fields.items():
        if normalized.get(name) == neutral:
            normalized.pop(name, None)
    return normalized


def _text(
    value: object,
    name: str,
    *,
    maximum: int,
    required: bool = False,
) -> str:
    if value is None:
        text = ""
    elif not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    else:
        text = value.strip()
    if required and not text:
        raise ValueError(f"{name} is required")
    if len(text) > maximum:
        raise ValueError(f"{name} must be {maximum} characters or fewer")
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise ValueError(f"{name} contains unsupported control characters")
    return text


def _finite_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{name} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    if abs(number) > MAX_ABS_VALUE:
        raise ValueError(f"{name} must be between {-MAX_ABS_VALUE:g} and {MAX_ABS_VALUE:g}")
    return number


def _list(value: object, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be an array")
    return value


def _validate_mermaid(args: dict[str, Any], title: str, alt_text: str) -> VisualRenderRequest:
    args = _without_neutral_fields(
        args,
        {"chart_type": "bar", "x_label": "", "y_label": "", "categories": [], "series": []},
    )
    _unknown_fields(args, _MERMAID_FIELDS, "Mermaid")
    raw_source = args.get("source")
    if not isinstance(raw_source, str):
        raise ValueError("source must be a string")
    source = raw_source.strip().replace("\r\n", "\n").replace("\r", "\n")
    if not source:
        raise ValueError("source is required")
    if len(source) > MAX_MERMAID_CHARS:
        raise ValueError(f"source must be {MAX_MERMAID_CHARS} characters or fewer")
    if any(ord(char) < 32 and char not in {"\n", "\t"} for char in source):
        raise ValueError("source contains unsupported control characters")
    lines = source.splitlines()
    if len(lines) > MAX_MERMAID_LINES:
        raise ValueError(f"source must be {MAX_MERMAID_LINES} lines or fewer")
    if source.startswith("---") or any(line.strip() == "---" for line in lines):
        raise ValueError("Mermaid frontmatter is not allowed")
    header = next(
        (line.strip() for line in lines if line.strip() and not line.lstrip().startswith("%%")), ""
    )
    if not _MERMAID_HEADER_RE.fullmatch(header):
        raise ValueError("source must begin with an allowed Mermaid diagram header")
    forbidden = _MERMAID_FORBIDDEN_RE.search(source)
    if forbidden is not None:
        raise ValueError("source contains a forbidden Mermaid directive or external content")
    return VisualRenderRequest(kind="mermaid", title=title, alt_text=alt_text, source=source)


def _validate_categories(value: object, chart_type: str) -> tuple[str, ...]:
    raw = _list(value, "categories")
    maximum = MAX_BAR_CATEGORIES if chart_type == "bar" else MAX_POINTS_PER_SERIES
    if not raw:
        raise ValueError("categories must not be empty")
    if len(raw) > maximum:
        raise ValueError(f"categories may contain at most {maximum} items")
    categories = tuple(
        _text(item, f"categories[{index}]", maximum=MAX_AXIS_LABEL_CHARS, required=True)
        for index, item in enumerate(raw)
    )
    if len(set(categories)) != len(categories):
        raise ValueError("categories must be unique")
    return categories


def _validate_chart_series(
    value: object,
    chart_type: ChartType,
    category_count: int,
) -> tuple[VisualSeries, ...]:
    raw_series = _list(value, "series")
    if not raw_series:
        raise ValueError("series must not be empty")
    if len(raw_series) > MAX_SERIES:
        raise ValueError(f"series may contain at most {MAX_SERIES} items")
    series: list[VisualSeries] = []
    total_points = 0
    for series_index, raw in enumerate(raw_series):
        if not isinstance(raw, dict):
            raise ValueError(f"series[{series_index}] must be an object")
        item = cast(dict[str, Any], raw)
        name = _text(
            item.get("name"),
            f"series[{series_index}].name",
            maximum=MAX_AXIS_LABEL_CHARS,
            required=True,
        )
        if chart_type == "scatter":
            item = _without_neutral_fields(item, {"values": []})
            _unknown_fields(item, frozenset({"name", "points"}), f"series[{series_index}]")
            raw_points = _list(item.get("points"), f"series[{series_index}].points")
            if not raw_points:
                raise ValueError(f"series[{series_index}].points must not be empty")
            if len(raw_points) > MAX_POINTS_PER_SERIES:
                raise ValueError(
                    f"series[{series_index}].points may contain at most "
                    f"{MAX_POINTS_PER_SERIES} items"
                )
            points: list[ScatterPoint] = []
            for point_index, raw_point in enumerate(raw_points):
                if not isinstance(raw_point, dict):
                    raise ValueError(
                        f"series[{series_index}].points[{point_index}] must be an object"
                    )
                point = cast(dict[str, Any], raw_point)
                _unknown_fields(
                    point,
                    frozenset({"x", "y", "label"}),
                    f"series[{series_index}].points[{point_index}]",
                )
                points.append(
                    ScatterPoint(
                        x=_finite_number(
                            point.get("x"), f"series[{series_index}].points[{point_index}].x"
                        ),
                        y=_finite_number(
                            point.get("y"), f"series[{series_index}].points[{point_index}].y"
                        ),
                        label=_text(
                            point.get("label"),
                            f"series[{series_index}].points[{point_index}].label",
                            maximum=MAX_AXIS_LABEL_CHARS,
                        ),
                    )
                )
            total_points += len(points)
            series.append(VisualSeries(name=name, points=tuple(points)))
            continue

        item = _without_neutral_fields(item, {"points": []})
        _unknown_fields(item, frozenset({"name", "values"}), f"series[{series_index}]")
        raw_values = _list(item.get("values"), f"series[{series_index}].values")
        if len(raw_values) != category_count:
            raise ValueError(
                f"series[{series_index}].values must match the {category_count} categories"
            )
        if len(raw_values) > MAX_POINTS_PER_SERIES:
            raise ValueError(
                f"series[{series_index}].values may contain at most {MAX_POINTS_PER_SERIES} items"
            )
        values = tuple(
            _finite_number(item_value, f"series[{series_index}].values[{value_index}]")
            for value_index, item_value in enumerate(raw_values)
        )
        total_points += len(values)
        series.append(VisualSeries(name=name, values=values))
    if total_points > MAX_TOTAL_POINTS:
        raise ValueError(f"visual may contain at most {MAX_TOTAL_POINTS} total points")
    return tuple(series)


def validate_visual_request(args: dict[str, Any]) -> VisualRenderRequest:
    kind = args.get("kind")
    if kind not in {"chart", "mermaid"}:
        raise ValueError("kind must be chart or mermaid")
    title = _text(args.get("title"), "title", maximum=MAX_TEXT_CHARS)
    alt_text = _text(args.get("alt_text"), "alt_text", maximum=MAX_ALT_TEXT_CHARS, required=True)
    if kind == "mermaid":
        return _validate_mermaid(args, title, alt_text)

    args = _without_neutral_fields(args, {"source": ""})
    _unknown_fields(args, _CHART_FIELDS, "chart")
    chart_type_raw = args.get("chart_type", "bar")
    if chart_type_raw not in _ALLOWED_CHART_TYPES:
        raise ValueError("chart_type must be bar, line, or scatter")
    chart_type = cast(ChartType, chart_type_raw)
    x_label = _text(args.get("x_label"), "x_label", maximum=MAX_AXIS_LABEL_CHARS)
    y_label = _text(args.get("y_label"), "y_label", maximum=MAX_AXIS_LABEL_CHARS)
    x_scale_raw = args.get("x_scale", "linear")
    y_scale_raw = args.get("y_scale", "linear")
    overlap_mode_raw = args.get("overlap_mode", "none")
    if x_scale_raw not in _ALLOWED_AXIS_SCALES:
        raise ValueError("x_scale must be linear or symlog")
    if y_scale_raw not in _ALLOWED_AXIS_SCALES:
        raise ValueError("y_scale must be linear or symlog")
    if overlap_mode_raw not in _ALLOWED_OVERLAP_MODES:
        raise ValueError("overlap_mode must be none or count")
    x_scale = cast(AxisScale, x_scale_raw)
    y_scale = cast(AxisScale, y_scale_raw)
    overlap_mode = cast(OverlapMode, overlap_mode_raw)
    if chart_type == "scatter":
        args = _without_neutral_fields(args, {"categories": []})
        if "categories" in args:
            raise ValueError("categories is not allowed for scatter charts")
        categories: tuple[str, ...] = ()
    else:
        if x_scale != "linear" or y_scale != "linear" or overlap_mode != "none":
            raise ValueError(
                "x_scale, y_scale, and overlap_mode support non-default values only for scatter charts"
            )
        categories = _validate_categories(args.get("categories"), chart_type)
    series = _validate_chart_series(args.get("series"), chart_type, len(categories))
    return VisualRenderRequest(
        kind="chart",
        chart_type=chart_type,
        title=title,
        x_label=x_label,
        y_label=y_label,
        alt_text=alt_text,
        categories=categories,
        series=series,
        x_scale=x_scale,
        y_scale=y_scale,
        overlap_mode=overlap_mode,
    )


def verify_rendered_png(path: Path, max_bytes: int) -> tuple[int, int, int]:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size <= 0:
            raise ValueError("renderer output is not a regular PNG file")
        if metadata.st_size > max_bytes:
            raise ValueError("renderer output exceeded the PNG size limit")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            payload = stream.read(max_bytes + 1)
    finally:
        os.close(descriptor)
    if len(payload) != metadata.st_size or len(payload) > max_bytes:
        raise ValueError("renderer output exceeded the PNG size limit")
    width, height = _validate_png_payload(payload)
    return metadata.st_size, width, height


def _validate_png_payload(payload: bytes) -> tuple[int, int]:
    if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("renderer output has invalid PNG magic bytes")
    offset = 8
    width = height = channels = 0
    idat = bytearray()
    saw_ihdr = saw_idat = saw_iend = False
    idat_finished = False
    while offset < len(payload):
        if len(payload) - offset < 12:
            raise ValueError("renderer output is a truncated PNG")
        length = struct.unpack(">I", payload[offset : offset + 4])[0]
        chunk_end = offset + 12 + length
        if chunk_end > len(payload):
            raise ValueError("renderer output is a truncated PNG")
        chunk_type = payload[offset + 4 : offset + 8]
        chunk_data = payload[offset + 8 : offset + 8 + length]
        expected_crc = struct.unpack(">I", payload[offset + 8 + length : chunk_end])[0]
        actual_crc = zlib.crc32(chunk_type + chunk_data) & 0xFFFFFFFF
        if actual_crc != expected_crc:
            raise ValueError("renderer output has an invalid PNG checksum")
        if not saw_ihdr:
            if chunk_type != b"IHDR" or length != 13:
                raise ValueError("renderer output has an invalid PNG header")
            (
                width,
                height,
                bit_depth,
                color_type,
                compression,
                filtering,
                interlace,
            ) = struct.unpack(">IIBBBBB", chunk_data)
            if (width, height) != (EXPECTED_WIDTH, EXPECTED_HEIGHT):
                raise ValueError(
                    f"renderer output dimensions must be {EXPECTED_WIDTH}x{EXPECTED_HEIGHT}"
                )
            channels = {2: 3, 6: 4}.get(color_type, 0)
            if (
                bit_depth != 8
                or not channels
                or compression != 0
                or filtering != 0
                or interlace != 0
            ):
                raise ValueError("renderer output uses an unsupported PNG encoding")
            saw_ihdr = True
        elif chunk_type == b"IHDR":
            raise ValueError("renderer output has multiple PNG headers")
        elif chunk_type == b"IDAT":
            if idat_finished:
                raise ValueError("renderer output has non-consecutive PNG image data")
            saw_idat = True
            idat.extend(chunk_data)
            if len(idat) > len(payload):
                raise ValueError("renderer output has oversized PNG image data")
        elif chunk_type == b"IEND":
            if length != 0 or not saw_idat or chunk_end != len(payload):
                raise ValueError("renderer output has an invalid PNG end marker")
            saw_iend = True
        elif saw_idat:
            idat_finished = True
        offset = chunk_end
        if saw_iend:
            break
    if not saw_ihdr or not saw_idat or not saw_iend:
        raise ValueError("renderer output is an incomplete PNG")

    expected_size = height * (1 + width * channels)
    decompressor = zlib.decompressobj()
    try:
        decoded = decompressor.decompress(bytes(idat), expected_size + 1)
    except zlib.error as exc:
        raise ValueError("renderer output has invalid PNG image data") from exc
    if (
        len(decoded) != expected_size
        or not decompressor.eof
        or decompressor.unused_data
        or decompressor.unconsumed_tail
    ):
        raise ValueError("renderer output has invalid PNG image data")
    row_size = 1 + width * channels
    if any(decoded[offset] > 4 for offset in range(0, len(decoded), row_size)):
        raise ValueError("renderer output has an invalid PNG row filter")
    return width, height


def init_visual_tool(
    registry: ToolRegistry,
    service: VisualService,
    workspace_manager: WorkspaceManager,
    config: VisualToolConfig,
    workspace_locks: UserLocks,
) -> None:
    async def render_request(args: dict, ctx: MessageContext, *, kind: str) -> str:
        try:
            request = validate_visual_request({**args, "kind": kind})
        except ValueError as exc:
            return tool_error(str(exc))
        if not ctx.context_key:
            return tool_error("visuals can only be rendered in a conversation context")
        if ctx.budget_remaining(BudgetName.VISUAL_RENDERS) <= 0:
            return tool_error(f"visual render limit reached ({MAX_RENDERS_PER_TURN})")
        if len(ctx.outbox.output_files) >= config.max_attachments:
            return tool_error(f"attachment limit reached ({config.max_attachments})")

        job_dir: Path | None = None
        keep_job = False
        async with workspace_activity(workspace_locks, ctx):
            if ctx.budget_remaining(BudgetName.VISUAL_RENDERS) <= 0:
                return tool_error(f"visual render limit reached ({MAX_RENDERS_PER_TURN})")
            if len(ctx.outbox.output_files) >= config.max_attachments:
                return tool_error(f"attachment limit reached ({config.max_attachments})")
            try:
                job_dir = await asyncio.to_thread(
                    workspace_manager.generated_job_dir,
                    ctx.context_key,
                    f"visual-{uuid4().hex}",
                    owner_user_id=ctx.user_id,
                )
                if not ctx.consume_budget(BudgetName.VISUAL_RENDERS):
                    return tool_error(f"visual render limit reached ({MAX_RENDERS_PER_TURN})")
                result = await service.render(request, job_dir)
                expected_output = job_dir / "render.png"
                if result.output_path != expected_output:
                    raise ValueError("renderer returned an invalid output path")
                size, width, height = await asyncio.to_thread(
                    verify_rendered_png, result.output_path, config.max_png_bytes
                )
                if (result.width, result.height) != (width, height):
                    raise ValueError("renderer result dimensions did not match the PNG")
                output_path = job_dir / f"visual-{ctx.budget_used(BudgetName.VISUAL_RENDERS)}.png"
                await asyncio.to_thread(os.replace, result.output_path, output_path)
                enqueue_output_file(
                    ctx,
                    output_path,
                    job_dir,
                    max_attachments=config.max_attachments,
                    description=request.alt_text,
                )
                keep_job = True
            except (AttachmentLimitError, OSError, ValueError, VisualServiceError) as exc:
                return tool_error(str(exc))
            finally:
                if job_dir is not None and not keep_job:
                    await asyncio.shield(asyncio.to_thread(shutil.rmtree, job_dir, True))

        payload = {
            "ok": True,
            "kind": request.kind,
            "filename": output_path.name,
            "title": request.title,
            "alt_text": request.alt_text,
            "width": width,
            "height": height,
            "bytes": size,
            "attached_to_reply": True,
        }
        if request.chart_type is not None:
            payload["chart_type"] = request.chart_type
            if request.chart_type == "scatter":
                payload["x_scale"] = request.x_scale
                payload["y_scale"] = request.y_scale
                payload["overlap_mode"] = request.overlap_mode
        return json.dumps(payload, ensure_ascii=False)

    async def render_chart(args: dict, ctx: MessageContext) -> str:
        return await render_request(args, ctx, kind="chart")

    async def render_diagram(args: dict, ctx: MessageContext) -> str:
        return await render_request(args, ctx, kind="mermaid")

    registry.register(
        name=CHART_TOOL_NAME,
        description=(
            "Render and attach one accessible fixed-style bar, line, or scatter chart as a "
            "1200x675 PNG. Bar and line charts use categories plus series values; scatter "
            "charts use series points and no categories. Scatter charts may use symmetric-log "
            "axes for extreme signed ranges and count badges for exact overlaps. Colors, "
            "patterns, typography, dimensions, and safety settings are fixed. Every successful "
            "call queues the PNG for the final Discord reply."
        ),
        parameters={
            "type": "object",
            "properties": {
                "chart_type": {
                    "type": "string",
                    "enum": ["bar", "line", "scatter"],
                    "description": "Chart form; defaults to bar when omitted.",
                },
                "title": {"type": "string", "maxLength": MAX_TEXT_CHARS},
                "x_label": {
                    "type": "string",
                    "maxLength": MAX_AXIS_LABEL_CHARS,
                    "description": "Optional chart x-axis label.",
                },
                "y_label": {
                    "type": "string",
                    "maxLength": MAX_AXIS_LABEL_CHARS,
                    "description": "Optional chart y-axis label.",
                },
                "x_scale": {
                    "type": "string",
                    "enum": ["linear", "symlog"],
                    "description": (
                        "Scatter-only x-axis scale; symlog preserves zero and negative values "
                        "while separating values across large magnitude ranges. Defaults to linear."
                    ),
                },
                "y_scale": {
                    "type": "string",
                    "enum": ["linear", "symlog"],
                    "description": (
                        "Scatter-only y-axis scale; symlog preserves zero and negative values "
                        "while separating values across large magnitude ranges. Defaults to linear."
                    ),
                },
                "overlap_mode": {
                    "type": "string",
                    "enum": ["none", "count"],
                    "description": (
                        "Scatter-only duplicate handling. count marks exact shared coordinates "
                        "with their observation count; defaults to none."
                    ),
                },
                "alt_text": {
                    "type": "string",
                    "maxLength": MAX_ALT_TEXT_CHARS,
                    "description": "Required concise description of the visual and its meaning.",
                },
                "categories": {
                    "type": "array",
                    "maxItems": MAX_POINTS_PER_SERIES,
                    "items": {"type": "string", "maxLength": MAX_AXIS_LABEL_CHARS},
                    "description": "Required for bar/line; omit for scatter.",
                },
                "series": {
                    "type": "array",
                    "maxItems": MAX_SERIES,
                    "description": (
                        "Required for charts. Bar/line series use values; scatter series use points."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string",
                                "maxLength": MAX_AXIS_LABEL_CHARS,
                            },
                            "values": {
                                "type": "array",
                                "maxItems": MAX_POINTS_PER_SERIES,
                                "items": {
                                    "type": "number",
                                    "minimum": -MAX_ABS_VALUE,
                                    "maximum": MAX_ABS_VALUE,
                                },
                            },
                            "points": {
                                "type": "array",
                                "maxItems": MAX_POINTS_PER_SERIES,
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "x": {
                                            "type": "number",
                                            "minimum": -MAX_ABS_VALUE,
                                            "maximum": MAX_ABS_VALUE,
                                        },
                                        "y": {
                                            "type": "number",
                                            "minimum": -MAX_ABS_VALUE,
                                            "maximum": MAX_ABS_VALUE,
                                        },
                                        "label": {
                                            "type": "string",
                                            "maxLength": MAX_AXIS_LABEL_CHARS,
                                        },
                                    },
                                    "required": ["x", "y"],
                                    "additionalProperties": False,
                                },
                            },
                        },
                        "required": ["name"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["alt_text", "series"],
            "additionalProperties": False,
        },
        handler=render_chart,
        min_tier=TrustTier.MEMBER,
        searchable=True,
        category="Visuals",
        budget_specs=(ToolBudgetSpec(BudgetName.VISUAL_RENDERS, MAX_RENDERS_PER_TURN),),
    )
    registry.register(
        name=DIAGRAM_TOOL_NAME,
        description=(
            "Render and attach one accessible constrained Mermaid diagram as a 1200x675 PNG. "
            "Supply Mermaid source beginning with an allowed flowchart, sequence, state, "
            "class, or entity-relationship header. Styling, HTML, links, external content, "
            "code, files, paths, and dimensions are forbidden. Every successful call queues "
            "the PNG for the final Discord reply."
        ),
        parameters={
            "type": "object",
            "properties": {
                "title": {"type": "string", "maxLength": MAX_TEXT_CHARS},
                "alt_text": {
                    "type": "string",
                    "maxLength": MAX_ALT_TEXT_CHARS,
                    "description": "Required concise description of the diagram and its meaning.",
                },
                "source": {
                    "type": "string",
                    "maxLength": MAX_MERMAID_CHARS,
                    "description": "Constrained Mermaid source with an allowed diagram header.",
                },
            },
            "required": ["alt_text", "source"],
            "additionalProperties": False,
        },
        handler=render_diagram,
        min_tier=TrustTier.MEMBER,
        searchable=True,
        category="Visuals",
        budget_specs=(ToolBudgetSpec(BudgetName.VISUAL_RENDERS, MAX_RENDERS_PER_TURN),),
    )
