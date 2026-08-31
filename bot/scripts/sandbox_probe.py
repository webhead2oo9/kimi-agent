"""Report why the code-execution sandbox is or is not available on this host.

``sandbox_available()`` deliberately collapses every prerequisite into one
boolean so the tool layer fails closed. That is the right shape for the
runtime and the wrong shape for a person setting up a host or a CI runner,
who needs to know *which* prerequisite is missing. This script runs the same
checks in the same order and names the first one that fails, then runs the
real start probe with debug logging so its stderr is visible.

Run from ``bot/`` as ``python -m scripts.sandbox_probe``. Exit status 0 means
a jailed process actually started here and the executable-skill sandbox probe
passed as well; anything else means ``run_code`` would not register.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys

# The probe mirrors the gate on purpose, so it reads the gate's own helpers
# rather than re-deriving them and drifting.
from sandbox import runner, seccomp
from sandbox.runner import SandboxConfig, sandbox_available
from skills.sandbox import SandboxUnavailableError, validate_sandbox_runtime

_USER_MANAGER_TIMEOUT_SECONDS = 10.0


def _check_user_manager(config: SandboxConfig) -> tuple[bool, str]:
    """Start one trivial transient scope the way every sandbox run does."""

    command = [config.systemd_run_bin, "--user", "--scope", "--quiet", "--collect", "--", "true"]
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
    return True, "transient user scope started"


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


# Kernel knobs that decide whether an unprivileged user namespace keeps its
# capabilities. bwrap reports a restriction as "loopback: Failed RTM_NEWADDR:
# Operation not permitted" or "Creating new namespace failed", neither of
# which names the knob.
_USERNS_SYSCTLS = (
    "kernel.unprivileged_userns_clone",
    "kernel.apparmor_restrict_unprivileged_userns",
    "user.max_user_namespaces",
)


def _userns_sysctls() -> str:
    parts: list[str] = []
    for name in _USERNS_SYSCTLS:
        path = Path("/proc/sys") / name.replace(".", "/")
        try:
            parts.append(f"{name}={path.read_text(encoding='utf-8').strip()}")
        except OSError:
            parts.append(f"{name}=<absent>")
    return " ".join(parts)


def main() -> int:
    config = SandboxConfig()
    euid = os.geteuid() if hasattr(os, "geteuid") else None
    launch_env = runner._launch_env()

    checks: list[tuple[str, bool, str]] = [
        ("linux", sys.platform == "linux", sys.platform),
        ("not root", not runner._running_as_root(), f"euid={euid}"),
    ]
    for binary in (config.bwrap_bin, config.prlimit_bin, config.systemd_run_bin):
        found = shutil.which(binary)
        checks.append((binary, found is not None, found or "not on PATH"))
    checks.append((config.python_bin, Path(config.python_bin).exists(), "sandbox interpreter"))
    checks.append(
        (
            "kernel.core_pattern is a file pattern",
            runner._host_core_dump_boundary_safe(),
            _read_core_pattern(),
        )
    )
    checks.append(("libseccomp", *_check_libseccomp()))
    checks.append(
        (
            "user systemd manager",
            *_check_user_manager(config),
        )
    )

    width = max(len(name) for name, _, _ in checks)
    for name, ok, detail in checks:
        print(f"{'ok  ' if ok else 'FAIL'} {name.ljust(width)}  {detail}")
    print(
        f"     launch env: XDG_RUNTIME_DIR={launch_env.get('XDG_RUNTIME_DIR', '')} "
        f"DBUS_SESSION_BUS_ADDRESS={launch_env.get('DBUS_SESSION_BUS_ADDRESS', '')}"
    )
    print(f"     user namespaces: {_userns_sysctls()}")

    # The real gate, with its debug logging routed to stderr so the start
    # probe's own failure text (bwrap, prlimit, systemd-run) is not swallowed.
    logging.basicConfig(level=logging.DEBUG, stream=sys.stderr, format="%(name)s: %(message)s")
    logging.getLogger("sandbox").setLevel(logging.DEBUG)
    available = sandbox_available(config)
    print(f"{'ok  ' if available else 'FAIL'} sandbox_available()")

    try:
        validate_sandbox_runtime()
    except SandboxUnavailableError as exc:
        print(f"FAIL executable-skill sandbox probe: {exc}")
        skills_ok = False
    else:
        print("ok   executable-skill sandbox probe")
        skills_ok = True

    return 0 if available and skills_ok else 1


if __name__ == "__main__":
    sys.exit(main())
