from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from branding import DEFAULT_BOT_SLUG


class SandboxUnavailableError(RuntimeError):
    """Raised when executable skills cannot be isolated safely."""


@dataclass(frozen=True)
class SandboxRuntime:
    bwrap: str
    prlimit: str


@dataclass(frozen=True)
class ScriptSandboxLimits:
    """Per-process prlimit ceilings plus the tmpfs cap, not an aggregate budget.

    Every field except tmpfs_bytes becomes a prlimit rlimit, which the kernel
    applies to each process separately, so a forking script can multiply the
    totals. The process count is additionally per-real-UID and therefore shared
    with the rest of the service account. The example systemd unit at
    ``deploy/kimi.service.example`` adds aggregate memory, CPU, and PID caps
    around the bot and its children.
    """

    memory_bytes: int = 2048 * 1024 * 1024
    cpu_seconds: int = 300
    file_size_bytes: int = 100 * 1024 * 1024
    open_files: int = 256
    processes: int = 64
    tmpfs_bytes: int = 256 * 1024 * 1024


def detect_sandbox_runtime() -> SandboxRuntime:
    """Resolve the mandatory Linux sandbox executables, with no fallback."""

    if sys.platform != "linux":
        raise SandboxUnavailableError(
            "Executable skill tools require Linux; unsandboxed execution is disabled"
        )
    geteuid = getattr(os, "geteuid", None)
    if geteuid is not None and geteuid() == 0:
        raise SandboxUnavailableError(
            "Executable skill tools require an unprivileged service account; "
            "refusing to run as root"
        )

    missing: list[str] = []
    resolved: dict[str, str] = {}
    for name in ("bwrap", "prlimit"):
        path = shutil.which(name)
        if not path or not os.access(path, os.X_OK):
            missing.append(name)
        else:
            resolved[name] = str(Path(path).resolve())
    if missing:
        joined = ", ".join(missing)
        raise SandboxUnavailableError(
            f"Executable skill tools require {joined}; unsandboxed execution is disabled"
        )
    return SandboxRuntime(bwrap=resolved["bwrap"], prlimit=resolved["prlimit"])


def _base_bwrap_command(runtime: SandboxRuntime, *, allow_network: bool) -> list[str]:
    """Namespace and mount skeleton shared by every executable-skill invocation.

    Unlike sandbox/runner.py and web_browser/service.py, this surface installs no
    seccomp filter. It runs operator-installed scripts from the private store,
    which skills/README.md requires be reviewed like application code, rather
    than model-generated code. Namespaces, a full capability drop, disabled
    nested user namespaces, and the prlimit ceilings are the boundary here.
    """

    command = [
        runtime.bwrap,
        "--unshare-all",
        "--unshare-user",
    ]
    if allow_network:
        # --unshare-all includes a network namespace. This explicit override is
        # deliberately present only for tools whose declaration opts into the
        # host network.
        command.append("--share-net")
    command.extend(
        [
            "--die-with-parent",
            "--new-session",
            "--disable-userns",
            "--cap-drop",
            "ALL",
            "--hostname",
            f"{DEFAULT_BOT_SLUG}-skill",
            "--dir",
            "/usr",
            "--ro-bind",
            "/usr/bin",
            "/usr/bin",
            "--ro-bind",
            "/usr/lib",
            "/usr/lib",
            "--ro-bind-try",
            "/usr/lib64",
            "/usr/lib64",
            "--ro-bind-try",
            "/usr/libexec",
            "/usr/libexec",
            "--ro-bind-try",
            "/usr/sbin",
            "/usr/sbin",
            "--ro-bind-try",
            "/usr/share",
            "/usr/share",
            "--ro-bind-try",
            "/lib",
            "/lib",
            "--ro-bind-try",
            "/lib64",
            "/lib64",
            "--symlink",
            "usr/bin",
            "/bin",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
        ]
    )
    return command


def _covered_by_base_mount(path: Path) -> bool:
    if path == Path("/usr"):
        # A conventional system Python prefix is satisfied by the narrower
        # runtime subtrees above; do not expose unrelated content under /usr.
        return True
    roots = map(
        Path,
        (
            "/usr/bin",
            "/usr/lib",
            "/usr/lib64",
            "/usr/libexec",
            "/usr/sbin",
            "/usr/share",
            "/lib",
            "/lib64",
        ),
    )
    return any(path == root or path.is_relative_to(root) for root in roots)


def _runtime_mounts(interpreter: Path) -> list[Path]:
    """Return non-FHS Python/runtime roots needed by the selected interpreter."""

    candidates = [interpreter.parent, interpreter.resolve().parent]
    if interpreter.resolve() == Path(sys.executable).resolve():
        candidates.extend((Path(sys.prefix), Path(sys.base_prefix)))

    mounts: list[Path] = []
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved == Path("/"):
            raise SandboxUnavailableError(
                "Refusing to expose the host root as an interpreter mount"
            )
        if _covered_by_base_mount(resolved):
            continue
        if not resolved.exists():
            raise SandboxUnavailableError(f"Interpreter runtime path does not exist: {resolved}")
        if any(resolved == parent or resolved.is_relative_to(parent) for parent in mounts):
            continue
        mounts = [child for child in mounts if not child.is_relative_to(resolved)]
        mounts.append(resolved)
    return sorted(mounts, key=lambda item: (len(item.parts), str(item)))


def _network_mount_args() -> list[str]:
    args = ["--dir", "/etc"]
    for path in (
        "/etc/resolv.conf",
        "/etc/hosts",
        "/etc/nsswitch.conf",
        "/etc/gai.conf",
        "/etc/ssl/certs",
        "/etc/ca-certificates",
    ):
        args.extend(("--ro-bind-try", path, path))
    return args


def build_sandbox_command(
    *,
    runtime: SandboxRuntime,
    limits: ScriptSandboxLimits,
    interpreter: Path,
    resolved_script: Path,
    skill_dir: Path,
    workspace_dir: Path | None,
    allow_network: bool,
) -> list[str]:
    """Build the fixed Bubblewrap/prlimit command for one skill invocation."""

    skill_root = skill_dir.resolve()
    script = resolved_script.resolve()
    try:
        script_relative = script.relative_to(skill_root)
    except ValueError as exc:
        raise ValueError(f"Script path escapes skill directory: {resolved_script}") from exc

    interpreter_path = interpreter.absolute()
    if not interpreter_path.exists():
        raise SandboxUnavailableError(f"Interpreter does not exist: {interpreter_path}")
    command = [
        runtime.prlimit,
        f"--as={limits.memory_bytes}",
        f"--cpu={limits.cpu_seconds}",
        f"--fsize={limits.file_size_bytes}",
        f"--nofile={limits.open_files}",
        f"--nproc={limits.processes}",
        "--core=0",
        "--",
        *_base_bwrap_command(runtime, allow_network=allow_network),
        "--size",
        str(limits.tmpfs_bytes),
        "--tmpfs",
        "/tmp",
    ]
    # Runtime roots may themselves live under /tmp (common in ephemeral
    # deployments), so add them after the tmpfs instead of masking them with it.
    for mount in _runtime_mounts(interpreter_path):
        command.extend(("--ro-bind", str(mount), str(mount)))

    command.extend(
        (
            "--ro-bind",
            str(skill_root),
            "/skill",
            "--dir",
            "/tmp/home",
        )
    )
    if workspace_dir is not None:
        workspace = workspace_dir.resolve()
        if not workspace.is_dir():
            raise SandboxUnavailableError(f"Skill workspace is not a directory: {workspace}")
        command.extend(("--bind", str(workspace), "/workspace"))
    if allow_network:
        command.extend(_network_mount_args())

    command.extend(
        (
            "--chdir",
            "/skill",
            "--",
            str(interpreter_path),
            str(PurePosixPath("/skill") / script_relative.as_posix()),
        )
    )
    return command


def validate_sandbox_runtime(limits: ScriptSandboxLimits) -> SandboxRuntime:
    """Run a minimal namespace probe so configured script tools fail at boot.

    The probe applies the same prlimit ceilings real invocations will use, so
    a host that cannot start a jail under the configured limits fails here, by
    name, instead of registering tools whose every run would die at clone().
    ``limits`` is required on purpose: a probe that quietly falls back to
    defaults certifies ceilings nobody runs.
    """

    runtime = detect_sandbox_runtime()
    true_path = shutil.which("true")
    if true_path is None:
        raise SandboxUnavailableError("Executable skill sandbox probe requires the true utility")
    resolved_true = Path(true_path).resolve()
    if not _covered_by_base_mount(resolved_true):
        raise SandboxUnavailableError(
            f"Executable skill sandbox probe utility is outside the system runtime: {resolved_true}"
        )

    command = [
        runtime.prlimit,
        f"--as={limits.memory_bytes}",
        f"--cpu={limits.cpu_seconds}",
        f"--fsize={limits.file_size_bytes}",
        f"--nofile={limits.open_files}",
        f"--nproc={limits.processes}",
        "--core=0",
        "--",
        *_base_bwrap_command(runtime, allow_network=False),
        "--",
        str(resolved_true),
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            env={"PATH": "/usr/bin:/bin"},
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SandboxUnavailableError(f"Executable skill sandbox probe failed: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode(errors="replace").strip().splitlines()
        suffix = f": {detail[-1]}" if detail else ""
        if any("Resource temporarily unavailable" in line for line in detail):
            # RLIMIT_NPROC counts every process of the service uid, so the
            # configured ceiling has to clear what the account already runs.
            # EAGAIN has other per-uid sources too, hence "likely".
            suffix += (
                f" (likely the uid's process count exceeds the configured "
                f"SCRIPT_SANDBOX_MAX_PROCESSES={limits.processes})"
            )
        raise SandboxUnavailableError(
            f"Executable skill sandbox probe exited {completed.returncode}{suffix}"
        )
    return runtime
