from __future__ import annotations

import asyncio
import json
import os
import struct
import zlib
from pathlib import Path
from typing import Any

import pytest

import web_browser.visual_service as visual_service
from agent.turn import _stage_response_files_sync
from tools.registry import BudgetName, MessageContext, ToolRegistry, TurnBudget
from tools.visuals import (
    CHART_TOOL_NAME,
    DIAGRAM_TOOL_NAME,
    VisualToolConfig,
    init_visual_tool,
    verify_rendered_png,
    validate_visual_request,
)
from tools.workspace.common import UserLocks
from trust.tiers import TrustTier
from web_browser.service import BrowserServiceConfig
from web_browser.visual_service import (
    VisualRenderRequest,
    VisualRenderResult,
    VisualService,
    _stop_unit_shielded,
    build_visual_worker_command,
)
from workspace import WorkspaceManager


def _context(*, context_key: str = "g1:c1:root") -> MessageContext:
    return MessageContext(
        user_id="42",
        user_name="member",
        guild_id="1",
        channel_id="2",
        thread_id=None,
        trust_tier=TrustTier.MEMBER,
        context_key=context_key,
        budget=TurnBudget(caps={BudgetName.VISUAL_RENDERS: 4}),
        activated_tools={CHART_TOOL_NAME, DIAGRAM_TOOL_NAME},
    )


def _png(width: int = 1200, height: int = 675) -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        body = kind + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))

    scanline = b"\x00" + b"\xff\xff\xff" * width
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(scanline * height))
        + chunk(b"IEND", b"")
    )


def test_visual_schema_is_provider_neutral_and_searchable(tmp_path: Path) -> None:
    class Service:
        async def render(self, request: VisualRenderRequest, job_dir: Path) -> VisualRenderResult:
            del request, job_dir
            raise AssertionError("not called")

    registry = ToolRegistry()
    init_visual_tool(
        registry,
        Service(),  # type: ignore[arg-type]
        WorkspaceManager(tmp_path),
        VisualToolConfig(),
        UserLocks(),
    )

    chart = registry.get_searchable_entry(CHART_TOOL_NAME, TrustTier.MEMBER)
    diagram = registry.get_searchable_entry(DIAGRAM_TOOL_NAME, TrustTier.MEMBER)
    assert chart is not None
    assert diagram is not None
    assert chart.category == diagram.category == "Visuals"
    assert chart.searchable is diagram.searchable is True
    assert "source" not in chart.parameters["properties"]
    assert chart.parameters["properties"]["x_scale"]["enum"] == ["linear", "symlog"]
    assert chart.parameters["properties"]["y_scale"]["enum"] == ["linear", "symlog"]
    assert chart.parameters["properties"]["overlap_mode"]["enum"] == ["none", "count"]
    assert not {"chart_type", "categories", "series"} & diagram.parameters["properties"].keys()

    def assert_neutral(value: object) -> None:
        if isinstance(value, dict):
            assert "oneOf" not in value
            assert "anyOf" not in value
            for child in value.values():
                assert_neutral(child)
        elif isinstance(value, list):
            for child in value:
                assert_neutral(child)

    assert_neutral(chart.parameters)
    assert_neutral(diagram.parameters)


def test_chart_validation_rejects_conditional_fields_and_nonfinite_values() -> None:
    request = validate_visual_request(
        {
            "kind": "chart",
            "chart_type": "bar",
            "title": "Quarterly signups",
            "x_label": "Quarter",
            "y_label": "Users",
            "alt_text": "Signups increase from Q1 through Q3.",
            "categories": ["Q1", "Q2", "Q3"],
            "series": [{"name": "Signups", "values": [10, 15, 22]}],
        }
    )
    assert request.chart_type == "bar"
    assert request.series[0].values == (10.0, 15.0, 22.0)

    default_bar = validate_visual_request(
        {
            "kind": "chart",
            "alt_text": "One bar.",
            "categories": ["A"],
            "series": [{"name": "S", "values": [1]}],
        }
    )
    assert default_bar.chart_type == "bar"

    scatter_controls = validate_visual_request(
        {
            "kind": "chart",
            "chart_type": "scatter",
            "x_scale": "symlog",
            "y_scale": "symlog",
            "overlap_mode": "count",
            "alt_text": "Near-zero and outlier points use symmetric logarithmic axes.",
            "series": [
                {
                    "name": "Observations",
                    "points": [
                        {"x": 0, "y": 0},
                        {"x": 0, "y": 0},
                        {"x": -0.001, "y": 0.002},
                        {"x": 1_000_000, "y": -1_000_000},
                    ],
                }
            ],
        }
    )
    assert scatter_controls.x_scale == "symlog"
    assert scatter_controls.y_scale == "symlog"
    assert scatter_controls.overlap_mode == "count"

    for field, value, message in (
        ("x_scale", "log", "x_scale must be linear or symlog"),
        ("y_scale", "log", "y_scale must be linear or symlog"),
        ("overlap_mode", "jitter", "overlap_mode must be none or count"),
    ):
        with pytest.raises(ValueError, match=message):
            validate_visual_request(
                {
                    "kind": "chart",
                    "chart_type": "scatter",
                    field: value,
                    "alt_text": "Points.",
                    "series": [{"name": "S", "points": [{"x": 1, "y": 2}]}],
                }
            )

    with pytest.raises(ValueError, match="non-default values only for scatter"):
        validate_visual_request(
            {
                "kind": "chart",
                "chart_type": "line",
                "y_scale": "symlog",
                "alt_text": "Line.",
                "categories": ["A"],
                "series": [{"name": "S", "values": [1]}],
            }
        )

    with pytest.raises(ValueError, match="categories is not allowed"):
        validate_visual_request(
            {
                "kind": "chart",
                "chart_type": "scatter",
                "alt_text": "Points.",
                "categories": ["forbidden"],
                "series": [{"name": "S", "points": [{"x": 1, "y": 2}]}],
            }
        )
    with pytest.raises(ValueError, match="must be finite"):
        validate_visual_request(
            {
                "kind": "chart",
                "chart_type": "line",
                "alt_text": "Line.",
                "categories": ["A"],
                "series": [{"name": "S", "values": [float("nan")]}],
            }
        )
    with pytest.raises(ValueError, match="must be between"):
        validate_visual_request(
            {
                "kind": "chart",
                "chart_type": "line",
                "alt_text": "Line.",
                "categories": ["A"],
                "series": [{"name": "S", "values": [1e100]}],
            }
        )
    with pytest.raises(ValueError, match="categories must be unique"):
        validate_visual_request(
            {
                "kind": "chart",
                "chart_type": "bar",
                "alt_text": "Bars.",
                "categories": ["A", "A"],
                "series": [{"name": "S", "values": [1, 2]}],
            }
        )
    with pytest.raises(ValueError, match="unknown chart field"):
        validate_visual_request(
            {
                "kind": "chart",
                "chart_type": "line",
                "alt_text": "Line.",
                "categories": ["A"],
                "series": [{"name": "S", "values": [1]}],
                "colors": ["red"],
            }
        )


def test_visual_validation_accepts_neutral_flat_schema_placeholders() -> None:
    chart = validate_visual_request(
        {
            "kind": "chart",
            "chart_type": "line",
            "title": "Build times",
            "x_label": "Build",
            "y_label": "Seconds",
            "alt_text": "Build times fall over six builds.",
            "categories": ["1", "2"],
            "series": [
                {"name": "Optimized", "values": [42, 37], "points": []},
            ],
            "source": "",
        }
    )
    assert chart.chart_type == "line"
    assert chart.series[0].values == (42.0, 37.0)

    scatter = validate_visual_request(
        {
            "kind": "chart",
            "chart_type": "scatter",
            "title": "Latency",
            "x_label": "Payload",
            "y_label": "Milliseconds",
            "alt_text": "Latency rises with payload size.",
            "categories": [],
            "series": [
                {"name": "Requests", "values": [], "points": [{"x": 1, "y": 2}]},
            ],
            "source": "",
        }
    )
    assert scatter.chart_type == "scatter"
    assert scatter.series[0].points[0].x == 1.0

    mermaid = validate_visual_request(
        {
            "kind": "mermaid",
            "chart_type": "bar",
            "title": "Request flow",
            "x_label": "",
            "y_label": "",
            "alt_text": "A request flows from validation to response.",
            "categories": [],
            "series": [],
            "source": "flowchart LR\nA[Request] --> B[Response]",
        }
    )
    assert mermaid.kind == "mermaid"


@pytest.mark.parametrize(
    "source",
    [
        "---\ntheme: dark\n---\nflowchart TD\nA-->B",
        'flowchart TD\nclick A href "https://example.com"',
        "flowchart TD\nA[<b>unsafe</b>]",
        "flowchart TD\nclassDef danger fill:red",
        "flowchart TD\nA:::danger",
        'flowchart TD\nA@{ img: "relative.png" }',
        "journey\ntitle Unsupported",
    ],
)
def test_mermaid_validation_rejects_unsafe_or_unsupported_source(source: str) -> None:
    with pytest.raises(ValueError):
        validate_visual_request(
            {
                "kind": "mermaid",
                "title": "Diagram",
                "alt_text": "A diagram.",
                "source": source,
            }
        )


@pytest.mark.parametrize(
    "source",
    [
        "flowchart TD\n  A[Start] --> B[Done]",
        "sequenceDiagram\n  Alice->>Bob: Hello",
        "stateDiagram-v2\n  [*] --> Ready",
        "classDiagram\n  class Animal\n  Animal : +name",
        "erDiagram\n  USER ||--o{ ORDER : places",
    ],
)
def test_mermaid_validation_accepts_supported_diagrams(source: str) -> None:
    request = validate_visual_request(
        {
            "kind": "mermaid",
            "title": "Diagram",
            "alt_text": "A supported diagram.",
            "source": source,
        }
    )
    assert request.kind == "mermaid"


def test_visual_service_requires_exact_pinned_mermaid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = BrowserServiceConfig(
        runtime_dir=tmp_path / "runtime",
        profiles_dir=tmp_path / "profiles",
        bridge_script=tmp_path / "visual_bridge.mjs",
        require_root_owned_runtime=False,
    )
    package = config.runtime_dir / "node_modules" / "mermaid"
    bundle = package / "dist" / "mermaid.min.js"
    bundle.parent.mkdir(parents=True)
    bundle.write_text("bundle", encoding="utf-8")
    (package / "package.json").write_text('{"version":"11.17.2"}', encoding="utf-8")
    service = VisualService(config)
    monkeypatch.setattr(visual_service.BrowserService, "availability_error", lambda self: None)

    assert service.availability_error() is None

    (package / "package.json").write_text('{"version":"11.12.2"}', encoding="utf-8")
    assert "exactly version 11.17.2" in str(service.availability_error())


def test_visual_worker_command_is_ephemeral_and_offline(tmp_path: Path) -> None:
    config = BrowserServiceConfig(
        runtime_dir=tmp_path / "runtime",
        profiles_dir=tmp_path / "profiles",
        bridge_script=tmp_path / "visual_bridge.mjs",
        call_timeout_seconds=47.5,
        require_root_owned_runtime=False,
    )
    job = tmp_path / "job"
    command = build_visual_worker_command(
        config,
        job,
        unit_name="visual-test",
        seccomp_fd=9,
        max_output_bytes=1_234_567,
    )

    assert "--unshare-all" in command
    assert "--share-net" not in command
    assert "/etc/resolv.conf" not in command
    assert "/etc/ssl" not in command
    assert str(config.profiles_dir) not in command
    assert command[command.index("--seccomp") + 1] == "9"
    assert command[-2:] == ["/runtime/node", "/visual_bridge.mjs"]
    timeout_index = command.index("VISUAL_CALL_TIMEOUT_SECONDS")
    assert command[timeout_index + 1] == "47.5"
    output_limit_index = command.index("VISUAL_MAX_OUTPUT_BYTES")
    assert command[output_limit_index + 1] == "1234567"
    visual_math = str((config.bridge_script.parent / "visual_math.mjs").resolve())
    math_index = command.index(visual_math)
    assert command[math_index - 1 : math_index + 2] == [
        "--ro-bind",
        visual_math,
        "/visual_math.mjs",
    ]
    job_index = command.index(str(job.resolve()))
    assert command[job_index - 1 : job_index + 2] == ["--bind", str(job.resolve()), "/output"]
    assert any(command[index : index + 2] == ["--tmpfs", "/work"] for index in range(len(command)))


@pytest.mark.asyncio
async def test_visual_service_confirms_unit_stop_when_spawn_is_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = BrowserServiceConfig(
        runtime_dir=tmp_path / "runtime",
        profiles_dir=tmp_path / "profiles",
        bridge_script=tmp_path / "visual_bridge.mjs",
        require_root_owned_runtime=False,
    )
    service = VisualService(config)
    monkeypatch.setattr(service, "availability_error", lambda: None)
    monkeypatch.setattr(visual_service.sys, "platform", "linux")
    monkeypatch.setattr(visual_service, "seccomp_bpf_bytes", lambda: b"policy")
    monkeypatch.setattr(
        visual_service,
        "open_bpf_fd",
        lambda: os.open(os.devnull, os.O_RDONLY),
    )
    stopped: list[str] = []

    async def cancelled_spawn(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise asyncio.CancelledError

    async def stop_unit(config: BrowserServiceConfig, unit_name: str) -> None:
        del config
        stopped.append(unit_name)

    monkeypatch.setattr(visual_service.asyncio, "create_subprocess_exec", cancelled_spawn)
    monkeypatch.setattr(visual_service, "_stop_unit", stop_unit)
    job = tmp_path / "job"
    job.mkdir()

    with pytest.raises(asyncio.CancelledError):
        await service._render_isolated(
            VisualRenderRequest(kind="mermaid", title="", alt_text="Diagram", source="graph TD"),
            job,
        )

    assert len(stopped) == 1
    assert stopped[0].startswith("kimi-visual-")


@pytest.mark.asyncio
async def test_stop_confirmation_finishes_before_propagating_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = BrowserServiceConfig(
        runtime_dir=tmp_path,
        profiles_dir=tmp_path / "profiles",
        bridge_script=tmp_path / "bridge.mjs",
        require_root_owned_runtime=False,
    )
    started = asyncio.Event()
    release = asyncio.Event()
    finished = False

    async def stop_unit(config: BrowserServiceConfig, unit_name: str) -> None:
        nonlocal finished
        del config, unit_name
        started.set()
        await release.wait()
        finished = True

    monkeypatch.setattr(visual_service, "_stop_unit", stop_unit)
    task = asyncio.create_task(_stop_unit_shielded(config, "visual-test"))
    await started.wait()
    task.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished is True


@pytest.mark.asyncio
async def test_visual_tool_renders_verifies_and_queues_png(tmp_path: Path) -> None:
    class Service:
        def __init__(self) -> None:
            self.requests: list[VisualRenderRequest] = []

        async def render(self, request: VisualRenderRequest, job_dir: Path) -> VisualRenderResult:
            self.requests.append(request)
            output = job_dir / "render.png"
            output.write_bytes(_png())
            return VisualRenderResult(output, 1200, 675)

    service = Service()
    manager = WorkspaceManager(tmp_path / "workspace")
    registry = ToolRegistry()
    init_visual_tool(
        registry,
        service,  # type: ignore[arg-type]
        manager,
        VisualToolConfig(max_png_bytes=1024 * 1024, max_attachments=2),
        UserLocks(),
    )
    ctx = _context()

    result = json.loads(
        await registry.dispatch(
            CHART_TOOL_NAME,
            {
                "chart_type": "scatter",
                "title": "Relationship",
                "x_label": "X",
                "y_label": "Y",
                "x_scale": "symlog",
                "y_scale": "symlog",
                "overlap_mode": "count",
                "alt_text": "Three points trend upward.",
                "series": [
                    {
                        "name": "Observations",
                        "points": [
                            {"x": 1, "y": 2},
                            {"x": 2, "y": 3},
                            {"x": 3, "y": 5},
                        ],
                    }
                ],
            },
            ctx,
        )
    )

    assert result.get("ok") is True, result
    assert result["x_scale"] == "symlog"
    assert result["y_scale"] == "symlog"
    assert result["overlap_mode"] == "count"
    assert result["filename"] == "visual-1.png"
    assert "workspace" not in json.dumps(result)
    assert len(service.requests) == 1
    assert service.requests[0].x_scale == "symlog"
    assert service.requests[0].y_scale == "symlog"
    assert service.requests[0].overlap_mode == "count"
    assert ctx.budget_used(BudgetName.VISUAL_RENDERS) == 1
    assert len(ctx.output_files) == 1
    assert Path(ctx.output_files[0]).read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert ctx.output_file_descriptions[ctx.output_files[0]] == "Three points trend upward."

    diagram = json.loads(
        await registry.dispatch(
            DIAGRAM_TOOL_NAME,
            {
                "title": "Request flow",
                "alt_text": "A request flows to a response.",
                "source": "flowchart LR\nA[Request] --> B[Response]",
            },
            ctx,
        )
    )
    assert diagram.get("ok") is True, diagram
    assert diagram["kind"] == "mermaid"
    assert "chart_type" not in diagram
    assert len(service.requests) == 2
    assert len(ctx.output_files) == 2


def test_png_verification_rejects_corrupt_and_incomplete_images(tmp_path: Path) -> None:
    valid = tmp_path / "valid.png"
    valid.write_bytes(_png())
    assert verify_rendered_png(valid, 1024 * 1024)[1:] == (1200, 675)

    truncated = tmp_path / "truncated.png"
    truncated.write_bytes(valid.read_bytes()[:24])
    with pytest.raises(ValueError, match="truncated|incomplete"):
        verify_rendered_png(truncated, 1024 * 1024)

    corrupt_payload = bytearray(valid.read_bytes())
    corrupt_payload[-5] ^= 0x01
    corrupt = tmp_path / "corrupt.png"
    corrupt.write_bytes(corrupt_payload)
    with pytest.raises(ValueError, match="checksum"):
        verify_rendered_png(corrupt, 1024 * 1024)

    wrong_dimensions = tmp_path / "wrong.png"
    wrong_dimensions.write_bytes(_png(width=10, height=10))
    with pytest.raises(ValueError, match="dimensions"):
        verify_rendered_png(wrong_dimensions, 1024 * 1024)


def test_delivery_staging_preserves_attachment_description(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    source_root.mkdir()
    source = source_root / "visual.png"
    source.write_bytes(_png())
    manager = WorkspaceManager(tmp_path / "workspace")

    staged, _root, _embed, descriptions = _stage_response_files_sync(
        manager,
        "context",
        "42",
        [str(source)],
        [source_root],
        None,
        {str(source): "An upward trend."},
    )

    assert descriptions == {staged[0]: "An upward trend."}


@pytest.mark.asyncio
async def test_visual_tool_checks_context_and_attachment_cap_before_rendering(
    tmp_path: Path,
) -> None:
    class Service:
        calls = 0

        async def render(self, request: VisualRenderRequest, job_dir: Path) -> VisualRenderResult:
            del request, job_dir
            self.calls += 1
            raise AssertionError("not called")

    service = Service()
    registry = ToolRegistry()
    init_visual_tool(
        registry,
        service,  # type: ignore[arg-type]
        WorkspaceManager(tmp_path / "workspace"),
        VisualToolConfig(max_attachments=1),
        UserLocks(),
    )
    args: dict[str, Any] = {
        "alt_text": "A flow.",
        "source": "flowchart TD\nA-->B",
    }
    missing_context = _context(context_key="")
    assert "conversation context" in await registry.dispatch(
        DIAGRAM_TOOL_NAME, args, missing_context
    )

    capped = _context()
    capped.output_files.append(str(tmp_path / "existing.png"))
    assert "attachment limit" in await registry.dispatch(DIAGRAM_TOOL_NAME, args, capped)
    assert service.calls == 0
