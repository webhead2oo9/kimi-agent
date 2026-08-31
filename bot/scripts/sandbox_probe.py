"""Report why the code-execution sandbox is or is not available on this host.

``sandbox_available()`` deliberately collapses every prerequisite into one
boolean so the tool layer fails closed. That is the right shape for the
runtime and the wrong shape for a person setting up a host or a CI runner,
who needs to know *which* prerequisite is missing. This script builds the
same ``SandboxConfig`` startup builds from the live settings, runs the gate's
checks in the gate's order, prints each result as soon as it is known, and
then runs the real start probe with debug logging so its stderr is visible.

Run from ``bot/`` as ``python -m scripts.sandbox_probe``; it reads ``.env`` or
``ENV_FILE`` exactly as the bot does. Exit status 0 means a jailed process
actually started with the configured profile and the executable-skill sandbox
probe passed as well; anything else means ``run_code`` would not register.
"""

from __future__ import annotations

from collections.abc import Callable
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys

from app.tools import build_sandbox_config
from config.settings import Settings

# The probe mirrors the gate on purpose, so it reads the gate's own helpers
# rather than re-deriving them and drifting.
from sandbox import runner, seccomp
from sandbox.runner import SandboxConfig, sandbox_available
from skills.sandbox import SandboxUnavailableError, validate_sandbox_runtime

_USER_MANAGER_TIMEOUT_SECONDS = 10.0
_NAME_WIDTH = len("kernel.core_pattern is a file pattern")

# Kernel knobs that decide whether an unprivileged user namespace keeps its
# capabilities. bwrap reports a restriction as "loopback: Failed RTM_NEWADDR:
# Operation not permitted" or "Creating new namespace failed", neither of
# which names the knob.
_USERNS_SYSCTLS = (
    "kernel.unprivileged_userns_clone",
    "kernel.apparmor_restrict_unprivileged_userns",
    "user.max_user_namespaces",
)


def _check_user_manager(config: SandboxConfig) -> tuple[bool, str]:
    """Start one trivial transient scope with the properties every run passes.

    Using the real prefix means a user manager without cgroup delegation for
    pids/memory/cpu fails here, by name, rather than inside the combined gate.
    """

    command = [*runner._build_systemd_run_prefix(config), "true"]
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            env=runner._launch_env(),
            timeout=_USER_MANAGER_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"could not start: {exc}"
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip() or "no stderr"
        return False, f"rc={completed.returncode}: {detail}"
    return True, "transient user scope started with the run properties"


def _check_libseccomp() -> tuple[bool, str]:
    try:
        program = seccomp.seccomp_bpf_bytes()
    except seccomp.SeccompUnavailableError as exc:
        return False, str(exc)
    return True, f"{len(program) // 8} filter instructions"


def _read_core_pattern() -> str:
    try:
        return runner._CORE_PATTERN_PATH.read_text(encoding="utf-8").strip() or "<empty>"
    except OSError as exc:
        return f"<unreadable: {exc}>"


def _userns_sysctls() -> str:
    parts: list[str] = []
    for name in _USERNS_SYSCTLS:
        path = Path("/proc/sys") / name.replace(".", "/")
        try:
            parts.append(f"{name}={path.read_text(encoding='utf-8').strip()}")
        except OSError:
            parts.append(f"{name}=<absent>")
    return " ".join(parts)


def _report(name: str, check: Callable[[], tuple[bool, str]]) -> bool:
    """Print one row as soon as it is known, so a crash cannot eat the report."""

    try:
        ok, detail = check()
    except Exception as exc:
        # A diagnostic must survive its subject; the repr is the finding.
        ok, detail = False, f"check raised {exc!r}"
    print(f"{'ok  ' if ok else 'FAIL'} {name.ljust(_NAME_WIDTH)}  {detail}")
    return ok


def main() -> int:
    settings = Settings()
    config = build_sandbox_config(settings)
    euid = os.geteuid() if hasattr(os, "geteuid") else None
    launch_env = runner._launch_env()

    def which(binary: str) -> Callable[[], tuple[bool, str]]:
        def check() -> tuple[bool, str]:
            found = shutil.which(binary)
            return found is not None, found or "not on PATH"

        return check

    print(
        f"     profile: network_mode={config.network_mode} python_bin={config.python_bin} "
        f"workspace={config.workspace_probe_root} "
        f"(CODE_EXEC_ENABLED={'on' if settings.code_exec_enabled else 'off'})"
    )
    _report("linux", lambda: (sys.platform == "linux", sys.platform))
    _report("not root", lambda: (not runner._running_as_root(), f"euid={euid}"))
    for binary in (config.bwrap_bin, config.prlimit_bin, config.systemd_run_bin):
        _report(binary, which(binary))
    _report(
        config.python_bin,
        lambda: (Path(config.python_bin).exists(), "sandbox interpreter"),
    )
    _report(
        "kernel.core_pattern is a file pattern",
        lambda: (runner._host_core_dump_boundary_safe(), _read_core_pattern()),
    )
    _report(
        "workspace allows execution",
        lambda: (runner._workspace_exec_boundary_safe(config), config.workspace_probe_root),
    )
    _report("libseccomp", _check_libseccomp)
    _report("user systemd manager", lambda: _check_user_manager(config))
    print(
        f"     launch env: XDG_RUNTIME_DIR={launch_env.get('XDG_RUNTIME_DIR', '')} "
        f"DBUS_SESSION_BUS_ADDRESS={launch_env.get('DBUS_SESSION_BUS_ADDRESS', '')}"
    )
    print(f"     user namespaces: {_userns_sysctls()}")

    # The real gate, with its debug logging routed to stderr so the start
    # probe's own failure text (bwrap, prlimit, systemd-run) is not swallowed.
    # A netns or host profile runs its network legs here too.
    logging.basicConfig(level=logging.DEBUG, stream=sys.stderr, format="%(name)s: %(message)s")
    logging.getLogger("sandbox").setLevel(logging.DEBUG)
    available = _report(
        "sandbox_available()",
        lambda: (sandbox_available(config), f"{config.network_mode} profile start probe"),
    )

    def skills_probe() -> tuple[bool, str]:
        try:
            validate_sandbox_runtime()
        except SandboxUnavailableError as exc:
            return False, str(exc)
        return True, "jailed true exited 0"

    skills_ok = _report("executable-skill sandbox probe", skills_probe)
    return 0 if available and skills_ok else 1


if __name__ == "__main__":
    sys.exit(main())
