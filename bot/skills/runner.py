from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import signal
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from skills.sandbox import (
    ScriptSandboxLimits,
    build_sandbox_command,
    detect_sandbox_runtime,
)
from skills.secrets import scrub_output

log = logging.getLogger(__name__)

INTERPRETERS: dict[str, list[str]] = {
    ".py": [sys.executable],
    ".sh": ["bash"],
    ".js": ["node"],
}
DEFAULT_INTERPRETER = ["bash"]


@dataclass
class ScriptResult:
    stdout: str
    stderr: str
    return_code: int
    timed_out: bool = False
    output_files: list[str] = field(default_factory=list)
    output_files_omitted: int = 0


def validate_script_path(script_path: str, skill_dir: Path) -> Path:
    resolved = (skill_dir / script_path).resolve()
    if not resolved.is_relative_to(skill_dir.resolve()):
        raise ValueError(f"Script path escapes skill directory: {script_path}")
    if not resolved.exists():
        raise FileNotFoundError(f"Script not found: {resolved}")
    if not resolved.is_file():
        raise FileNotFoundError(f"Script is not a file: {resolved}")
    return resolved


def _get_interpreter(script_path: Path) -> Path:
    configured = INTERPRETERS.get(script_path.suffix, DEFAULT_INTERPRETER)[0]
    resolved = configured if os.path.isabs(configured) else shutil.which(configured)
    if not resolved:
        raise FileNotFoundError(f"Interpreter not found for {script_path.suffix or 'script'}")
    return Path(resolved).absolute()


# Env vars that influence how/what code the interpreter loads. A declared secret
# must never be able to set these and hijack the reviewed script's interpreter.
_RESERVED_ENV_NAMES = frozenset(
    {
        "PATH",
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
        "DYLD_INSERT_LIBRARIES",
        "DYLD_LIBRARY_PATH",
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONSTARTUP",
        "NODE_OPTIONS",
        "BASH_ENV",
        "ENV",
        "IFS",
        # The per-run scratch home (set by _build_env). A secret must not be
        # able to redirect library config/cache loading to an attacker path.
        "HOME",
        "USERPROFILE",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "MPLCONFIGDIR",
    }
)


def _build_env(
    secrets: dict[str, str],
    workspace_dir: str | None = None,
    scratch_home: str | None = None,
) -> dict[str, str]:
    env: dict[str, str] = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    if scratch_home:
        # Libraries commonly demand a writable home/config dir (matplotlib hard-
        # fails with "Could not determine home directory" without one). Point
        # every convention at a private per-run scratch dir, never the real
        # home (isolation) and never the workspace (cache files would pollute
        # the user's quota and listings).
        env["HOME"] = scratch_home
        env["USERPROFILE"] = scratch_home
        env["XDG_CACHE_HOME"] = scratch_home
        env["XDG_CONFIG_HOME"] = scratch_home
        env["MPLCONFIGDIR"] = scratch_home
    for key, value in secrets.items():
        if key in _RESERVED_ENV_NAMES:
            log.warning("Ignoring skill secret with reserved env name %r", key)
            continue
        env[key] = value
    if workspace_dir:
        env["WORKSPACE_DIR"] = workspace_dir
    return env


def _decode_scrub_and_cap(
    data: bytes,
    secrets: dict[str, str],
    max_chars: int,
    omitted_bytes: int = 0,
) -> tuple[str, bool]:
    text = scrub_output(data.decode(errors="replace").strip(), secrets)
    if max_chars <= 0 or len(text) <= max_chars:
        if omitted_bytes > 0:
            return f"{text}\n[TRUNCATED {omitted_bytes} bytes]", True
        return text, False
    omitted = len(text) - max_chars
    return f"{text[:max_chars]}\n[TRUNCATED {omitted + omitted_bytes} chars]", True


async def _read_capped_stream(
    stream: asyncio.StreamReader | None,
    max_output_chars: int,
    secrets: dict[str, str],
) -> tuple[bytes, int]:
    if stream is None:
        return b"", 0

    max_secret_len = max(
        (len(value.encode()) for value in secrets.values() if value),
        default=0,
    )
    max_bytes = (max_output_chars * 4) + max_secret_len if max_output_chars > 0 else 0
    chunks: list[bytes] = []
    retained = 0
    omitted = 0

    while True:
        chunk = await stream.read(8192)
        if not chunk:
            break
        if max_bytes <= 0:
            omitted += len(chunk)
            continue

        remaining = max_bytes - retained
        if remaining > 0:
            chunks.append(chunk[:remaining])
            retained += min(len(chunk), remaining)
        if len(chunk) > remaining:
            omitted += len(chunk) - max(remaining, 0)

    return b"".join(chunks), omitted


async def _communicate_capped(
    proc: asyncio.subprocess.Process,
    stdin_data: bytes,
    max_output_chars: int,
    secrets: dict[str, str],
) -> tuple[bytes, int, bytes, int]:
    stdout_task = asyncio.create_task(_read_capped_stream(proc.stdout, max_output_chars, secrets))
    stderr_task = asyncio.create_task(_read_capped_stream(proc.stderr, max_output_chars, secrets))

    try:
        if proc.stdin is not None:
            try:
                proc.stdin.write(stdin_data)
                await proc.stdin.drain()
            except BrokenPipeError, ConnectionResetError:
                # Some scripts don't read stdin at all and may exit successfully
                # before the JSON argument payload is fully written.
                pass
            finally:
                with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                    proc.stdin.close()

        await proc.wait()
        stdout_bytes, stdout_omitted = await stdout_task
        stderr_bytes, stderr_omitted = await stderr_task
        return stdout_bytes, stdout_omitted, stderr_bytes, stderr_omitted
    except BaseException:
        stdout_task.cancel()
        stderr_task.cancel()
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        raise


async def _cleanup_process_group(proc: asyncio.subprocess.Process | None) -> None:
    if proc is None:
        return

    try:
        if os.name == "posix" and proc.pid is not None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                return
            except OSError:
                return
        elif proc.returncode is None:
            proc.kill()

        if proc.returncode is None:
            await proc.wait()
    except Exception:
        log.debug("Process cleanup failed for pid %s", proc.pid, exc_info=True)


def _collect_output_files(
    workspace_dir: str | None,
    max_files: int = 10,
    max_file_bytes: int = 25 * 1024 * 1024,
    max_scan_entries: int = 1000,
) -> tuple[list[str], int]:
    if not workspace_dir:
        return [], 0
    root = Path(workspace_dir).resolve()
    if not root.exists():
        return [], 0

    files: list[str] = []
    omitted = 0
    scanned = 0
    pending_dirs = [root]
    dir_index = 0
    while dir_index < len(pending_dirs):
        directory = pending_dirs[dir_index]
        dir_index += 1
        try:
            entries = sorted(directory.iterdir(), key=lambda path: path.name)
        except OSError:
            continue
        for path in entries:
            scanned += 1
            if scanned > max_scan_entries:
                return files, omitted + 1
            try:
                if path.is_symlink():
                    continue
                resolved = path.resolve()
                if not resolved.is_relative_to(root):
                    continue
                if path.is_dir():
                    pending_dirs.append(path)
                    continue
                if not path.is_file():
                    continue
                if path.stat().st_size > max_file_bytes:
                    omitted += 1
                    continue
            except OSError:
                continue
            if len(files) < max_files:
                files.append(str(resolved))
            else:
                omitted += 1
    return files, omitted


def _protect_secret_output_files(
    output_files: list[str],
    secrets: dict[str, str],
) -> tuple[list[str], int]:
    """Keep output files while ensuring no declared secret value leaves in them.

    Text files containing a secret are scrubbed in place (secret -> [REDACTED])
    and kept; binary files containing a raw secret value are dropped, since they
    cannot be rewritten safely; files free of any secret pass through untouched.
    Returns the kept paths and the count of dropped files.

    Caveat: only the raw secret bytes are detected. A script that re-encodes a
    secret (base64/hex) before writing it can still leak it, the same limitation
    that applies to stdout/stderr scrubbing.
    """
    secret_bytes = [value.encode() for value in secrets.values() if value]
    if not secret_bytes:
        return output_files, 0
    kept: list[str] = []
    dropped = 0
    for path in output_files:
        try:
            data = Path(path).read_bytes()
        except OSError:
            dropped += 1
            continue
        if not any(value in data for value in secret_bytes):
            kept.append(path)
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            dropped += 1
            continue
        try:
            Path(path).write_text(scrub_output(text, secrets), encoding="utf-8")
        except OSError:
            dropped += 1
            continue
        kept.append(path)
    return kept, dropped


async def run_script(
    script_path: str,
    skill_dir: Path,
    arguments: dict,
    secrets: dict[str, str],
    timeout: float = 1200,
    workspace_dir: str | None = None,
    max_output_chars: int = 200000,
    max_output_files: int = 10,
    max_output_file_bytes: int = 25 * 1024 * 1024,
    max_output_scan_entries: int = 1000,
    allow_network: bool = False,
    sandbox_limits: ScriptSandboxLimits | None = None,
    _sandbox_enabled: bool = True,
) -> ScriptResult:
    resolved_path = validate_script_path(script_path, skill_dir)
    interpreter = _get_interpreter(resolved_path)
    scratch_home: str | None = None
    command: list[str]
    cwd: str | None
    if _sandbox_enabled:
        runtime = detect_sandbox_runtime()
        limits = sandbox_limits or ScriptSandboxLimits()
        workspace = Path(workspace_dir) if workspace_dir else None
        command = build_sandbox_command(
            runtime=runtime,
            limits=limits,
            interpreter=interpreter,
            resolved_script=resolved_path,
            skill_dir=skill_dir,
            workspace_dir=workspace,
            allow_network=allow_network,
        )
        env = _build_env(
            secrets,
            "/workspace" if workspace is not None else None,
            scratch_home="/tmp/home",
        )
        env["PATH"] = ":".join((str(interpreter.parent), "/usr/local/bin", "/usr/bin", "/bin"))
        env["TMPDIR"] = "/tmp"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        cwd = None
    else:
        # Test-only orchestration seam. Production registration never disables
        # the sandbox; keeping this explicit lets cross-platform unit tests cover
        # stream caps, redaction, and process cleanup without emulating Linux.
        scratch_home = tempfile.mkdtemp(prefix="skill-home-")
        env = _build_env(secrets, workspace_dir, scratch_home=scratch_home)
        command = [str(interpreter), str(resolved_path)]
        cwd = str(skill_dir)
    stdin_data = json.dumps(arguments).encode()
    proc: asyncio.subprocess.Process | None = None

    try:
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=cwd,
            start_new_session=os.name == "posix",
        )
        stdout_bytes, stdout_omitted, stderr_bytes, stderr_omitted = await asyncio.wait_for(
            _communicate_capped(proc, stdin_data, max_output_chars, secrets),
            timeout=timeout,
        )

        stdout, _ = _decode_scrub_and_cap(
            stdout_bytes,
            secrets,
            max_output_chars,
            stdout_omitted,
        )
        stderr, _ = _decode_scrub_and_cap(
            stderr_bytes,
            secrets,
            max_output_chars,
            stderr_omitted,
        )

        output_files, output_files_omitted = _collect_output_files(
            workspace_dir,
            max_files=max_output_files,
            max_file_bytes=max_output_file_bytes,
            max_scan_entries=max_output_scan_entries,
        )
        if output_files and any(secrets.values()):
            output_files, dropped = _protect_secret_output_files(output_files, secrets)
            if dropped:
                log.warning(
                    "Dropped %d binary output file(s) containing a secret from script: %s",
                    dropped,
                    script_path,
                )
                output_files_omitted += dropped

        return ScriptResult(
            stdout=stdout,
            stderr=stderr,
            return_code=proc.returncode or 0,
            output_files=output_files,
            output_files_omitted=output_files_omitted,
        )

    except TimeoutError:
        log.warning("Script timed out after %ss: %s", timeout, script_path)
        return ScriptResult(
            stdout="",
            stderr=f"Script timed out after {timeout} seconds",
            return_code=-1,
            timed_out=True,
        )
    except Exception as e:
        log.exception("Script execution failed: %s", script_path)
        return ScriptResult(
            stdout="",
            stderr=scrub_output(str(e), secrets),
            return_code=-1,
        )
    finally:
        await _cleanup_process_group(proc)
        if scratch_home is not None:
            await asyncio.to_thread(shutil.rmtree, scratch_home, True)
