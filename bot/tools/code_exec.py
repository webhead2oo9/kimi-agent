"""Workspace code execution, backed by the systemd-scope + bwrap sandbox.

Registered when code exec is enabled and the sandbox profile can launch (see
app/tools.py). The single tool defaults to MEMBER tier and can be restricted to REGULAR or
STAFF at registration, but the layer that matters is the sandbox itself
(sandbox/runner.py): the *arguments* are model-generated, the
model reads untrusted context, and the caller may be adversarial, so the
namespace/cgroup/rlimit profile, not the registry tier, is what contains what
actually runs. The executed file runs against the requesting user's own
workspace, which is bind-mounted read-write inside the sandbox.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import stat
import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from sandbox.netns_lease import NetnsLease, NetnsLeaseSafetyError
from sandbox.runner import (
    SandboxConfig,
    SandboxResult,
    SandboxRunMode,
    run_command_in_sandbox,
    run_python_in_sandbox,
    run_workspace_file_in_sandbox,
)
from sandbox.workspace_quota import (
    FileState as _FileState,
    QuotaCleanup as _QuotaCleanup,
    cleanup_quota_created_entries as _cleanup_quota_created_entries,
    snapshot_workspace as _snapshot_workspace,
)
from tools.output_queue import unqueue_output_file
from tools.registry import MessageContext, ToolRegistry
from tools.workspace.common import (
    ATTACHMENT_HINT,
    UserLocks,
    quota_ok,
    scrub_user_paths,
    tool_error,
    workspace_activity,
)
from tools.workspace.config import DEFAULT_MAX_USER_BYTES
from trust.tiers import TrustTier
from workspace.manager import WorkspaceKey, WorkspaceManager, remove_owned_tree

MAX_STDIN_BYTES = 100_000
MAX_INLINE_CODE_BYTES = 100_000
MAX_ARG_COUNT = 32
MAX_ARG_BYTES = 4096
MAX_CHANGED_FILES_REPORTED = 50
RUN_FILE_MODES = {"auto", "python", "shell", "direct"}


@asynccontextmanager
async def _no_op_async_context():
    yield


# --- Networked runs (run_code in host/netns mode) ---
# Per-user rolling weekly cap, counted from marker rows on this surface,
# mirroring the research/pages precedent. The deployment setting defaults to a
# finite value; 0 remains an explicit operator override that disables the cap.
NETWORK_RUN_LIMIT_SURFACE = "run_code"
NETWORK_RUN_LIMIT_WINDOW = timedelta(days=7)
DEFAULT_NETWORK_WEEKLY_LIMIT = 100
# Per-user venv living in the workspace, created --copies (no symlinks the
# sweeper/path API reject). Python-mode runs auto-use it when healthy.
_VENV_DIR = ".venv"
_VENV_PYTHON = "/work/.venv/bin/python3"
MAX_PIP_SPECS = 16
MAX_PIP_SPECS_BYTES = 2048
# A single PEP 508-ish requirement: name[extras](version specifiers). Deliberately
# tight: no leading dash (pip flags), slash/colon (URLs, local paths), or
# whitespace. Runs as a separate argv element, so this only guards flag injection
# and obvious junk, not shell metacharacters (there is no shell).
_PIP_SPEC_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9_,.-]+\])?"
    r"([<>=!~]=?[A-Za-z0-9.*+!_-]+(,[<>=!~]=?[A-Za-z0-9.*+!_-]+)*)?$"
)


@dataclass
class CodeExecRuntimeGuards:
    """Process-wide execution, VPN, conflict, and egress-quota coordination."""

    semaphore: asyncio.Semaphore
    netns_lease: NetnsLease
    network_quota_locks: UserLocks
    network_weekly_limit: int
    netns_conflict: Callable[[str, str], bool] | None = None
    # Ask the browser service to close the named user's idle worker and drop
    # the physical lease now rather than after its idle TTL. Returns whether the
    # lease was released. Managed coding jobs use it before waiting on the lease.
    netns_yield: Callable[[str], Awaitable[bool]] | None = None

    @classmethod
    def create(
        cls,
        *,
        max_concurrency: int,
        network_weekly_limit: int,
        netns_lease: NetnsLease | None = None,
        netns_conflict: Callable[[str, str], bool] | None = None,
        netns_yield: Callable[[str], Awaitable[bool]] | None = None,
    ) -> CodeExecRuntimeGuards:
        return cls(
            semaphore=asyncio.Semaphore(max(1, max_concurrency)),
            netns_lease=netns_lease or NetnsLease(),
            network_quota_locks=UserLocks(),
            network_weekly_limit=network_weekly_limit,
            netns_conflict=netns_conflict,
            netns_yield=netns_yield,
        )

    async def reserve_network_run(
        self,
        *,
        usage_store: Any | None,
        user_id: str,
        user_name: str,
        channel_id: str,
        guild_id: str | None,
        trust_tier: TrustTier,
        operation: str,
    ) -> str | None:
        """Atomically reserve one rolling-week network execution slot."""

        if self.network_weekly_limit <= 0 or trust_tier >= TrustTier.STAFF:
            return None
        if usage_store is None:
            return tool_error(
                "networked execution is temporarily unavailable because usage "
                "accounting is not ready"
            )
        async with self.network_quota_locks.for_user(WorkspaceKey(user_id)):
            limit_error = await _network_limit_error(
                usage_store, user_id, self.network_weekly_limit
            )
            if limit_error is not None:
                return limit_error
            await usage_store.record_usage_marker(
                user_id=user_id,
                user_name=user_name,
                channel_id=channel_id,
                guild_id=guild_id,
                surface=NETWORK_RUN_LIMIT_SURFACE,
                operation=operation,
            )
        return None


def init_code_exec_tool(
    registry: ToolRegistry,
    workspace_manager: WorkspaceManager,
    sandbox_config: SandboxConfig,
    *,
    locks: UserLocks,
    min_tier: TrustTier = TrustTier.MEMBER,
    max_concurrency: int = 1,
    max_user_bytes: int = DEFAULT_MAX_USER_BYTES,
    network_weekly_limit: int = DEFAULT_NETWORK_WEEKLY_LIMIT,
    netns_lease: NetnsLease | None = None,
    netns_conflict: Callable[[str, str], bool] | None = None,
    runtime_guards: CodeExecRuntimeGuards | None = None,
) -> None:
    # Bound how many sandboxes run at once across the bot, independent of the
    # per-user response lock; one user can still queue several roots. Excess
    # runs wait here rather than fail.
    guards = runtime_guards or CodeExecRuntimeGuards.create(
        max_concurrency=max_concurrency,
        network_weekly_limit=network_weekly_limit,
        netns_lease=netns_lease,
        netns_conflict=netns_conflict,
    )

    async def _reserve_network_quota(ctx: MessageContext, operation: str) -> str | None:
        """Check-and-reserve one network-run slot; None if allowed. STAFF exempt."""
        return await guards.reserve_network_run(
            usage_store=ctx.usage_store,
            user_id=ctx.user_id,
            user_name=ctx.user_name,
            channel_id=ctx.conversation_channel_id,
            guild_id=ctx.guild_id,
            trust_tier=ctx.trust_tier,
            operation=operation,
        )

    # `locks` is the SAME UserLocks the workspace file tools hold (wired in
    # app/tools.py). Holding the per-workspace lock across the sandbox run makes a
    # running script mutually exclusive with write_file/edit_file/multi_edit/etc.
    # on the same workspace, which closes the resolve->write symlink-swap TOCTOU:
    # a script cannot swap a path component to a symlink during another tool's
    # check->write window, because no such tool runs while the script holds the
    # lock. A leftover symlink after the run is static and rejected at resolve.

    async def _run_workspace_file_impl(
        args: dict,
        ctx: MessageContext,
        *,
        forced_mode: SandboxRunMode | None,
        inline_code: str | None = None,
        networked: bool = False,
        pip_specs: tuple[str, ...] = (),
        _lease_held: bool = False,
    ) -> str:
        active_config = sandbox_config
        stdin_arg = args.get("stdin")
        if stdin_arg is not None and not isinstance(stdin_arg, str):
            return tool_error("stdin must be a string")
        if isinstance(stdin_arg, str) and len(stdin_arg.encode("utf-8")) > MAX_STDIN_BYTES:
            return tool_error(f"stdin exceeds the {MAX_STDIN_BYTES} byte limit")
        if not _lease_held:
            run_lease = (
                guards.netns_lease
                if networked and sandbox_config.network_mode == "netns"
                else guards.semaphore
            )
            try:
                async with run_lease, workspace_activity(locks, ctx):
                    return await _run_workspace_file_impl(
                        args,
                        ctx,
                        forced_mode=forced_mode,
                        inline_code=inline_code,
                        networked=networked,
                        pip_specs=pip_specs,
                        _lease_held=True,
                    )
            except Exception as e:
                return tool_error(scrub_user_paths(str(e), workspace_manager, ctx.workspace_key))
        try:
            mode = forced_mode
            argv: tuple[str, ...] = ()
            path: Path | None = None
            pip_only = False
            if inline_code is None:
                path_arg = str(args.get("path", "")).strip()
                if not path_arg:
                    if networked and pip_specs:
                        # "install X into my venv" with no script to run.
                        pip_only = True
                        rel = "<pip install>"
                    else:
                        return tool_error("path is required")
                else:
                    path = workspace_manager.resolve_user_file_path(
                        ctx.workspace_key,
                        path_arg,
                        must_exist=True,
                    )
                    if path.is_symlink() or not path.is_file():
                        return tool_error("path is not a file")
                    if mode is None:
                        try:
                            mode = _coerce_mode(args.get("mode")) or _auto_mode(path)
                            argv = _coerce_argv(args.get("argv"))
                        except _ToolArgumentError as e:
                            return tool_error(str(e))
                        if mode == "direct":
                            _ensure_owner_executable(path)
                    rel = workspace_manager.relative_user_file_path(ctx.workspace_key, path)
            else:
                try:
                    argv = _coerce_argv(args.get("argv"))
                except _ToolArgumentError as e:
                    return tool_error(str(e))
                rel = "<inline code>"

            if networked:
                # Reserve a weekly slot before the (expensive) run; a reserved run
                # counts even if it later fails, since it consumed VPN egress.
                quota_error = await _reserve_network_quota(ctx, "run")
                if quota_error is not None:
                    return quota_error

            workspace_dir = workspace_manager.user_files_dir(ctx.workspace_key)
            interpreter = "default"
            # Plain sandboxes use their concurrency semaphore. Networked sandboxes
            # instead use the single shared netns lease, which is also held for a
            # browser worker's complete lifetime. The per-workspace lock serializes
            # either kind of run against file writes. Order is always global lease
            # -> workspace lock, and write tools take only the latter, so no deadlock.
            async with _no_op_async_context():
                # The before/after workspace walks are offloaded off the event loop:
                # a large workspace (up to CODE_EXEC_MAX_WORKSPACE_FILES) must not
                # stall the shared loop, and the walk holds the GIL between syscalls.
                before, before_complete = await asyncio.to_thread(
                    _snapshot_workspace,
                    workspace_manager,
                    ctx.workspace_key,
                    max_workspace_files=active_config.max_workspace_files,
                    max_env_roots=active_config.max_env_files,
                )
                temp_script: Path | None = None
                result: SandboxResult | None = None
                quota_cleanup: _QuotaCleanup | None = None
                skip_payload = False
                try:
                    # Networked runs with pip_install: create/repair the venv, then
                    # install, before the payload. A creation or install failure is
                    # surfaced directly and the payload is skipped.
                    if networked and pip_specs:
                        venv_failure = await _ensure_venv(active_config, workspace_dir)
                        if venv_failure is not None:
                            result = venv_failure
                            skip_payload = True
                        else:
                            install_result = await _pip_install(
                                active_config, workspace_dir, pip_specs
                            )
                            if install_result.exit_code != 0 or pip_only:
                                result = install_result
                                skip_payload = True
                    if not skip_payload:
                        # Inline runs carry their run mode in forced_mode (python or
                        # shell); path runs resolved `mode` above.
                        run_mode = (
                            forced_mode
                            if inline_code is not None
                            else ("python" if forced_mode == "python" else mode)
                        )
                        if inline_code is not None:
                            # Inline code becomes a transient workspace file: written
                            # under the same lock every write tool holds, run through
                            # the same sandbox profile, and removed before the
                            # artifact diff so it never rides the attachment rail.
                            # Capability-wise this equals write_file + run.
                            payload = inline_code.encode("utf-8")
                            suffix = ".sh" if run_mode == "shell" else ".py"
                            temp_script = workspace_dir / f".inline-{uuid.uuid4().hex}{suffix}"
                            if not quota_ok(
                                workspace_manager,
                                ctx.workspace_key,
                                new_size=len(payload),
                                destination=temp_script,
                                temp_path=None,
                                max_user_bytes=max_user_bytes,
                            ):
                                return tool_error(
                                    "running inline code would exceed your workspace "
                                    "quota; delete some files first"
                                )
                            temp_script.write_bytes(payload)
                            path = temp_script
                        assert path is not None
                        # A healthy per-user venv becomes the toolchain for the run:
                        # its interpreter for python mode, and its bin/ on PATH so
                        # installed console scripts (pip, pytest, pio, ...) resolve in
                        # shell/direct mode. Packages installed by a networked run
                        # remain visible to later offline runs too.
                        effective_config = active_config
                        if _venv_is_healthy(workspace_dir):
                            interpreter = "workspace_venv"
                            effective_config = replace(
                                active_config,
                                python_bin_override=(_VENV_PYTHON if run_mode == "python" else ""),
                                extra_env=(
                                    *active_config.extra_env,
                                    ("PATH", "/work/.venv/bin:/usr/bin"),
                                ),
                            )
                        if run_mode == "python":
                            # Python files and inline Python use the isolated interpreter.
                            result = await run_python_in_sandbox(
                                effective_config,
                                workspace_dir,
                                path,
                                stdin=stdin_arg if isinstance(stdin_arg, str) else None,
                                argv=argv,
                            )
                        else:
                            assert run_mode is not None
                            result = await run_workspace_file_in_sandbox(
                                effective_config,
                                workspace_dir,
                                path,
                                stdin=stdin_arg if isinstance(stdin_arg, str) else None,
                                mode=run_mode,
                                argv=argv,
                            )
                finally:
                    # Unlink before the diff below so the temp script is not
                    # reported as a changed workspace file.
                    if temp_script is not None:
                        temp_script.unlink(missing_ok=True)
                assert result is not None
                if result.quota_exceeded:
                    quota_cleanup = await asyncio.to_thread(
                        _cleanup_quota_created_entries,
                        workspace_manager,
                        ctx.workspace_key,
                        before,
                        remove_preexisting_envs=result.environment_quota_exceeded,
                        remove_new_ordinary=before_complete,
                    )
                changed_files = (
                    []
                    if not before_complete
                    else await asyncio.to_thread(
                        _changed_workspace_files,
                        workspace_manager,
                        ctx.workspace_key,
                        before,
                        max_workspace_files=active_config.max_workspace_files,
                        max_env_roots=active_config.max_env_files,
                    )
                )
                workspace_root = workspace_manager.user_files_dir(ctx.workspace_key).resolve()
                stale_outputs = await asyncio.to_thread(
                    _unavailable_queued_workspace_files,
                    ctx.output_files,
                    workspace_root,
                )
                for output in stale_outputs:
                    unqueue_output_file(ctx, output)
            reported_changed_files = changed_files[:MAX_CHANGED_FILES_REPORTED]
            changed_files_payload = _changed_files_payload(reported_changed_files, ctx)
            run_report: dict[str, object] = {
                "path": rel,
                "mode": mode,
                "network": networked,
                "interpreter": interpreter,
                "exit_code": result.exit_code,
                "timed_out": result.timed_out,
                "duration_ms": result.duration_ms,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "changed_file_count": len(changed_files),
                "changed_files_truncated": len(changed_files) > MAX_CHANGED_FILES_REPORTED,
                "changed_files": changed_files_payload,
                "attached_files": [
                    item["path"] for item in changed_files_payload if item["queued"]
                ],
            }
            if len(changed_files) > MAX_CHANGED_FILES_REPORTED or any(
                not item["queued"] for item in changed_files_payload
            ):
                run_report["attachment_hint"] = ATTACHMENT_HINT
            if quota_cleanup is not None:
                retained_changes = sum(item["status"] == "modified" for item in changed_files)
                run_report["quota_exceeded"] = True
                run_report["quota_cleanup"] = {
                    "removed_entries": quota_cleanup.removed_entries,
                    "removed_bytes": quota_cleanup.removed_bytes,
                    "removed_env_dirs": quota_cleanup.removed_env_dirs,
                    "retained_preexisting_changes": retained_changes,
                    "note": (
                        "The pre-run workspace snapshot hit its entry ceiling; no "
                        "ordinary paths were removed because their prior state could "
                        "not be proven safely; environment roots are still removed "
                        "when their own quota caused the rejection."
                        if not before_complete
                        else "The environment exceeded its dedicated quota, so "
                        "regenerable .venv/.pio trees were removed. Pre-existing "
                        "ordinary files were retained."
                        if result.environment_quota_exceeded
                        else "New paths created by the violating run were removed. "
                        "Changes to paths that existed before the run were retained "
                        "to avoid destructive rollback."
                    ),
                }
            return json.dumps(run_report)
        except NetnsLeaseSafetyError:
            raise
        except Exception as e:
            return tool_error(scrub_user_paths(str(e), workspace_manager, ctx.workspace_key))

    async def _run_code(args: dict, ctx: MessageContext) -> str:
        networked = sandbox_config.network_mode != "none"
        netns_run = sandbox_config.network_mode == "netns"
        if netns_run and (
            ctx.browser_netns_claimed
            or (
                guards.netns_conflict is not None
                and guards.netns_conflict(ctx.user_id, ctx.tool_event_turn_id)
            )
        ):
            return tool_error(
                "networked code cannot run after the browser in the same turn; retry later"
            )
        code_arg = args.get("code")
        path_arg = str(args.get("path", "")).strip()
        try:
            pip_specs = _coerce_pip_specs(args.get("pip_install"))
            code_mode = _coerce_mode(args.get("mode"))
        except _ToolArgumentError as e:
            return tool_error(str(e))
        has_code = isinstance(code_arg, str) and bool(code_arg.strip())
        if code_arg is not None and not has_code:
            return tool_error("code must be a non-empty string")
        if has_code:
            if path_arg:
                return tool_error("pass either code or path, not both")
            if len(cast(str, code_arg).encode("utf-8")) > MAX_INLINE_CODE_BYTES:
                return tool_error(f"code exceeds the {MAX_INLINE_CODE_BYTES} byte limit")
        if not has_code and not path_arg and not pip_specs:
            return tool_error("pass at least one of code, path, or pip_install")
        if pip_specs and not networked:
            return tool_error("pip_install requires host or netns network mode")
        # Inline code runs as Python by default, or as a shell script with
        # mode=shell (one call for `git clone ...`, `pio run ...`, etc.).
        inline_forced: SandboxRunMode | None = None
        if has_code:
            inline_forced = "shell" if code_mode == "shell" else "python"
        if netns_run:
            ctx.networked_exec_inflight = True
        try:
            return await _run_workspace_file_impl(
                args,
                ctx,
                forced_mode=inline_forced,
                inline_code=cast(str, code_arg) if has_code else None,
                networked=networked,
                pip_specs=pip_specs,
            )
        finally:
            if netns_run:
                ctx.networked_exec_inflight = False

    network_description = {
        "none": "Network access is disabled; pip_install is unavailable.",
        "host": (
            "Internet access uses the bot host's network namespace and can reach "
            "anything the host can reach."
        ),
        "netns": (
            "Internet access uses an operator-provisioned network namespace that "
            "keeps the host and private network unreachable."
        ),
    }[sandbox_config.network_mode]
    registry.register(
        name="run_code",
        description=(
            "Run inline code or a workspace file in a locked Linux sandbox. "
            f"{network_description} Python is the default for inline code; mode=shell "
            "runs an inline shell script. Workspace files support auto, python, shell, "
            "and direct modes. In networked modes, pip_install creates or updates a "
            "persistent workspace .venv. Files created or modified by the run remain "
            "in the workspace but are not attached automatically; call queue_file with a "
            "reported changed-file path to include it with the final reply."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Workspace-relative file. Pass path or code, not both.",
                },
                "code": {
                    "type": "string",
                    "description": "Inline Python, or an inline shell script with mode=shell.",
                },
                "pip_install": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": MAX_PIP_SPECS,
                    "description": (
                        "Package requirements to install into the persistent workspace .venv. "
                        "Requires host or netns network mode."
                    ),
                },
                "mode": {
                    "type": "string",
                    "enum": sorted(RUN_FILE_MODES),
                    "description": (
                        "For files: auto/python/shell/direct. For inline code: Python by "
                        "default, or shell when explicitly selected."
                    ),
                },
                "argv": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": MAX_ARG_COUNT,
                    "description": "Optional command-line arguments.",
                },
                "stdin": {
                    "type": "string",
                    "description": "Optional text piped to standard input.",
                },
            },
        },
        handler=_run_code,
        min_tier=min_tier,
    )


class _ToolArgumentError(ValueError):
    pass


def _coerce_mode(raw: object) -> SandboxRunMode | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise _ToolArgumentError("mode must be a string")
    mode = raw.strip().lower()
    if not mode or mode == "auto":
        return None
    if mode not in RUN_FILE_MODES:
        allowed = ", ".join(sorted(RUN_FILE_MODES))
        raise _ToolArgumentError(f"mode must be one of: {allowed}")
    return cast(SandboxRunMode, mode)


def _auto_mode(path: Path) -> SandboxRunMode:
    suffix = path.suffix.lower()
    if suffix == ".py":
        return "python"
    if suffix in {".sh", ".bash"}:
        return "shell"
    return "direct"


def _coerce_argv(raw: object) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise _ToolArgumentError("argv must be an array of strings")
    if len(raw) > MAX_ARG_COUNT:
        raise _ToolArgumentError(f"argv may contain at most {MAX_ARG_COUNT} items")
    argv: list[str] = []
    total_bytes = 0
    for item in raw:
        if not isinstance(item, str):
            raise _ToolArgumentError("argv must be an array of strings")
        if "\x00" in item:
            raise _ToolArgumentError("argv entries may not contain NUL bytes")
        total_bytes += len(item.encode("utf-8"))
        if total_bytes > MAX_ARG_BYTES:
            raise _ToolArgumentError(f"argv exceeds the {MAX_ARG_BYTES} byte limit")
        argv.append(item)
    return tuple(argv)


def _ensure_owner_executable(path: Path) -> None:
    if os.name != "posix":
        return
    current_mode = path.stat().st_mode
    if current_mode & stat.S_IXUSR:
        return
    path.chmod(current_mode | stat.S_IXUSR)


async def _network_limit_error(store: Any, user_id: str, limit: int) -> str | None:
    now = datetime.now(UTC)
    events = await store.usage_markers(
        user_id,
        surfaces=(NETWORK_RUN_LIMIT_SURFACE,),
        since=now - NETWORK_RUN_LIMIT_WINDOW,
    )
    used = sum(event.unit_count for event in events)
    if used < limit:
        return None
    reset_at = min(event.created_at for event in events) + NETWORK_RUN_LIMIT_WINDOW
    epoch = int(reset_at.timestamp())
    return tool_error(
        f"Weekly network-run limit reached: this user has used {used} of {limit} "
        f"networked runs/builds in the past 7 days. Capacity frees up <t:{epoch}:R>. "
        "Tell the user they are at their weekly limit and include that Discord "
        "timestamp verbatim so it renders in their local time. Do not retry before then."
    )


def _coerce_pip_specs(raw: object) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise _ToolArgumentError("pip_install must be an array of requirement strings")
    if len(raw) > MAX_PIP_SPECS:
        raise _ToolArgumentError(f"pip_install accepts at most {MAX_PIP_SPECS} packages")
    specs: list[str] = []
    total = 0
    for item in raw:
        if not isinstance(item, str):
            raise _ToolArgumentError("pip_install entries must be strings")
        spec = item.strip()
        total += len(spec.encode("utf-8"))
        if total > MAX_PIP_SPECS_BYTES:
            raise _ToolArgumentError("pip_install requirements are too long")
        if not _PIP_SPEC_RE.match(spec):
            raise _ToolArgumentError(
                f"invalid package requirement {item!r} (no flags, URLs, or paths)"
            )
        specs.append(spec)
    return tuple(specs)


def _venv_is_healthy(workspace_dir: Path) -> bool:
    venv = workspace_dir / _VENV_DIR
    cfg = venv / "pyvenv.cfg"
    python = venv / "bin" / "python3"
    try:
        return cfg.is_file() and python.is_file() and not python.is_symlink()
    except OSError:
        return False


async def _ensure_venv(config: SandboxConfig, workspace_dir: Path) -> SandboxResult | None:
    """Ensure a healthy per-user .venv exists, recreating a broken one.

    Returns None when the venv is ready; a SandboxResult when creation failed
    (surface it to the model). --copies avoids the symlinked interpreter the
    sweeper never removes and the path API rejects.
    """
    if _venv_is_healthy(workspace_dir):
        return None
    venv = workspace_dir / _VENV_DIR
    if venv.exists():
        with suppress(OSError):
            await asyncio.to_thread(remove_owned_tree, venv)
    result = await run_command_in_sandbox(
        config,
        workspace_dir,
        [config.python_bin, "-m", "venv", "--copies", "/work/.venv"],
    )
    if result.exit_code != 0 or not _venv_is_healthy(workspace_dir):
        return result
    return None


async def _pip_install(
    config: SandboxConfig, workspace_dir: Path, specs: tuple[str, ...]
) -> SandboxResult:
    return await run_command_in_sandbox(
        config,
        workspace_dir,
        [
            _VENV_PYTHON,
            "-m",
            "pip",
            "install",
            "--no-cache-dir",
            "--disable-pip-version-check",
            *specs,
        ],
    )


def _changed_workspace_files(
    workspace_manager: WorkspaceManager,
    user_id: WorkspaceKey,
    before: dict[str, _FileState],
    *,
    max_workspace_files: int,
    max_env_roots: int,
) -> list[dict[str, object]]:
    root = workspace_manager.user_files_dir(user_id)
    changed: list[dict[str, object]] = []
    after, complete = _snapshot_workspace(
        workspace_manager,
        user_id,
        max_workspace_files=max_workspace_files,
        max_env_roots=max_env_roots,
    )
    if not complete:
        return []
    for rel, state in after.items():
        if state.kind != "file":
            continue
        previous = before.get(rel)
        if previous is not None and previous == state:
            continue
        path = root.joinpath(*rel.split("/"))
        changed.append(
            {
                "path": rel,
                "abs_path": path.resolve(strict=False),
                "size_bytes": state.size,
                "status": "created" if previous is None else "modified",
            }
        )
    changed.sort(key=lambda item: str(item["path"]))
    return changed


def _changed_files_payload(
    changed_files: list[dict[str, object]],
    ctx: MessageContext,
) -> list[dict[str, object]]:
    attached_paths = set(ctx.output_files)
    payload: list[dict[str, object]] = []
    for item in changed_files:
        rel = str(item["path"])
        abs_path = item["abs_path"]
        queued = (
            isinstance(abs_path, Path) and str(abs_path.resolve(strict=False)) in attached_paths
        )
        entry: dict[str, object] = {
            "path": rel,
            "status": item["status"],
            "size_bytes": item["size_bytes"],
            "queued": queued,
        }
        payload.append(entry)
    return payload


def _unavailable_queued_workspace_files(output_files: list[str], workspace_root: Path) -> list[str]:
    stale: list[str] = []
    for output in output_files:
        path = Path(output)
        if not path.is_relative_to(workspace_root):
            continue
        try:
            resolved = path.resolve(strict=True)
        except OSError:
            stale.append(output)
            continue
        if path.is_symlink() or not path.is_file() or not resolved.is_relative_to(workspace_root):
            stale.append(output)
    return stale
