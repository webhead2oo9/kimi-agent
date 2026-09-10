"""Fetch approved inputs and execute a scheduled script without model or bot credentials."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from app.task_output import snapshot_output
from sandbox.runner import SandboxConfig, run_python_in_sandbox
from tools.downloads import fetch_url_to_file
from tools.registry import MessageContext, TurnOutbox
from tools.task_python import (
    INPUT_STATE_KEY,
    MAX_RESULT_BYTES,
    DiscordPythonInput,
    TaskPythonResult,
    user_task_state,
)
from tools.workspace.common import UserLocks, scrub_user_paths, workspace_activity
from utils.asyncio import await_uncancellable

if TYPE_CHECKING:
    from app.tools import RuntimeTools
    from discord_adapter.gateway import DiscordGateway
    from tools.scheduled_tasks import TaskDefinition

MAX_INPUT_BYTES = 10 * 1024 * 1024
MAX_HISTORY_PAGES = 100
FETCH_TIMEOUT_SECONDS = 60


@asynccontextmanager
async def execution_slot(
    semaphore: asyncio.Semaphore, locks: UserLocks, ctx: MessageContext
) -> AsyncIterator[None]:
    """Bound resource acquisition so opposing lock orders always make progress.

    An LLM turn can already own its workspace when it calls run_code, whereas a
    standalone code call takes the semaphore first. Neither ordering is safe for
    an unattended competitor. Release the workspace if the code slot does not
    arrive promptly, allowing a code call that already owns that slot to proceed.
    """
    while True:
        async with workspace_activity(locks, ctx):
            try:
                async with asyncio.timeout(0.1):
                    await semaphore.acquire()
            except TimeoutError:
                pass
            else:
                try:
                    yield
                finally:
                    semaphore.release()
                return


@dataclass(frozen=True)
class PythonExecution:
    result: TaskPythonResult
    files: list[tuple[str, str | None, bytes]]
    input_cursors: dict[str, str]
    duration_ms: int


def package_config(config: SandboxConfig, owner_files: Path) -> SandboxConfig:
    """Mount just the owner's persistent packages, with the same interpreter precedence."""
    venv = owner_files / ".venv"
    if not venv.exists() and not venv.is_symlink():
        return config
    interpreter = venv / "bin" / "python3"
    cfg = venv / "pyvenv.cfg"
    if (
        venv.is_symlink()
        or not venv.is_dir()
        or (venv / "bin").is_symlink()
        or interpreter.is_symlink()
        or not interpreter.is_file()
        or cfg.is_symlink()
        or not cfg.is_file()
    ):
        raise ValueError(
            "The existing Python package environment is unavailable; repair it in setup"
        )
    return replace(
        config,
        python_bin_override=str(interpreter.resolve()),
        extra_ro_binds=(*config.extra_ro_binds, str(venv.resolve())),
    )


def _read_result(root: Path) -> TaskPythonResult:
    # O_NONBLOCK prevents an executable-created FIFO from blocking the host reader.
    descriptor = os.open(root / "result.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_RESULT_BYTES:
            raise ValueError("result.json must be a regular file of at most 1 MiB")
        data = handle.read(MAX_RESULT_BYTES + 1)
    if len(data) > MAX_RESULT_BYTES:
        raise ValueError("result.json exceeds 1 MiB")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate JSON key: {key}")
            result[key] = value
        return result

    def nonfinite(value: str) -> None:
        raise ValueError(f"Non-finite JSON number: {value}")

    return TaskPythonResult.model_validate(
        json.loads(data, object_pairs_hook=unique_object, parse_constant=nonfinite)
    )


def _snapshot_files(
    root: Path, result: TaskPythonResult, channel: Any, max_attachments: int
) -> list[tuple[str, str | None, bytes]]:
    if not result.files:
        return []
    if len(result.files) > max_attachments:
        raise ValueError(f"Python output exceeds the configured {max_attachments} attachment limit")
    paths: list[str] = []
    descriptions: list[tuple[str, str]] = []
    names: set[str] = set()
    for item in result.files:
        path = root
        for part in item.path.split("/"):
            path /= part
            if path.is_symlink():
                raise ValueError("Python output must not contain symlinks")
        if not path.is_file() or path.name in names:
            raise ValueError("Python outputs must be regular files with unique filenames")
        names.add(path.name)
        paths.append(str(path))
        if item.description:
            descriptions.append((str(path), item.description))
    outbox = TurnOutbox(
        output_files=tuple(paths),
        allowed_file_roots=(str(root / "outputs"),),
        output_file_descriptions=dict(descriptions),
    )
    files, _ = snapshot_output(channel, outbox, [])
    return files


class TaskPythonRunner:
    def __init__(self, tools: RuntimeTools, gateway: DiscordGateway) -> None:
        self.tools, self.gateway = tools, gateway

    async def run(
        self,
        definition: TaskDefinition,
        ctx: MessageContext,
        *,
        task_id: str,
        revision: int,
        state: dict[str, Any],
        output_channel: Any,
        guard: Callable[[str], Awaitable[None]],
    ) -> PythonExecution:
        config = self.tools.task_python_sandbox_config
        guards = self.tools.code_exec_guards
        if config is None or config.network_mode != "none" or guards is None:
            raise ValueError("Scheduled Python requires an available offline code sandbox")
        assert definition.python is not None
        spec = definition.python
        async with execution_slot(guards.semaphore, self.tools.workspace_locks, ctx):
            await guard("run_code")
            root: Path | None = None

            def create() -> Path:
                nonlocal root
                root = self.tools.workspace_manager.create_job_dir(ctx.workspace_key)
                (root / "inputs").mkdir(mode=0o700)
                (root / "outputs").mkdir(mode=0o700)
                (root / "task.py").write_text(spec.code, encoding="utf-8")
                return root

            try:
                root = await await_uncancellable(asyncio.to_thread(create))
                now = datetime.now(UTC)
                async with asyncio.timeout(FETCH_TIMEOUT_SECONDS):
                    inputs, cursors = await self._inputs(definition, ctx, root, state, now, guard)
                payload = {
                    "task_id": task_id,
                    "revision": revision,
                    "run_id": ctx.scheduled_run_id,
                    "now": now.isoformat(),
                    "initialized": bool(state.get("_task_initialized")),
                    "state": user_task_state(state),
                    "inputs": inputs,
                }
                await await_uncancellable(
                    asyncio.to_thread(
                        (root / "input.json").write_text,
                        json.dumps(payload, allow_nan=False),
                        encoding="utf-8",
                    )
                )
                owner_files = await asyncio.to_thread(
                    self.tools.workspace_manager.user_files_dir, ctx.workspace_key
                )
                active_config = await asyncio.to_thread(package_config, config, owner_files)
                await guard("run_code")
                execution = await run_python_in_sandbox(active_config, root, root / "task.py")
                await guard("run_code")
                if execution.exit_code != 0 or execution.timed_out or execution.quota_exceeded:
                    reason = "timed out" if execution.timed_out else "failed"
                    diagnostic = scrub_user_paths(
                        execution.stderr, self.tools.workspace_manager, ctx.workspace_key
                    )[-2000:]
                    raise ValueError(
                        f"Python {reason} after {execution.duration_ms} ms: {diagnostic}"
                    )
                result = await await_uncancellable(asyncio.to_thread(_read_result, root))
                if definition.execution == "python_only" and result.outcome == "invoke_llm":
                    raise ValueError("Python-only tasks cannot invoke an LLM")
                if (
                    result.outcome in {"completed", "invoke_llm"}
                    and definition.condition
                    and definition.first_check == "silent"
                    and not state.get("_task_initialized")
                ):
                    result = result.model_copy(
                        update={
                            "outcome": "no_change",
                            "detail": "Initial baseline established silently",
                            "content": "",
                            "files": [],
                            "llm_context": "",
                        }
                    )
                files = await await_uncancellable(
                    asyncio.to_thread(
                        _snapshot_files,
                        root,
                        result,
                        output_channel,
                        min(10, self.tools.workspace_config.max_attachments),
                    )
                )
                return PythonExecution(result, files, cursors, execution.duration_ms)
            except TimeoutError as exc:
                raise ValueError("Python input acquisition exceeded its 60-second limit") from exc
            except (OSError, ValueError, RecursionError) as exc:
                detail = scrub_user_paths(str(exc), self.tools.workspace_manager, ctx.workspace_key)
                raise ValueError(f"Scheduled Python: {detail[:2500]}") from exc
            finally:
                if root is not None:
                    await await_uncancellable(asyncio.to_thread(shutil.rmtree, root))

    async def _inputs(
        self,
        definition: TaskDefinition,
        ctx: MessageContext,
        root: Path,
        state: dict[str, Any],
        now: datetime,
        guard: Callable[[str], Awaitable[None]],
    ) -> tuple[dict[str, Any], dict[str, str]]:
        assert definition.python is not None
        inputs: dict[str, Any] = {}
        cursors: dict[str, str] = {}
        previous = state.get(INPUT_STATE_KEY, {})
        if not isinstance(previous, dict):
            raise ValueError("Saved input cursors are invalid; reset task state")
        total_bytes = 0
        pages = 0
        for source in definition.python.inputs:
            path = root / "inputs" / source.name
            remaining = min(
                MAX_INPUT_BYTES - total_bytes,
                self.tools.workspace_config.max_user_bytes - total_bytes,
                self.tools.workspace_config.max_file_bytes,
            )
            if remaining <= 0:
                raise ValueError("Python inputs exceed the aggregate input byte limit")
            if isinstance(source, DiscordPythonInput):
                start = now - timedelta(seconds=source.lookback_seconds)
                if source.window == "since_success" and source.name in previous:
                    start = datetime.fromisoformat(previous[source.name])
                if start.tzinfo is None or start > now:
                    raise ValueError("Saved input window is invalid; reset task state")
                messages: list[dict[str, Any]] = []
                seen: set[str] = set()
                page_cursor: str | None = None
                message_bytes = 0
                while start < now:
                    await guard("get_channel_context")
                    if pages >= MAX_HISTORY_PAGES:
                        raise ValueError("Discord inputs exceed 100 pages; narrow the input window")
                    pages += 1
                    page = await self.gateway.collect_channel_history(
                        ctx,
                        {
                            "channel_id": source.channel_id,
                            "after": (start - timedelta(microseconds=1)).isoformat(),
                            "before": now.isoformat(),
                            "order": "asc",
                            "limit": 100,
                            "cursor": page_cursor,
                        },
                    )
                    for message in cast(list[dict[str, Any]], page["messages"]):
                        timestamp = datetime.fromisoformat(message["timestamp"])
                        if message["id"] in seen or not start <= timestamp < now:
                            continue
                        seen.add(message["id"])
                        message_bytes += len(json.dumps(message).encode("utf-8"))
                        if message_bytes > remaining:
                            raise ValueError("Discord inputs exceed the input byte limit")
                        messages.append(message)
                    if not page["has_more"]:
                        break
                    next_cursor = page["next_cursor"]
                    if (
                        not isinstance(next_cursor, str)
                        or not next_cursor
                        or next_cursor == page_cursor
                    ):
                        raise ValueError("Discord input pagination did not advance")
                    page_cursor = next_cursor
                data = json.dumps(messages).encode("utf-8")
                if len(data) > remaining:
                    raise ValueError("Discord inputs exceed the input byte limit")
                await await_uncancellable(asyncio.to_thread(path.write_bytes, data))
                total_bytes += len(data)
                inputs[source.name] = {
                    "kind": "discord",
                    "channel_id": source.channel_id,
                    "path": f"inputs/{source.name}",
                    "window_start": start.isoformat(),
                    "window_end": now.isoformat(),
                    "count": len(messages),
                }
                cursors[source.name] = now.isoformat()
            else:
                await guard("fetch_url")
                response = await fetch_url_to_file(
                    source.url,
                    path,
                    max_bytes=remaining,
                    timeout_seconds=min(
                        self.tools.workspace_config.fetch_timeout_seconds, FETCH_TIMEOUT_SECONDS
                    ),
                    max_redirects=self.tools.workspace_config.max_redirects,
                )
                total_bytes += response.size_bytes
                inputs[source.name] = {
                    "kind": "https",
                    "url": source.url,
                    "path": f"inputs/{source.name}",
                    "content_type": response.content_type,
                    "size_bytes": response.size_bytes,
                }
        return inputs, cursors
