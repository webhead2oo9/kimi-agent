"""Rootless workspace execution sandbox: systemd scope + bubblewrap + seccomp + prlimit.

Standalone and bot-agnostic: it takes a workspace directory and a file path
already containment-checked by the caller, and runs the script under a locked
isolation profile with an explicit network mode. ``none`` creates an empty
network namespace, ``host`` re-shares the bot host's network namespace, and
``netns`` re-shares a pre-provisioned VPN namespace through a privileged helper.
All modes use fresh user/pid/ipc/uts/cgroup namespaces with
nested user-namespace creation disabled inside (--disable-userns, cutting the
largest class of unprivileged-userns kernel LPEs), a seccomp deny-list filter
(sandbox/seccomp.py, installed by bwrap right before exec) that EPERMs the
high-value kernel attack surface (io_uring, bpf, userfaultfd, perf, keyring,
ptrace, mount/namespace calls, ...), the workspace bind-mounted read-write and
*nothing else* writable, a read-only system tree, a size-capped tmpfs /tmp,
all host capabilities gone. Python mode
uses an isolated interpreter (stdlib only unless the configured dedicated venv
is mounted read-only). POSIX rlimits (address space, CPU seconds, core dumps,
file size, open files) bound each process; a transient systemd user scope bounds
the whole process *tree*: TasksMax hard-caps pids (fork bombs), MemoryMax caps
real memory across all forks, and CPUQuota caps aggregate CPU, none of which the
per-process rlimits can do safely on a shared uid (RLIMIT_NPROC counts every
process of the host user, RLIMIT_AS/RLIMIT_CPU are per process).
A wall-clock timeout plus a process-group SIGKILL backstop the CPU limit,
mirroring the subprocess discipline in coding/checks.py.

The scope needs a running systemd user manager (enable lingering for the
service user on headless hosts); sandbox_available() probes the full profile
(including that a syscall the filter denies actually comes back EPERM inside
the sandbox) and fails closed when any layer cannot start.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import uuid4

from sandbox.netns_lease import NetnsLeaseSafetyError
from sandbox.seccomp import SeccompUnavailableError, open_bpf_fd, seccomp_bpf_bytes

log = logging.getLogger(__name__)
SandboxRunMode = Literal["python", "shell", "direct"]
SandboxNetworkMode = Literal["none", "host", "netns"]

_PACKAGE_DIR_NAMES = ("dist-packages", "site-packages")
_PYTHON_PACKAGE_ROOTS = (
    Path("/usr/lib"),
    Path("/usr/lib64"),
    Path("/usr/local/lib"),
    Path("/usr/local/lib64"),
)
_INACTIVE_USER_UNIT_STATES = frozenset({"failed", "inactive", "unknown"})
_SYSTEMCTL_TIMEOUT_SECONDS = 5.0
_CORE_PATTERN_PATH = Path("/proc/sys/kernel/core_pattern")


async def _await_task_ignoring_cancellation[T](task: asyncio.Task[T]) -> T:
    """Drain an independent safety task despite repeated caller cancellation."""

    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


@dataclass(frozen=True)
class SandboxConfig:
    python_bin: str = "/usr/bin/python3"
    bwrap_bin: str = "bwrap"
    prlimit_bin: str = "prlimit"
    systemd_run_bin: str = "systemd-run"
    wall_timeout_seconds: float = 15.0
    max_cpu_seconds: int = 10
    # Per-process address-space rlimit. Heavier libraries (numpy et al.) map far
    # more virtual memory than they touch, so this is deliberately looser than
    # max_total_memory_mb, which caps what the tree actually uses.
    max_memory_mb: int = 1024
    # Cgroup caps on the transient scope: whole-tree pid count, real memory,
    # and aggregate CPU across every fork (swap is always off). CPUQuota uses
    # systemd's percentage convention: 100 = one full CPU, 200 = two CPUs.
    max_tasks: int = 64
    max_total_memory_mb: int = 512
    cpu_quota_percent: int = 100
    # Size of the sandbox's private tmpfs /tmp. Writes there are RAM, so this
    # closes the fill-memory-via-tmpfs hole the address-space rlimit misses.
    tmp_size_mb: int = 128
    max_fsize_mb: int = 50
    max_open_files: int = 256
    max_workspace_bytes: int = 100 * 1024 * 1024
    max_workspace_files: int = 2000
    max_output_bytes: int = 20_000
    # Real workspace filesystem root used only by the startup capability probe.
    # Persistent venv interpreters and direct-mode files execute below this
    # mount, so a noexec workspace must fail registration rather than individual
    # user runs later. Empty keeps standalone/unit callers filesystem-neutral.
    workspace_probe_root: str = ""
    # Extra host paths bind-mounted READ-ONLY into the sandbox (e.g. a dedicated
    # packages venv and /etc/fonts). Read-only, so a script can never modify them;
    # they must contain no secrets. Bound with --ro-bind-try (a missing path is
    # skipped, not fatal).
    extra_ro_binds: tuple[str, ...] = ()
    # ``host`` keeps the ordinary unprivileged user scope and re-shares the bot
    # host's network namespace. ``netns`` switches to a transient user service so
    # a namespace-selector-free root helper can enter a pre-provisioned VPN namespace.
    network_mode: SandboxNetworkMode = "none"
    sudo_bin: str = "sudo"
    # Root-owned helper that accepts the sandbox command, enters the VPN netns,
    # and drops back to the invoking user. The netns name is baked into the helper,
    # never passed by the bot. Empty disables the network path.
    netns_helper_bin: str = ""
    # Per-netns resolv.conf maintained by the operator; hard-bound over
    # /etc/resolv.conf so a missing file fails the launch rather than silently
    # leaving the sandbox without DNS.
    netns_resolv_conf: str = ""
    # A LAN endpoint (`host` or `host:port`, default port 80) the startup probe
    # must find UNREACHABLE end-to-end. Point it at a known-OPEN LAN service (e.g.
    # the Hindsight host and its real port) so even a closed-port leak is caught.
    # Empty skips that probe leg.
    network_probe_blocked_ip: str = ""
    # Extra --setenv pairs for the child (e.g. PLATFORMIO_* for builds); applied
    # after the base PATH setenv so an override wins last.
    extra_env: tuple[tuple[str, str], ...] = ()
    # When set, replaces python_bin for mode="python" (the per-user workspace venv
    # interpreter, resolved to an in-sandbox /work path by the caller).
    python_bin_override: str = ""
    # Directory names (e.g. .venv, .pio) whose bytes and entries are accounted
    # against separate environment limits, so a large venv/toolchain tree cannot
    # trip the document-sized quota while zero-byte files still consume a bounded
    # number of filesystem inodes. Empty leaves the limit unset.
    env_dir_names: tuple[str, ...] = ()
    max_env_bytes: int = 0
    max_env_files: int = 200_000


@dataclass(frozen=True)
class SandboxResult:
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    duration_ms: int
    quota_exceeded: bool = False
    # Environment trees are regenerable, unlike ordinary user documents. The
    # tool layer uses this bit to remove pre-existing .venv/.pio roots when one
    # of their dedicated limits is exceeded and restore service availability.
    environment_quota_exceeded: bool = False


class SandboxTeardownError(NetnsLeaseSafetyError):
    """A network sandbox unit could not be confirmed inactive."""


class SandboxUnavailableError(RuntimeError):
    """The required Linux sandbox boundary is unavailable on this host."""


def sandbox_available(config: SandboxConfig) -> bool:
    """True only if the sandbox profile can actually start on this host."""
    if sys.platform != "linux" or _running_as_root():
        return False
    if config.network_mode != "none":
        return network_sandbox_available(config)
    binaries_exist = (
        all(
            shutil.which(binary) is not None
            for binary in (config.bwrap_bin, config.prlimit_bin, config.systemd_run_bin)
        )
        and Path(config.python_bin).exists()
    )
    if (
        not binaries_exist
        or not _host_core_dump_boundary_safe()
        or not _workspace_exec_boundary_safe(config)
    ):
        return False
    return _probe_sandbox_start(config)


def _host_core_dump_boundary_safe() -> bool:
    """Require a file core pattern so hard RLIMIT_CORE=0 remains authoritative.

    Linux deliberately ignores RLIMIT_CORE for a ``core_pattern`` pipe. Refuse to
    expose the sandbox on such hosts instead of allowing crashes to invoke Apport,
    systemd-coredump, or another collector outside workspace/cgroup quotas.
    """
    if sys.platform != "linux":
        return False
    try:
        pattern = _CORE_PATTERN_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        log.debug("Sandbox unavailable: kernel.core_pattern could not be read")
        return False
    if not _core_pattern_is_safe(pattern):
        log.debug("Sandbox unavailable: kernel.core_pattern is empty or piped")
        return False
    return True


def _running_as_root() -> bool:
    """True when the process has host uid 0; code execution must stay rootless."""
    geteuid = getattr(os, "geteuid", None)
    return bool(geteuid is not None and geteuid() == 0)


def _workspace_exec_boundary_safe(config: SandboxConfig) -> bool:
    """Require the configured workspace filesystem to permit executable files."""
    if not config.workspace_probe_root:
        return True
    root = Path(config.workspace_probe_root).resolve()
    probe_path: Path | None = None
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, raw_path = tempfile.mkstemp(prefix=".sandbox-exec-probe-", dir=root)
        os.close(fd)
        probe_path = Path(raw_path)
        probe_path.chmod(0o700)
        return os.access(probe_path, os.X_OK)
    except OSError:
        log.debug("Sandbox unavailable: workspace execute probe failed", exc_info=True)
        return False
    finally:
        if probe_path is not None:
            with contextlib.suppress(OSError):
                probe_path.unlink()


def _core_pattern_is_safe(pattern: str) -> bool:
    return bool(pattern) and not pattern.startswith("|")


def build_sandbox_command(
    config: SandboxConfig,
    workspace_dir: Path,
    script_path: Path,
    *,
    seccomp_fd: int,
    mode: SandboxRunMode = "python",
    argv: Sequence[str] = (),
    unit_name: str | None = None,
    bpf_path: str | None = None,
) -> list[str]:
    """Build the full launch argv for running script_path under /work.

    Pure (no execution) so the exact isolation profile can be unit-tested.
    ``seccomp_fd`` is deliberately required: no caller can build a bwrap
    command without the filter (see ``open_bpf_fd``). Raises ValueError if
    script_path is not inside workspace_dir; that check is a final layer of
    defense in depth on top of the caller's containment.
    ``unit_name``/``bpf_path`` are required only on the network path (see
    ``_assemble_launch``).
    """
    workspace_dir = workspace_dir.resolve()
    try:
        rel = script_path.resolve().relative_to(workspace_dir)
    except ValueError as exc:
        raise ValueError("script must live inside the workspace") from exc
    in_sandbox_script = "/work/" + rel.as_posix()
    return _assemble_launch(
        config,
        workspace_dir,
        _build_run_argv(config, in_sandbox_script, mode, argv),
        seccomp_fd=seccomp_fd,
        unit_name=unit_name,
        bpf_path=bpf_path,
    )


def build_sandbox_command_argv(
    config: SandboxConfig,
    workspace_dir: Path,
    argv: Sequence[str],
    *,
    seccomp_fd: int,
    unit_name: str | None = None,
    bpf_path: str | None = None,
) -> list[str]:
    """Build the launch argv for running a fixed absolute command under /work.

    Unlike build_sandbox_command, argv[0] is an absolute host binary (e.g. the
    PlatformIO launcher on an extra_ro_binds path), not a workspace file, so
    there is no in-workspace containment check to run. The workspace is still the
    only writable mount and --chdir is /work.
    """
    if not argv:
        raise ValueError("argv must be non-empty")
    return _assemble_launch(
        config,
        workspace_dir.resolve(),
        list(argv),
        seccomp_fd=seccomp_fd,
        unit_name=unit_name,
        bpf_path=bpf_path,
    )


def _assemble_launch(
    config: SandboxConfig,
    workspace_dir: Path,
    run_argv: list[str],
    *,
    seccomp_fd: int,
    unit_name: str | None,
    bpf_path: str | None,
) -> list[str]:
    """Compose prefix + rlimits + bwrap + writable-mount + run argv.

    The default path is a user scope; the network path is a transient user
    service that enters the VPN netns via the sudo helper (see the prefix
    builders). Only the prefix differs; every bwrap isolation flag is shared.
    """
    if config.network_mode == "netns":
        if not unit_name or not bpf_path:
            raise ValueError("network launch requires unit_name and bpf_path")
        prefix = _build_systemd_run_network_prefix(
            config, unit_name=unit_name, bpf_path=bpf_path, seccomp_fd=seccomp_fd
        )
    else:
        prefix = _build_systemd_run_prefix(config, unit_name=unit_name)
    rlimits = _build_rlimit_prefix(config)
    bwrap = _build_bwrap_base(config, seccomp_fd=seccomp_fd)
    bwrap += [
        "--bind",
        str(workspace_dir),
        "/work",  # the ONLY writable mount
        "--chdir",
        "/work",
        "--",
        *run_argv,
    ]
    return prefix + rlimits + bwrap


def _build_run_argv(
    config: SandboxConfig,
    in_sandbox_script: str,
    mode: SandboxRunMode,
    argv: Sequence[str],
) -> list[str]:
    args = list(argv)
    if mode == "python":
        return [
            config.python_bin_override or config.python_bin,
            "-I",  # isolated mode: no env, no cwd on sys.path
            in_sandbox_script,
            *args,
        ]
    if mode == "shell":
        return ["/bin/sh", in_sandbox_script, *args]
    if mode == "direct":
        return [in_sandbox_script, *args]
    raise ValueError(f"unsupported sandbox run mode: {mode}")


def _build_systemd_run_prefix(config: SandboxConfig, *, unit_name: str | None = None) -> list[str]:
    """Wrap the run in a transient scope under the delegated user slice.

    Scope mode runs the command in the foreground with inherited stdio and a
    propagated exit code, so the pipes/kill discipline below is unchanged.
    """
    command = [
        config.systemd_run_bin,
        "--user",
        "--scope",
        "--quiet",
        "--collect",  # reap failed scopes (e.g. OOM kills) instead of accumulating
    ]
    if unit_name is not None:
        command.append(f"--unit={unit_name}")
    command.extend(
        [
            "-p",
            f"TasksMax={config.max_tasks}",
            "-p",
            f"MemoryMax={config.max_total_memory_mb}M",
            "-p",
            "MemorySwapMax=0",
            "-p",
            f"CPUQuota={config.cpu_quota_percent}%",
            # Manager-owned backstop: a scope must still die if the bot process is
            # hard-killed and can no longer enforce its asyncio wall timer.
            "-p",
            f"RuntimeMaxSec={int(config.wall_timeout_seconds) + 5}",
            "--",
        ]
    )
    return command


def _build_systemd_run_network_prefix(
    config: SandboxConfig,
    *,
    unit_name: str,
    bpf_path: str,
    seccomp_fd: int,
) -> list[str]:
    """Launch prefix for a networked run: transient user *service* + sudo helper.

    The bot service runs with NoNewPrivileges=yes, which is inherited by every
    descendant and makes execve() of a setuid binary (sudo) non-privileged. A
    user *scope* forks the payload inside the bot's process tree, so sudo there
    fails. A transient user *service* is instead forked by the per-user systemd
    manager, outside the bot's NNP tree, where sudo works normally.

    The seccomp program can't ride an inherited fd across the manager fork + the
    sudo boundary (sudo closes fds >= 3), so the manager opens it from a file via
    OpenFile= and hands it to the unit as fd 3 (SD_LISTEN_FDS_START). ``sudo -C
    <fd+1>`` raises sudo's closefrom bar to keep fd 3 open (the sudoers drop-in
    grants closefrom_override for exactly this command); _build_bwrap_base then
    installs it with ``--seccomp 3``. The helper enters the VPN netns and drops
    straight back to the invoking user before exec.
    """
    return [
        config.systemd_run_bin,
        "--user",
        "--pipe",  # connect our stdio to the payload (service mode has no tty)
        "--wait",  # block until the unit exits, propagating its status
        "--collect",  # reap the transient unit even on failure/OOM
        "--quiet",
        f"--unit={unit_name}",
        "-p",
        f"TasksMax={config.max_tasks}",
        "-p",
        f"MemoryMax={config.max_total_memory_mb}M",
        "-p",
        "MemorySwapMax=0",
        "-p",
        f"CPUQuota={config.cpu_quota_percent}%",
        # Manager-side wall backstop: kills the whole unit cgroup even if the bot
        # dies mid-run. A few seconds over the app-side wall timeout.
        "-p",
        f"RuntimeMaxSec={int(config.wall_timeout_seconds) + 5}",
        "-p",
        f"OpenFile={bpf_path}:seccomp:read-only",
        "--",
        config.sudo_bin,
        "-n",  # never prompt; fail closed if no NOPASSWD rule
        "-C",
        str(seccomp_fd + 1),  # keep fd 3 (the OpenFile seccomp fd) across sudo
        config.netns_helper_bin,
    ]


def _build_rlimit_prefix(config: SandboxConfig) -> list[str]:
    return [
        config.prlimit_bin,
        f"--as={config.max_memory_mb * 1024 * 1024}",
        f"--cpu={config.max_cpu_seconds}",
        # Hard-disable file core dumps; piped kernel handlers are rejected by
        # _host_core_dump_boundary_safe because Linux ignores this limit for them.
        "--core=0:0",
        f"--fsize={config.max_fsize_mb * 1024 * 1024}",
        f"--nofile={config.max_open_files}",
        "--",
    ]


def _build_bwrap_base(config: SandboxConfig, *, seccomp_fd: int) -> list[str]:
    bwrap = [
        config.bwrap_bin,
        "--unshare-all",  # user+pid+ipc+uts+cgroup+net, so no network
        # --unshare-all only *tries* the user namespace; --disable-userns needs
        # the hard form. Together they let bwrap create the one outer userns for
        # the sandbox, then bar the code inside from creating nested ones, which
        # cuts the largest class of unprivileged-userns kernel LPEs. Assert it
        # actually took so the probe fails closed on a kernel that can't enforce.
        "--unshare-user",
        "--disable-userns",
        "--assert-userns-disabled",
        # Deny-list seccomp program (sandbox/seccomp.py) read from an inherited
        # memfd; bwrap installs it right before exec, and refuses to start on a
        # malformed program, so the probe fails closed here too.
        "--seccomp",
        str(seccomp_fd),
        "--die-with-parent",  # collapse the namespace if the bot dies
        "--new-session",  # detach from any controlling tty
        "--clearenv",
        "--setenv",
        "PATH",
        "/usr/bin",
        "--setenv",
        "HOME",
        "/tmp",
        "--setenv",
        "MPLBACKEND",
        "Agg",  # headless matplotlib, if installed
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--size",
        str(config.tmp_size_mb * 1024 * 1024),
        "--tmpfs",
        "/tmp",
        "--ro-bind",
        "/usr",
        "/usr",  # interpreter + stdlib, read-only
        *_system_python_package_masks(),
        "--symlink",
        "usr/lib",
        "/lib",
        "--symlink",
        "usr/lib64",
        "/lib64",
        "--symlink",
        "usr/bin",
        "/bin",
    ]
    if config.network_mode != "none":
        # Retain the netns the sudo helper entered (bwrap's --share-net is only
        # valid alongside --unshare-all) instead of unsharing back to an empty
        # one. DNS and a CA store are the only additions needed for TLS egress.
        bwrap += [
            "--share-net",
            # Netns mode uses its provisioned resolver. Host mode deliberately
            # inherits the host resolver alongside the host network namespace.
            "--ro-bind",
            config.netns_resolv_conf if config.network_mode == "netns" else "/etc/resolv.conf",
            "/etc/resolv.conf",
            "--ro-bind-try",
            "/etc/ssl/certs",
            "/etc/ssl/certs",
        ]
    for path in config.extra_ro_binds:  # e.g. a packages venv, /etc/fonts
        bwrap += ["--ro-bind-try", path, path]
    # Extra child env (e.g. PLATFORMIO_* for builds); after the base PATH setenv
    # so an override of PATH here wins last.
    for key, value in config.extra_env:
        bwrap += ["--setenv", key, value]
    return bwrap


def _system_python_package_masks() -> list[str]:
    """Hide host-managed Python packages while leaving the stdlib mounted.

    The sandbox ro-binds /usr so the configured system interpreter and stdlib
    exist. On Debian/Fedora-style hosts that also exposes system package dirs
    under /usr/lib*/python*/{dist,site}-packages; python -I does not stop a
    script from manually adding those paths. Mask any package dirs that exist on
    the host with empty read-only tmpfs mounts so stdlib-only mode is host-state
    independent.
    """
    masks: list[str] = []
    for path in _system_python_package_dirs():
        masks.extend(["--tmpfs", path, "--remount-ro", path])
    return masks


def _system_python_package_dirs() -> tuple[str, ...]:
    paths: set[str] = set()
    for root in _PYTHON_PACKAGE_ROOTS:
        try:
            python_dirs = [p for p in root.glob("python3*") if p.is_dir()]
        except OSError:
            continue
        for python_dir in python_dirs:
            for leaf in _PACKAGE_DIR_NAMES:
                package_dir = python_dir / leaf
                if package_dir.is_dir():
                    paths.add(package_dir.as_posix())
    return tuple(sorted(paths))


def _launch_env() -> dict[str, str]:
    """Minimal env for systemd-run/prlimit/bwrap themselves.

    systemd-run --user must reach the per-user manager; a system service
    running as the sandbox user lacks the session env, so derive the runtime
    dir from the uid when unset. bwrap --clearenv wipes the child's env
    independently, so nothing here reaches the executed script.
    """
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    if os.name == "posix":
        runtime_dir = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        env["XDG_RUNTIME_DIR"] = runtime_dir
        env["DBUS_SESSION_BUS_ADDRESS"] = os.environ.get(
            "DBUS_SESSION_BUS_ADDRESS", f"unix:path={runtime_dir}/bus"
        )
    return env


# Proves the seccomp filter is live end-to-end, not merely loaded:
# personality(0xffffffff) is a query form that always succeeds unprivileged,
# so only the filter can turn it into EPERM (errno 1). Exit 0 = enforced.
_SECCOMP_PROBE_SNIPPET = (
    "import ctypes, sys\n"
    "libc = ctypes.CDLL(None, use_errno=True)\n"
    "rc = libc.personality(ctypes.c_ulong(0xFFFFFFFF))\n"
    "sys.exit(0 if (rc == -1 and ctypes.get_errno() == 1) else 7)\n"
)


def _probe_sandbox_start(
    config: SandboxConfig,
    *,
    snippet: str = _SECCOMP_PROBE_SNIPPET,
    timeout_seconds: float = 5.0,
) -> bool:
    try:
        seccomp_fd = open_bpf_fd()
    except SeccompUnavailableError, OSError:
        log.debug("Sandbox availability probe: seccomp filter unavailable", exc_info=True)
        return False
    try:
        command = (
            _build_systemd_run_prefix(config)
            + _build_rlimit_prefix(config)
            + _build_bwrap_base(config, seccomp_fd=seccomp_fd)
            + [
                "--chdir",
                "/tmp",
                "--",
                config.python_bin,
                "-I",
                "-c",
                snippet,
            ]
        )
        try:
            result = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                env=_launch_env(),
                pass_fds=(seccomp_fd,),
                timeout=timeout_seconds,
                check=False,
            )
        except OSError, subprocess.SubprocessError:
            log.debug("Sandbox availability probe could not start", exc_info=True)
            return False
    finally:
        os.close(seccomp_fd)
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        log.debug("Sandbox availability probe failed (rc=%s): %s", result.returncode, stderr)
        return False
    return True


def _runtime_bpf_dir() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    directory = Path(runtime) / "vrsandbox"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return directory


def _write_bpf_file(unit_name: str) -> Path:
    """Write the seccomp program to a 0600 file for systemd ``OpenFile=``.

    The service-mode manager fork plus the sudo boundary can't carry an inherited
    memfd, so the manager opens this file by path and hands it to the unit as fd
    3 (SD_LISTEN_FDS_START). Caller deletes it in a finally.
    """
    path = _runtime_bpf_dir() / f"{unit_name}.bpf"
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, seccomp_bpf_bytes())
    finally:
        os.close(fd)
    return path


# Network probe: the base personality/EPERM check (seccomp survived the new
# service-mode + sudo prefix) plus egress assertions: a route exists, DNS works
# through the netns resolver, the tunnel carries TLS, and a configured LAN
# endpoint is unreachable end-to-end. Exit codes are distinct so a probe failure
# is diagnosable from the log.
#
# The blocked-endpoint check is `host` or `host:port` (default port 80). Point it
# at a known-OPEN LAN service (e.g. the Hindsight host and its real port): a
# leaking netns CONNECTS (success = leak, fail the probe); an isolated one is
# stopped by routing or the nft `reject`, which surfaces as an OSError (including
# the ECONNREFUSED that ICMP port-unreachable maps to), all treated as isolated.
# A known-open target is required precisely because a closed/rejected port and a
# firewall block are indistinguishable by errno alone.
def _network_probe_snippet(blocked: str) -> str:
    blocked_check = ""
    if blocked:
        host, _, port_s = blocked.rpartition(":")
        host = host or blocked
        port = port_s if port_s.isdigit() else "80"
        blocked_check = (
            "try:\n"
            f"    socket.create_connection(({host!r}, {port}), timeout=2).close()\n"
            "    sys.exit(11)\n"  # connected: LAN reachable, netns is leaking
            "except OSError:\n"
            "    pass\n"  # blocked (no route / nft reject): isolated, good
        )
    return (
        "import ctypes, socket, ssl, sys, venv\n"
        "libc = ctypes.CDLL(None, use_errno=True)\n"
        "rc = libc.personality(ctypes.c_ulong(0xFFFFFFFF))\n"
        "if not (rc == -1 and ctypes.get_errno() == 1):\n"
        "    sys.exit(7)\n"  # seccomp not enforced
        "try:\n"
        "    venv.EnvBuilder(with_pip=True, symlinks=False).create('/tmp/venv-probe')\n"
        "except Exception:\n"
        "    sys.exit(14)\n"  # system Python cannot build the persistent package venv
        "u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)\n"
        "u.connect(('1.1.1.1', 53))\n"  # route lookup; ENETUNREACH in an empty netns
        "u.close()\n"
        "try:\n"
        "    socket.getaddrinfo('example.com', 443, type=socket.SOCK_STREAM)\n"
        "except OSError:\n"
        "    sys.exit(12)\n"  # configured netns DNS is unavailable
        "try:\n"
        "    with socket.create_connection(('example.com', 443), timeout=4) as raw:\n"
        "        with ssl.create_default_context().wrap_socket(raw, server_hostname='example.com'):\n"
        "            pass\n"
        "except OSError:\n"
        "    sys.exit(13)\n"  # DNS resolved, but real TLS egress failed
        f"{blocked_check}"
        "sys.exit(0)\n"
    )


def network_sandbox_available(config: SandboxConfig) -> bool:
    """True only if the configured network profile starts and egress works.

    Host mode uses the ordinary user scope and intentionally inherits every route
    the bot host can reach. Netns mode additionally fails closed on any missing
    helper, resolver, sudo rule, namespace, tunnel, or blocked-target assertion.
    """
    if sys.platform != "linux" or _running_as_root() or config.network_mode == "none":
        return False
    if config.network_mode == "host":
        binaries_exist = (
            all(
                shutil.which(binary) is not None
                for binary in (config.bwrap_bin, config.prlimit_bin, config.systemd_run_bin)
            )
            and Path(config.python_bin).exists()
        )
        return (
            binaries_exist
            and _host_core_dump_boundary_safe()
            and _workspace_exec_boundary_safe(config)
            and _probe_sandbox_start(
                config,
                snippet=_network_probe_snippet(""),
                timeout_seconds=20.0,
            )
        )
    binaries_exist = (
        all(
            shutil.which(binary) is not None
            for binary in (config.bwrap_bin, config.prlimit_bin, config.systemd_run_bin)
        )
        and Path(config.python_bin).exists()
        and shutil.which(config.sudo_bin) is not None
        and _trusted_root_owned_file(Path(config.netns_helper_bin), executable=True)
        and _trusted_root_owned_file(Path(config.netns_resolv_conf))
    )
    if (
        not binaries_exist
        or not _host_core_dump_boundary_safe()
        or not _workspace_exec_boundary_safe(config)
    ):
        return False
    return _probe_network_sandbox_start(config)


def _trusted_root_owned_file(path: Path, *, executable: bool = False) -> bool:
    """Require a non-symlink root-controlled file and root-controlled parent chain."""
    try:
        file_stat = path.lstat()
        if (
            not stat.S_ISREG(file_stat.st_mode)
            or file_stat.st_uid != 0
            or file_stat.st_mode & 0o022
            or (executable and not file_stat.st_mode & 0o111)
        ):
            return False
        parent = path.absolute().parent
        while True:
            parent_stat = parent.lstat()
            if (
                not stat.S_ISDIR(parent_stat.st_mode)
                or parent_stat.st_uid != 0
                or parent_stat.st_mode & 0o022
            ):
                return False
            if parent == parent.parent:
                return True
            parent = parent.parent
    except OSError:
        return False


def _probe_network_sandbox_start(config: SandboxConfig) -> bool:
    unit_name = f"sandbox-net-probe-{uuid4().hex}"
    try:
        bpf_path = _write_bpf_file(unit_name)
    except SeccompUnavailableError, OSError:
        log.debug("Network probe: seccomp filter unavailable", exc_info=True)
        return False
    probe_dir = _runtime_bpf_dir() / unit_name
    try:
        probe_dir.mkdir(mode=0o700, exist_ok=True)
        command = build_sandbox_command_argv(
            config,
            probe_dir,
            [
                config.python_bin,
                "-I",
                "-c",
                _network_probe_snippet(config.network_probe_blocked_ip),
            ],
            seccomp_fd=3,
            unit_name=unit_name,
            bpf_path=str(bpf_path),
        )
        try:
            result = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                env=_launch_env(),
                timeout=20.0,
                check=False,
            )
        except subprocess.TimeoutExpired:
            # subprocess.run kills only the systemd-run client on timeout; the
            # manager-owned service (and a blocked DNS lookup inside it) can
            # otherwise survive until RuntimeMaxSec expires.
            _stop_user_unit_sync(unit_name)
            log.debug("Network sandbox probe timed out")
            return False
        except OSError, subprocess.SubprocessError:
            log.debug("Network sandbox probe could not start", exc_info=True)
            return False
    finally:
        with contextlib.suppress(OSError):
            bpf_path.unlink()
        with contextlib.suppress(OSError):
            probe_dir.rmdir()
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        log.debug("Network sandbox probe failed (rc=%s): %s", result.returncode, stderr)
        return False
    return True


async def run_python_in_sandbox(
    config: SandboxConfig,
    workspace_dir: Path,
    script_path: Path,
    *,
    stdin: str | None = None,
    argv: Sequence[str] = (),
) -> SandboxResult:
    """Run script_path inside the sandbox and return its captured result."""
    return await run_workspace_file_in_sandbox(
        config,
        workspace_dir,
        script_path,
        stdin=stdin,
        mode="python",
        argv=argv,
    )


async def run_workspace_file_in_sandbox(
    config: SandboxConfig,
    workspace_dir: Path,
    file_path: Path,
    *,
    stdin: str | None = None,
    mode: SandboxRunMode = "direct",
    argv: Sequence[str] = (),
    unit_name: str | None = None,
) -> SandboxResult:
    """Run file_path inside the sandbox and return its captured result."""
    if sys.platform != "linux":
        raise SandboxUnavailableError("Code execution is available only on Linux")
    if _running_as_root():
        raise SandboxUnavailableError(
            "Code execution requires an unprivileged service account; refusing to run as root"
        )
    if config.network_mode == "netns":
        return await _run_networked(
            config,
            workspace_dir,
            lambda fd, unit, bpf: build_sandbox_command(
                config,
                workspace_dir,
                file_path,
                seccomp_fd=fd,
                mode=mode,
                argv=argv,
                unit_name=unit,
                bpf_path=bpf,
            ),
            stdin=stdin,
            unit_name=unit_name,
        )
    seccomp_fd = open_bpf_fd()
    try:
        command = build_sandbox_command(
            config,
            workspace_dir,
            file_path,
            seccomp_fd=seccomp_fd,
            mode=mode,
            argv=argv,
            unit_name=unit_name,
        )
        return await _run_built_command(
            config,
            workspace_dir,
            command,
            seccomp_fd=seccomp_fd,
            unit_name=unit_name,
            stdin=stdin,
        )
    finally:
        os.close(seccomp_fd)


async def run_command_in_sandbox(
    config: SandboxConfig,
    workspace_dir: Path,
    argv: Sequence[str],
    *,
    stdin: str | None = None,
) -> SandboxResult:
    """Run a fixed absolute command (argv[0] on an extra_ro_binds path) under /work.

    Used by run_build for the pinned PlatformIO invocation. Arbitrary model code
    never reaches this entry, only a fixed argv the tool layer builds.
    """
    if sys.platform != "linux":
        raise SandboxUnavailableError("Code execution is available only on Linux")
    if _running_as_root():
        raise SandboxUnavailableError(
            "Code execution requires an unprivileged service account; refusing to run as root"
        )
    if config.network_mode == "netns":
        return await _run_networked(
            config,
            workspace_dir,
            lambda fd, unit, bpf: build_sandbox_command_argv(
                config,
                workspace_dir,
                argv,
                seccomp_fd=fd,
                unit_name=unit,
                bpf_path=bpf,
            ),
            stdin=stdin,
        )
    seccomp_fd = open_bpf_fd()
    try:
        command = build_sandbox_command_argv(config, workspace_dir, argv, seccomp_fd=seccomp_fd)
        return await _run_built_command(
            config, workspace_dir, command, seccomp_fd=seccomp_fd, stdin=stdin
        )
    finally:
        os.close(seccomp_fd)


async def _run_networked(
    config: SandboxConfig,
    workspace_dir: Path,
    build: Callable[[int, str, str], list[str]],
    *,
    stdin: str | None,
    unit_name: str | None = None,
) -> SandboxResult:
    """Run the network path: seccomp via OpenFile= fd 3, transient unit, no memfd."""
    unit_name = unit_name or f"sandbox-net-{uuid4().hex}"
    try:
        bpf_path = _write_bpf_file(unit_name)
    except SeccompUnavailableError, OSError:
        log.exception("Network run: seccomp filter unavailable")
        return SandboxResult(
            exit_code=1,
            stdout="",
            stderr="Sandbox filter unavailable; cannot run.",
            timed_out=False,
            duration_ms=0,
        )
    try:
        command = build(3, unit_name, str(bpf_path))
        return await _run_built_command(
            config, workspace_dir, command, seccomp_fd=None, unit_name=unit_name, stdin=stdin
        )
    finally:
        with contextlib.suppress(OSError):
            bpf_path.unlink()


async def _run_built_command(
    config: SandboxConfig,
    workspace_dir: Path,
    command: list[str],
    *,
    seccomp_fd: int | None,
    unit_name: str | None = None,
    stdin: str | None,
) -> SandboxResult:
    started = time.monotonic()
    if not _host_core_dump_boundary_safe():
        return SandboxResult(
            exit_code=1,
            stdout="",
            stderr=("Sandbox unavailable: the host core-dump handler is not safely bounded."),
            timed_out=False,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    # Offload the workspace tree walk off the event loop, like the in-flight quota
    # monitor already does; a large workspace must not stall the loop.
    quota_reason = await asyncio.to_thread(_workspace_quota_reason, config, workspace_dir)
    if quota_reason is not None:
        return SandboxResult(
            exit_code=1,
            stdout="",
            stderr=f"Workspace quota exceeded before execution: {quota_reason}.",
            timed_out=False,
            duration_ms=int((time.monotonic() - started) * 1000),
            quota_exceeded=True,
            environment_quota_exceeded=_environment_quota_exceeded(quota_reason),
        )
    proc: asyncio.subprocess.Process | None = None
    timed_out = False
    quota_state = _QuotaState()
    out_buf, err_buf = bytearray(), bytearray()
    quota_task: asyncio.Task[None] | None = None
    try:
        spawn_task = asyncio.create_task(
            asyncio.create_subprocess_exec(
                *command,
                stdin=(
                    asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL
                ),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_launch_env(),
                start_new_session=os.name == "posix",
                # Default path inherits the seccomp memfd; the network path passes it
                # via systemd OpenFile=, so there is nothing to inherit here.
                pass_fds=() if seccomp_fd is None else (seccomp_fd,),
            )
        )
        try:
            proc = await asyncio.shield(spawn_task)
        except asyncio.CancelledError as cancellation:
            # A cancelled subprocess await does not prove systemd-run failed to
            # launch its manager-owned unit. Obtain the client handle before the
            # finally block stops the unit and releases the shared netns lease.
            proc = await _await_task_ignoring_cancellation(spawn_task)
            raise cancellation
        quota_task = asyncio.create_task(
            _monitor_workspace_quota(proc, workspace_dir, config, quota_state, unit_name)
        )
        # Drain stdout/stderr with our OWN bounded readers rather than
        # communicate(): a flood of output can never balloon the bot's memory
        # past the cap (the child blocks on a full pipe once we stop reading),
        # and on a wall-timeout we cancel cleanly instead of leaving a
        # half-drained transport that makes proc.wait() hang forever.
        tasks = [
            asyncio.create_task(_drain_capped(proc.stdout, out_buf, config.max_output_bytes)),
            asyncio.create_task(_drain_capped(proc.stderr, err_buf, config.max_output_bytes)),
        ]
        if stdin is not None:
            tasks.append(asyncio.create_task(_feed_stdin(proc, stdin.encode("utf-8"))))
        try:
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=config.wall_timeout_seconds)
        except TimeoutError:
            timed_out = True
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        if not timed_out:
            # Streams hit EOF; reap the child (bounded) to capture its exit code.
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except TimeoutError:
                timed_out = True
    finally:
        cleanup_task = asyncio.create_task(_finish_run_cleanup(quota_task, proc, unit_name))
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError as cancellation:
            # Do not let cancellation return the shared namespace lease before
            # its manager-owned unit has reached a confirmed terminal state.
            await _await_task_ignoring_cancellation(cleanup_task)
            raise cancellation
    duration_ms = int((time.monotonic() - started) * 1000)
    stdout = _cap(out_buf.decode("utf-8", errors="replace"), config.max_output_bytes)
    stderr = _cap(err_buf.decode("utf-8", errors="replace"), config.max_output_bytes)
    if not quota_state.reason:
        post_reason = await asyncio.to_thread(_workspace_quota_reason, config, workspace_dir)
        quota_state.reason = post_reason or ""
    if timed_out:
        note = f"Execution timed out after {config.wall_timeout_seconds:.0f}s and was killed."
        stderr = f"{stderr}\n{note}".strip()
    if quota_state.reason:
        note = f"Execution stopped because the workspace quota was exceeded: {quota_state.reason}."
        stderr = f"{stderr}\n{note}".strip()
    exit_code = None if timed_out else (proc.returncode if proc is not None else None)
    if quota_state.reason and exit_code == 0:
        exit_code = 1
    return SandboxResult(
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
        duration_ms=duration_ms,
        quota_exceeded=bool(quota_state.reason),
        environment_quota_exceeded=_environment_quota_exceeded(quota_state.reason),
    )


@dataclass
class _QuotaState:
    reason: str = ""


@dataclass(frozen=True)
class _WorkspaceUsage:
    bytes: int
    files: int
    env_bytes: int = 0
    env_files: int = 0
    scan_error: Literal["workspace", "environment"] | None = None


def _workspace_quota_reason(config: SandboxConfig, workspace_dir: Path) -> str | None:
    usage = _workspace_usage(config, workspace_dir)
    if usage.scan_error == "environment":
        return "environment tree could not be inspected safely"
    if usage.scan_error == "workspace":
        return "workspace tree could not be inspected safely"
    if usage.bytes > config.max_workspace_bytes:
        return f"{usage.bytes} bytes exceeds {config.max_workspace_bytes} bytes"
    if usage.files > config.max_workspace_files:
        return f"{usage.files} files exceeds {config.max_workspace_files} files"
    if config.max_env_bytes and usage.env_bytes > config.max_env_bytes:
        return f"environment uses {usage.env_bytes} bytes; limit is {config.max_env_bytes} bytes"
    if config.max_env_files and usage.env_files > config.max_env_files:
        return f"environment has {usage.env_files} entries; limit is {config.max_env_files} entries"
    return None


def _environment_quota_exceeded(reason: str | None) -> bool:
    return bool(reason and reason.startswith("environment "))


def _in_env_dir(config: SandboxConfig, rel_parts: tuple[str, ...]) -> bool:
    return bool(config.env_dir_names) and any(part in config.env_dir_names for part in rel_parts)


def _workspace_usage(config: SandboxConfig, workspace_dir: Path) -> _WorkspaceUsage:
    """Split environment-tree bytes/entries from ordinary workspace usage.

    A per-user venv or toolchain tree can be hundreds of MB and contain far more
    entries than a document workspace. Separate ceilings preserve that use case
    without exempting zero-byte files, directories, links, or special entries from
    inode accounting. The walk stops as soon as any configured ceiling is crossed,
    so a previously abusive tree cannot force every monitor pass to enumerate it
    in full.
    """
    total_bytes = 0
    env_bytes = 0
    total_files = 0
    env_files = 0
    scan_error: Literal["workspace", "environment"] | None = None
    pending: list[tuple[Path, bool]] = [(workspace_dir, False)]
    while pending and scan_error is None:
        directory, directory_is_env = pending.pop()
        try:
            entries = os.scandir(directory)
        except OSError:
            scan_error = "environment" if directory_is_env else "workspace"
            break
        with entries:
            for entry in entries:
                path = Path(entry.path)
                try:
                    path_stat = entry.stat(follow_symlinks=False)
                    rel_parts = path.relative_to(workspace_dir).parts
                except OSError:
                    scan_error = (
                        "environment"
                        if directory_is_env or entry.name in config.env_dir_names
                        else "workspace"
                    )
                    break
                in_env = directory_is_env or _in_env_dir(config, rel_parts)
                if in_env:
                    env_files += 1
                    env_bytes += path_stat.st_size
                else:
                    total_files += 1
                    total_bytes += path_stat.st_size
                if (
                    total_bytes > config.max_workspace_bytes
                    or total_files > config.max_workspace_files
                    or (config.max_env_bytes and env_bytes > config.max_env_bytes)
                    or (config.max_env_files and env_files > config.max_env_files)
                ):
                    pending.clear()
                    break
                if stat.S_ISDIR(path_stat.st_mode):
                    pending.append((path, in_env))
    return _WorkspaceUsage(
        bytes=total_bytes,
        files=total_files,
        env_bytes=env_bytes,
        env_files=env_files,
        scan_error=scan_error,
    )


async def _monitor_workspace_quota(
    proc: asyncio.subprocess.Process,
    workspace_dir: Path,
    config: SandboxConfig,
    state: _QuotaState,
    unit_name: str | None = None,
) -> None:
    while proc.returncode is None:
        reason = await asyncio.to_thread(_workspace_quota_reason, config, workspace_dir)
        if reason is not None:
            state.reason = reason
            await _kill_process_group(proc, unit_name)
            return
        await asyncio.sleep(0.05)


async def _drain_capped(
    stream: asyncio.StreamReader | None,
    buf: bytearray,
    cap: int,
) -> None:
    """Drain a stream fully, but keep only the first ``cap`` bytes in ``buf``.

    Reading to EOF (rather than stopping at the cap) is what lets a script that
    prints more than the cap still finish writing and exit, so ``proc.wait()``
    can reap it. Leaving bytes unread in the pipe would block the child on a
    full pipe and make the wait hang. Memory stays bounded because past the cap
    each chunk is read and discarded instead of buffered.
    """
    if stream is None:
        return
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            return
        if len(buf) <= cap:
            buf.extend(chunk)


async def _feed_stdin(proc: asyncio.subprocess.Process, data: bytes) -> None:
    if proc.stdin is None:
        return
    try:
        if data:
            proc.stdin.write(data)
            await proc.stdin.drain()
    except BrokenPipeError, ConnectionResetError, OSError:
        pass
    finally:
        with contextlib.suppress(Exception):
            proc.stdin.close()


async def _cleanup_process_group(
    proc: asyncio.subprocess.Process | None, unit_name: str | None = None
) -> None:
    teardown_error: SandboxTeardownError | None = None
    if proc is not None:
        try:
            await _kill_process_group(proc, unit_name)
        except SandboxTeardownError as exc:
            teardown_error = exc
        except Exception:
            if unit_name is not None:
                log.warning(
                    "Sandbox unit %s teardown failed unexpectedly",
                    unit_name,
                    exc_info=True,
                )
                teardown_error = SandboxTeardownError(
                    f"Network sandbox unit {unit_name} teardown could not be verified."
                )
            else:
                log.debug("Sandbox process cleanup failed", exc_info=True)
        # The plain path's process-group helper does not wait. Networked cleanup
        # already reaps the launcher before its final unit stop/check.
        if proc.returncode is None and unit_name is None:
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except TimeoutError:
                log.warning("Sandbox process did not exit within 5s of SIGKILL; abandoning")
    elif unit_name is not None:
        # Spawn failure can leave no client handle, but the unique unit may still
        # have been submitted. Stop and confirm it before the lease is released.
        try:
            await _stop_user_unit(unit_name)
        except SandboxTeardownError as exc:
            teardown_error = exc
        except Exception as exc:
            teardown_error = SandboxTeardownError(
                f"Network sandbox unit {unit_name} teardown could not be verified."
            )
            teardown_error.__cause__ = exc
    if teardown_error is not None:
        raise teardown_error


async def _finish_run_cleanup(
    quota_task: asyncio.Task[None] | None,
    proc: asyncio.subprocess.Process | None,
    unit_name: str | None,
) -> None:
    if quota_task is not None:
        quota_task.cancel()
        await asyncio.gather(quota_task, return_exceptions=True)
    await _cleanup_process_group(proc, unit_name)


async def _kill_process_group(
    proc: asyncio.subprocess.Process, unit_name: str | None = None
) -> None:
    # For networked runs, disable and reap systemd-run before the final stop and
    # is-active check. Before submission, querying first could report "unknown"
    # and then let the still-live client create the unit after cleanup returned.
    launcher_issue = ""
    teardown_error: SandboxTeardownError | None = None
    if proc.returncode is None:
        try:
            if os.name == "posix" and proc.pid is not None:
                # SIGKILL the whole session; --die-with-parent then collapses the
                # pid namespace, reaping any process the script forked.
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
        except ProcessLookupError, OSError:
            pass
    if unit_name is not None and proc.returncode is None:
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except TimeoutError:
            launcher_issue = "systemd-run launcher did not exit after SIGKILL"
        except Exception as exc:
            launcher_issue = f"systemd-run launcher reap failed: {type(exc).__name__}"
    if unit_name is not None:
        try:
            await _stop_user_unit(unit_name)
        except SandboxTeardownError as exc:
            teardown_error = exc
        except Exception:
            log.warning(
                "Sandbox unit %s teardown failed unexpectedly",
                unit_name,
                exc_info=True,
            )
            teardown_error = SandboxTeardownError(
                f"Network sandbox unit {unit_name} teardown could not be verified."
            )
    if launcher_issue:
        raise SandboxTeardownError(
            f"Network sandbox unit {unit_name} could not be stopped safely ({launcher_issue})."
        ) from teardown_error
    if teardown_error is not None:
        raise teardown_error


async def _stop_user_unit(unit_name: str) -> None:
    """Stop a transient network unit and require an explicitly inactive state."""

    task = asyncio.create_task(_stop_user_unit_confirmed(unit_name))
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError as cancellation:
        # The manager-owned service must not outlive cleanup merely because the
        # caller was cancelled; delay cancellation until its state is known.
        await _await_task_ignoring_cancellation(task)
        raise cancellation


async def _stop_user_unit_confirmed(unit_name: str) -> None:
    stop_issue = ""
    unit_ref = _systemd_unit_ref(unit_name)
    try:
        stop = await asyncio.create_subprocess_exec(
            "systemctl",
            "--user",
            "stop",
            unit_ref,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=_launch_env(),
        )
        try:
            return_code = await asyncio.wait_for(stop.wait(), timeout=_SYSTEMCTL_TIMEOUT_SECONDS)
            if return_code != 0:
                stop_issue = f"stop exited with status {return_code}"
        except TimeoutError:
            stop_issue = "stop timed out"
            with contextlib.suppress(ProcessLookupError):
                stop.kill()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(stop.wait(), timeout=1.0)
        except Exception as exc:
            stop_issue = f"stop failed: {type(exc).__name__}"
            with contextlib.suppress(ProcessLookupError):
                stop.kill()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(stop.wait(), timeout=1.0)
    except Exception as exc:
        stop_issue = f"stop could not start: {type(exc).__name__}"

    state = await _wait_for_user_unit_inactive(unit_name)
    if state in _INACTIVE_USER_UNIT_STATES:
        return
    detail = f"state={state}" if state is not None else "state unavailable"
    if stop_issue:
        detail = f"{stop_issue}; {detail}"
    raise SandboxTeardownError(
        f"Network sandbox unit {unit_name} could not be stopped safely ({detail})."
    )


async def _user_unit_state(unit_name: str) -> str | None:
    unit_ref = _systemd_unit_ref(unit_name)
    try:
        status = await asyncio.create_subprocess_exec(
            "systemctl",
            "--user",
            "is-active",
            unit_ref,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=_launch_env(),
        )
    except Exception:
        log.warning("Could not query sandbox unit %s", unit_name, exc_info=True)
        return None
    try:
        stdout, _ = await asyncio.wait_for(status.communicate(), timeout=_SYSTEMCTL_TIMEOUT_SECONDS)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            status.kill()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(status.wait(), timeout=1.0)
        return None
    except Exception:
        log.warning("Querying sandbox unit %s failed", unit_name, exc_info=True)
        with contextlib.suppress(ProcessLookupError):
            status.kill()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(status.wait(), timeout=1.0)
        return None
    try:
        state = stdout.decode("utf-8", errors="replace").strip().lower()
    except Exception:
        log.warning("Sandbox unit %s returned an invalid state", unit_name)
        return None
    return state or None


async def _wait_for_user_unit_inactive(unit_name: str) -> str | None:
    deadline = asyncio.get_running_loop().time() + _SYSTEMCTL_TIMEOUT_SECONDS
    state: str | None = None
    while True:
        state = await _user_unit_state(unit_name)
        if state in _INACTIVE_USER_UNIT_STATES:
            return state
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return state
        await asyncio.sleep(min(0.1, remaining))


def _stop_user_unit_sync(unit_name: str) -> None:
    """Best-effort blocking teardown for the synchronous startup probe."""
    try:
        subprocess.run(
            ["systemctl", "--user", "stop", _systemd_unit_ref(unit_name)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=_launch_env(),
            timeout=5.0,
            check=False,
        )
    except OSError, subprocess.SubprocessError:
        log.debug("Failed to stop sandbox unit %s", unit_name, exc_info=True)


def _systemd_unit_ref(unit_name: str) -> str:
    if unit_name.endswith((".service", ".scope")):
        return unit_name
    return f"{unit_name}.service"


async def stop_sandbox_unit(unit_name: str) -> None:
    """Stop a persisted managed-job unit and confirm it is inactive."""

    await _stop_user_unit(unit_name)


def _cap(text: str, max_bytes: int) -> str:
    if len(text) <= max_bytes:
        return text
    return text[:max_bytes] + f"\n[TRUNCATED {len(text) - max_bytes} chars]"
