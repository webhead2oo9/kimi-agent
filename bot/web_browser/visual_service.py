"""Ephemeral offline BetterWright service for fixed visual rendering."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import stat
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal
from uuid import uuid4

from sandbox.seccomp import SeccompUnavailableError, open_bpf_fd, seccomp_bpf_bytes
from web_browser.service import BrowserService, BrowserServiceConfig, _runtime_env, _stop_unit

type VisualKind = Literal["chart", "mermaid"]
type ChartType = Literal["bar", "line", "scatter"]
type AxisScale = Literal["linear", "symlog"]
type OverlapMode = Literal["none", "count"]

_RESULT_PREFIX = "__VISUAL_RESULT__"
_MERMAID_VERSION = "11.17.2"
_MAX_REQUEST_BYTES = 256 * 1024
_MAX_RESULT_BYTES = 16 * 1024
_MAX_STDERR_BYTES = 16 * 1024
_DEFAULT_MAX_OUTPUT_BYTES = 8 * 1024 * 1024


class VisualServiceError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ScatterPoint:
    x: float
    y: float
    label: str = ""


@dataclass(frozen=True, slots=True)
class VisualSeries:
    name: str
    values: tuple[float, ...] = ()
    points: tuple[ScatterPoint, ...] = ()


@dataclass(frozen=True, slots=True)
class VisualRenderRequest:
    kind: VisualKind
    title: str
    alt_text: str
    chart_type: ChartType | None = None
    x_label: str = ""
    y_label: str = ""
    categories: tuple[str, ...] = ()
    series: tuple[VisualSeries, ...] = ()
    x_scale: AxisScale = "linear"
    y_scale: AxisScale = "linear"
    overlap_mode: OverlapMode = "none"
    source: str = ""

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "kind": self.kind,
            "title": self.title,
            "alt_text": self.alt_text,
        }
        if self.kind == "mermaid":
            payload["source"] = self.source
            return payload
        payload.update(
            {
                "chart_type": self.chart_type,
                "x_label": self.x_label,
                "y_label": self.y_label,
                "x_scale": self.x_scale,
                "y_scale": self.y_scale,
                "overlap_mode": self.overlap_mode,
                "categories": list(self.categories),
                "series": [
                    {
                        "name": item.name,
                        **({"values": list(item.values)} if item.values else {}),
                        **(
                            {
                                "points": [
                                    {"x": point.x, "y": point.y, "label": point.label}
                                    for point in item.points
                                ]
                            }
                            if item.points
                            else {}
                        ),
                    }
                    for item in self.series
                ],
            }
        )
        return payload


@dataclass(frozen=True, slots=True)
class VisualRenderResult:
    output_path: Path
    width: int
    height: int


class VisualService:
    """Launch one offline worker for one normalized render request."""

    def __init__(
        self,
        config: BrowserServiceConfig,
        *,
        max_output_bytes: int = _DEFAULT_MAX_OUTPUT_BYTES,
    ) -> None:
        # Visual jobs never use the persistent browser's VPN or profile state.
        self.config = replace(config, network_mode="host")
        if isinstance(max_output_bytes, bool) or max_output_bytes <= 0:
            raise ValueError("max_output_bytes must be positive")
        self.max_output_bytes = max_output_bytes
        # Chromium is the expensive boundary here. Serialize first-party renders
        # process-wide rather than letting concurrent Discord turns multiply it.
        self._semaphore = asyncio.Semaphore(1)

    @property
    def mermaid_package(self) -> Path:
        return self.config.runtime_dir / "node_modules" / "mermaid"

    @property
    def mermaid_bundle(self) -> Path:
        return self.mermaid_package / "dist" / "mermaid.min.js"

    def availability_error(self) -> str | None:
        # Reuse the reviewed runtime, ownership, binary, and seccomp checks while
        # forcing the visual bridge's offline host-independent launch profile.
        base_error = BrowserService(self.config).availability_error()
        if base_error is not None:
            return base_error
        bundle = self.mermaid_bundle
        manifest = self.mermaid_package / "package.json"
        if not bundle.is_file():
            return f"pinned Mermaid browser bundle is missing: {bundle}"
        try:
            package = json.loads(manifest.read_text(encoding="utf-8"))
        except OSError, UnicodeError, json.JSONDecodeError:
            return f"pinned Mermaid package manifest is unreadable: {manifest}"
        if not isinstance(package, dict) or package.get("version") != _MERMAID_VERSION:
            return f"Mermaid runtime must be exactly version {_MERMAID_VERSION}"
        if self.config.require_root_owned_runtime:
            try:
                controlled_paths = (self.mermaid_package, manifest, bundle)
                unsafe = [
                    path
                    for path in controlled_paths
                    if path.stat().st_uid != 0
                    or path.stat().st_mode & (stat.S_IWGRP | stat.S_IWOTH)
                ]
            except OSError:
                return f"pinned Mermaid browser bundle is unreadable: {bundle}"
            if unsafe:
                return (
                    "pinned Mermaid runtime must be root-owned and not group/world writable: "
                    f"{unsafe[0]}"
                )
        return None

    async def render(
        self,
        request: VisualRenderRequest,
        job_dir: Path,
    ) -> VisualRenderResult:
        async with self._semaphore:
            return await self._render_isolated(request, job_dir)

    async def _render_isolated(
        self,
        request: VisualRenderRequest,
        job_dir: Path,
    ) -> VisualRenderResult:
        if sys.platform != "linux":
            raise VisualServiceError("Visual rendering is supported only on Linux.")
        payload = json.dumps(request.to_payload(), ensure_ascii=False, separators=(",", ":"))
        encoded = payload.encode("utf-8")
        if len(encoded) > _MAX_REQUEST_BYTES:
            raise VisualServiceError("Visual render request exceeded its service limit.")
        availability = await asyncio.to_thread(self.availability_error)
        if availability is not None:
            raise VisualServiceError(f"Visual rendering is unavailable: {availability}")

        try:
            seccomp_bpf_bytes()
            seccomp_fd = open_bpf_fd()
        except (OSError, SeccompUnavailableError) as exc:
            raise VisualServiceError("Visual rendering seccomp policy is unavailable.") from exc
        unit_name = f"kimi-visual-{uuid4().hex}"
        command = build_visual_worker_command(
            self.config,
            job_dir,
            unit_name=unit_name,
            seccomp_fd=seccomp_fd,
            max_output_bytes=self.max_output_bytes,
        )
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_runtime_env(),
                pass_fds=(seccomp_fd,),
            )
        except asyncio.CancelledError:
            await _stop_unit_shielded(self.config, unit_name)
            raise
        except OSError as exc:
            try:
                await _stop_unit_shielded(self.config, unit_name)
            except Exception as teardown_exc:
                raise VisualServiceError(
                    "Visual renderer could not start and its shutdown could not be confirmed."
                ) from teardown_exc
            raise VisualServiceError("Visual renderer could not start.") from exc
        finally:
            os.close(seccomp_fd)

        try:
            stdout, stderr, stdout_exceeded, stderr_exceeded = await asyncio.wait_for(
                _communicate_bounded(process, encoded),
                timeout=self.config.call_timeout_seconds + 5,
            )
        except TimeoutError as exc:
            await _terminate_process(process)
            try:
                await _stop_unit_shielded(self.config, unit_name)
            except Exception as teardown_exc:
                raise VisualServiceError(
                    "Visual renderer timed out and its shutdown could not be confirmed."
                ) from teardown_exc
            raise VisualServiceError("Visual rendering timed out.") from exc
        except asyncio.CancelledError:
            await _terminate_process(process)
            await _stop_unit_shielded(self.config, unit_name)
            raise
        except Exception as exc:
            await _terminate_process(process)
            try:
                await _stop_unit_shielded(self.config, unit_name)
            except Exception as teardown_exc:
                raise VisualServiceError(
                    "Visual renderer failed and its shutdown could not be confirmed."
                ) from teardown_exc
            raise VisualServiceError("Visual renderer communication failed.") from exc
        try:
            await _stop_unit_shielded(self.config, unit_name)
        except Exception as exc:
            raise VisualServiceError("Visual renderer shutdown could not be confirmed.") from exc
        if stdout_exceeded:
            raise VisualServiceError("Visual renderer returned an oversized response.")
        envelope = _parse_result(stdout)
        if process.returncode != 0 or not envelope.get("ok"):
            detail = str(envelope.get("error") or "").strip()
            if not detail and stderr and not stderr_exceeded:
                detail = stderr.decode(errors="replace").strip().splitlines()[-1]
            raise VisualServiceError(detail or "Visual rendering failed.")
        if envelope.get("filename") != "render.png":
            raise VisualServiceError("Visual renderer returned an invalid output name.")
        width = envelope.get("width")
        height = envelope.get("height")
        if isinstance(width, bool) or not isinstance(width, int):
            raise VisualServiceError("Visual renderer returned invalid dimensions.")
        if isinstance(height, bool) or not isinstance(height, int):
            raise VisualServiceError("Visual renderer returned invalid dimensions.")
        return VisualRenderResult(job_dir / "render.png", width, height)


async def _stop_unit_shielded(config: BrowserServiceConfig, unit_name: str) -> None:
    stop_task = asyncio.create_task(_stop_unit(config, unit_name))
    try:
        await asyncio.shield(stop_task)
    except asyncio.CancelledError:
        # Unit confirmation is a security boundary, not optional cleanup. Finish
        # it before propagating cancellation to the outer Discord turn.
        with contextlib.suppress(asyncio.CancelledError):
            await stop_task
        raise


async def _terminate_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
    await process.wait()


async def _communicate_bounded(
    process: asyncio.subprocess.Process,
    payload: bytes,
) -> tuple[bytes, bytes, bool, bool]:
    async def write_input() -> None:
        if process.stdin is None:
            return
        process.stdin.write(payload)
        await process.stdin.drain()
        process.stdin.close()
        await process.stdin.wait_closed()

    async def read_stream(
        stream: asyncio.StreamReader | None,
        limit: int,
    ) -> tuple[bytes, bool]:
        if stream is None:
            return b"", False
        captured = bytearray()
        exceeded = False
        while chunk := await stream.read(8192):
            remaining = max(0, limit + 1 - len(captured))
            if remaining:
                captured.extend(chunk[:remaining])
            if len(captured) > limit or len(chunk) > remaining:
                exceeded = True
        return bytes(captured[:limit]), exceeded

    stdout_task = asyncio.create_task(read_stream(process.stdout, _MAX_RESULT_BYTES))
    stderr_task = asyncio.create_task(read_stream(process.stderr, _MAX_STDERR_BYTES))
    try:
        await write_input()
        await process.wait()
        stdout, stdout_exceeded = await stdout_task
        stderr, stderr_exceeded = await stderr_task
        return stdout, stderr, stdout_exceeded, stderr_exceeded
    finally:
        for task in (stdout_task, stderr_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)


def _parse_result(stdout: bytes) -> dict[str, object]:
    for raw_line in reversed(stdout.splitlines()):
        if not raw_line.startswith(_RESULT_PREFIX.encode()):
            continue
        try:
            value = json.loads(raw_line[len(_RESULT_PREFIX) :])
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise VisualServiceError("Visual renderer returned invalid JSON.") from exc
        if isinstance(value, dict):
            return value
        break
    raise VisualServiceError("Visual renderer returned no result.")


def build_visual_worker_command(
    config: BrowserServiceConfig,
    job_dir: Path,
    *,
    unit_name: str,
    seccomp_fd: int,
    max_output_bytes: int = _DEFAULT_MAX_OUTPUT_BYTES,
) -> list[str]:
    """Compose the ephemeral offline renderer boundary."""

    runtime = str(config.runtime_dir.resolve())
    bridge = str(config.bridge_script.resolve())
    visual_math = str((config.bridge_script.parent / "visual_math.mjs").resolve())
    output = str(job_dir.resolve())
    limits = [
        "-p",
        f"TasksMax={config.max_tasks}",
        "-p",
        f"MemoryMax={config.max_total_memory_mb}M",
        "-p",
        "MemorySwapMax=0",
        "-p",
        f"CPUQuota={config.cpu_quota_percent}%",
        "-p",
        f"RuntimeMaxSec={config.call_timeout_seconds + 10}",
    ]
    return [
        config.systemd_run_bin,
        "--user",
        "--scope",
        "--quiet",
        "--collect",
        f"--unit={unit_name}",
        *limits,
        "--",
        config.prlimit_bin,
        "--core=0:0",
        f"--fsize={config.max_fsize_mb * 1024 * 1024}",
        f"--nofile={config.max_open_files}",
        "--",
        config.bwrap_bin,
        "--unshare-all",
        "--unshare-user",
        "--disable-userns",
        "--assert-userns-disabled",
        "--uid",
        "0",
        "--gid",
        "0",
        "--seccomp",
        str(seccomp_fd),
        "--die-with-parent",
        "--new-session",
        "--clearenv",
        "--setenv",
        "PATH",
        "/usr/bin",
        "--setenv",
        "HOME",
        "/work",
        "--setenv",
        "BETTERWRIGHT_HOME",
        "/work",
        "--setenv",
        "BETTERWRIGHT_MODULE",
        "/runtime/betterwright-entry.mjs",
        "--setenv",
        "BETTERWRIGHT_CHROMIUM_PATH",
        "/runtime/.betterwright/chromium/linux-x64/betterchromium",
        "--setenv",
        "MERMAID_BUNDLE",
        "/runtime/node_modules/mermaid/dist/mermaid.min.js",
        "--setenv",
        "VISUAL_CALL_TIMEOUT_SECONDS",
        str(config.call_timeout_seconds),
        "--setenv",
        "VISUAL_MAX_OUTPUT_BYTES",
        str(max_output_bytes),
        "--setenv",
        "NODE_NO_WARNINGS",
        "1",
        "--setenv",
        "LANG",
        "C.UTF-8",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--size",
        str(config.tmp_size_mb * 1024 * 1024),
        "--tmpfs",
        "/tmp",
        "--size",
        str(config.tmp_size_mb * 1024 * 1024),
        "--tmpfs",
        "/work",
        "--ro-bind",
        "/usr",
        "/usr",
        "--symlink",
        "usr/lib",
        "/lib",
        "--symlink",
        "usr/lib64",
        "/lib64",
        "--symlink",
        "usr/bin",
        "/bin",
        "--ro-bind",
        runtime,
        "/runtime",
        "--ro-bind-try",
        "/etc/fonts",
        "/etc/fonts",
        "--ro-bind",
        bridge,
        "/visual_bridge.mjs",
        "--ro-bind",
        visual_math,
        "/visual_math.mjs",
        "--bind",
        output,
        "/output",
        "--chdir",
        "/work",
        "--",
        config.node_bin,
        "/visual_bridge.mjs",
    ]
