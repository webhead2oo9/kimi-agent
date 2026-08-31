"""Report why the code-execution sandbox is or is not available on this host.

``sandbox_available()`` deliberately collapses every prerequisite into one
boolean so the tool layer fails closed. That is the right shape for the
runtime and the wrong shape for a person setting up a host or a CI runner,
who needs to know *which* prerequisite is missing. This script builds the
same ``SandboxConfig`` startup builds - ``.env``/``ENV_FILE``, an optional
``RUNTIME_ENV`` overlay (systemd ``EnvironmentFile`` semantics, as
``scripts/preflight`` reads it), and the operator ``settings.md`` overlay -
runs the gate's checks in the gate's order, prints each result as soon as it
is known, and then runs the real start probe with debug logging so its
stderr is visible.

Run from ``bot/`` as ``python -m scripts.sandbox_probe``, as the bot user,
with the same ``ENV_FILE``/``RUNTIME_ENV`` the service uses. Exit status 0
means a jailed process actually started with the configured profile and the
executable-skill sandbox probe passed at the configured process ceiling;
whether ``run_code`` registers additionally requires ``CODE_EXEC_ENABLED``,
which the profile header line reports.
"""

from __future__ import annotations

from collections.abc import Callable
import logging
import os
from pathlib import Path
import shutil
import stat as stat_module
import subprocess
import sys

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


def _merge_runtime_env() -> str | None:
    """Overlay RUNTIME_ENV exactly the way scripts/preflight does.

    The systemd unit reads a second EnvironmentFile the plain dotenv path never
    sees; without this merge the probe would certify a different profile than
    the service runs. Must happen before config.settings is imported, because
    that module constructs Settings at import time.
    """

    raw = os.environ.get("RUNTIME_ENV")
    if not raw:
        return None
    runtime_env = Path(raw)
    if not runtime_env.is_file():
        return f"RUNTIME_ENV={runtime_env} does not exist; probing without it"
    from dotenv import dotenv_values

    values = dotenv_values(runtime_env, interpolate=False)
    malformed = sorted(key for key, value in values.items() if value is None)
    if malformed:
        raise SystemExit(f"invalid assignment(s) in {runtime_env}: {', '.join(malformed)}")
    os.environ.update({key: value for key, value in values.items() if value is not None})
    return f"merged RUNTIME_ENV={runtime_env}"


def _report(name: str, check: Callable[[], tuple[bool, str]]) -> bool:
    """Print one row as soon as it is known, so a crash cannot eat the report."""

    try:
        ok, detail = check()
    except Exception as exc:
        # A diagnostic must survive its subject; the repr is the finding.
        ok, detail = False, f"check raised {exc!r}"
    print(f"{'ok  ' if ok else 'FAIL'} {name.ljust(_NAME_WIDTH)}  {detail}")
    return ok


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
    runtime_env_note = _merge_runtime_env()

    from pydantic import ValidationError

    try:
        from config.settings import Settings

        settings = Settings()
    except ValidationError as exc:
        print(f"FAIL settings: the environment does not validate\n{exc}")
        return 2

    # Startup layers the operator settings.md over the environment before any
    # config is captured (app/runtime.py:build_app); the probe must certify the
    # same layered profile.
    from config.operator_settings import apply_operator_settings

    # The probe mirrors the gate on purpose, so it reads the gate's own helpers
    # rather than re-deriving them and drifting.
    from app.tools import build_sandbox_config
    from sandbox import runner, seccomp
    from sandbox.runner import sandbox_available
    from skills.registration import build_script_sandbox_limits
    from skills.sandbox import SandboxUnavailableError, validate_sandbox_runtime

    overlaid = apply_operator_settings(settings)
    config = build_sandbox_config(settings)
    skill_limits = build_script_sandbox_limits(settings)
    euid = os.geteuid() if hasattr(os, "geteuid") else None
    launch_env = runner._launch_env()

    def which(binary: str) -> Callable[[], tuple[bool, str]]:
        def check() -> tuple[bool, str]:
            found = shutil.which(binary)
            return found is not None, found or "not on PATH"

        return check

    def trusted_file(path: str, *, executable: bool) -> Callable[[], tuple[bool, str]]:
        def check() -> tuple[bool, str]:
            ok = runner._trusted_root_owned_file(Path(path), executable=executable)
            return ok, path if ok else f"{path} is not a root-owned regular file"

        return check

    def workspace_row() -> tuple[bool, str]:
        # Deliberately non-creating: startup makes this directory as the
        # service user, and a diagnostic run from the wrong account must not
        # plant a 0700 root it cannot use.
        root = Path(config.workspace_probe_root)
        if not root.is_dir():
            return False, f"{root} does not exist; start the bot once or create it as its user"
        st = root.stat()
        detail = f"{root} uid={st.st_uid} mode={stat_module.filemode(st.st_mode)}"
        return runner._workspace_exec_boundary_safe(config), detail

    def user_manager_row() -> tuple[bool, str]:
        """Start one trivial transient scope with the properties offline runs pass.

        Using the real prefix means a user manager without cgroup delegation
        for pids/memory/cpu fails here, by name. A netns launch additionally
        uses a sudo-entered transient service this row does not cover; that
        path is exercised by the final gate row.
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

    def libseccomp_row() -> tuple[bool, str]:
        try:
            program = seccomp.seccomp_bpf_bytes()
        except seccomp.SeccompUnavailableError as exc:
            return False, str(exc)
        return True, f"{len(program) // 8} filter instructions"

    def core_pattern() -> str:
        try:
            return runner._CORE_PATTERN_PATH.read_text(encoding="utf-8").strip() or "<empty>"
        except OSError as exc:
            return f"<unreadable: {exc}>"

    if runtime_env_note:
        print(f"     {runtime_env_note}")
    if overlaid:
        print(f"     settings.md overlay applied: {', '.join(sorted(overlaid))}")
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
        lambda: (runner._host_core_dump_boundary_safe(), core_pattern()),
    )
    _report("workspace allows execution", workspace_row)
    _report("libseccomp", libseccomp_row)
    _report("user systemd manager", user_manager_row)
    if config.network_mode == "netns":
        _report(config.sudo_bin, which(config.sudo_bin))
        _report(
            "netns helper is trusted",
            trusted_file(config.netns_helper_bin, executable=True),
        )
        _report(
            "netns resolv.conf is trusted",
            trusted_file(config.netns_resolv_conf, executable=False),
        )
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

    def gate() -> tuple[bool, str]:
        return sandbox_available(config), f"{config.network_mode} profile start probe"

    available = _report("sandbox_available()", gate)

    def skills_probe() -> tuple[bool, str]:
        try:
            validate_sandbox_runtime(skill_limits)
        except SandboxUnavailableError as exc:
            return False, str(exc)
        return True, f"jailed true exited 0 at nproc={skill_limits.processes}"

    skills_ok = _report("executable-skill sandbox probe", skills_probe)
    return 0 if available and skills_ok else 1


if __name__ == "__main__":
    sys.exit(main())
