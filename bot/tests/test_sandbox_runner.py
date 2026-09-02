"""Exercises sandbox/runner.py directly: the isolation command it builds
(systemd-run flags, network namespace, seccomp) rather than any tool that
calls it. Kept separate from test_code_exec_tool.py so sandbox flags can be
verified without a full tool-registry dispatch.
"""

from __future__ import annotations

import asyncio
import dataclasses
import errno
import json
import os
import signal
import stat
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

import sandbox.runner as runner
import sandbox.seccomp as seccomp
from sandbox.netns_lease import NetnsLease, NetnsLeasePoisonedError
from sandbox.runner import (
    SandboxConfig,
    SandboxTeardownError,
    SandboxUnavailableError,
    build_sandbox_command,
    build_sandbox_command_argv,
    network_sandbox_available,
    run_python_in_sandbox,
    run_workspace_file_in_sandbox,
    sandbox_available,
)
from tests.sandbox_gate import sandbox_skip_allowed, sandbox_unavailable


def _net_config(**overrides) -> SandboxConfig:
    base = SandboxConfig(
        network_mode="netns",
        netns_helper_bin="/usr/local/sbin/code-exec-netns",
        sudo_bin="sudo",
        netns_resolv_conf="/etc/netns/code-exec/resolv.conf",
    )
    return dataclasses.replace(base, **overrides)


_REAL = sandbox_available(SandboxConfig())
_requires_sandbox = pytest.mark.skipif(
    sandbox_skip_allowed(not _REAL),
    reason="bwrap/prlimit/python not available on this host",
)

# Dummy fd number for the pure command-construction tests (never executed).
_FD = 7


def _requires_libseccomp() -> None:
    try:
        seccomp.seccomp_bpf_bytes()
    except seccomp.SeccompUnavailableError:
        sandbox_unavailable("libseccomp not available on this host")


@pytest.fixture(autouse=True)
def _isolate_host_core_dump_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unit tests exercise their own boundary; host policy must not short-circuit them."""
    monkeypatch.setattr(runner, "_host_core_dump_boundary_safe", lambda: True)


# --- command construction (pure, no execution) ----------------------------


def test_build_command_contains_isolation_flags(tmp_path: Path) -> None:
    ws = tmp_path / "work"
    ws.mkdir()
    script = ws / "run.py"
    script.write_text("print('x')", encoding="utf-8")
    cmd = build_sandbox_command(SandboxConfig(), ws, script, seccomp_fd=_FD)

    # No network, fresh namespaces, host root read-only, workspace the only bind.
    assert "--unshare-all" in cmd
    # Nested user namespaces are disabled inside and the assertion fails closed.
    assert "--unshare-user" in cmd
    assert "--disable-userns" in cmd
    assert "--assert-userns-disabled" in cmd
    assert "--die-with-parent" in cmd
    assert "--clearenv" in cmd
    assert cmd.count("--ro-bind") >= 1
    # The workspace is bound to /work and is the chdir.
    assert "--bind" in cmd and str(ws.resolve()) in cmd and "/work" in cmd
    assert cmd[cmd.index("--chdir") + 1] == "/work"
    # The seccomp deny-list program rides the passed fd into bwrap.
    assert cmd[cmd.index("--seccomp") + 1] == str(_FD)
    # Interpreter runs in isolated mode on the in-sandbox script path.
    assert "-I" in cmd
    assert cmd[-1] == "/work/run.py"


def test_default_config_builds_scope_mode_chain(tmp_path: Path) -> None:
    # A config with no netns_helper builds the scope-mode chain without
    # sudo or --share-net.
    ws = tmp_path / "work"
    ws.mkdir()
    (ws / "r.py").write_text("", encoding="utf-8")
    cmd = build_sandbox_command(SandboxConfig(), ws, ws / "r.py", seccomp_fd=_FD)
    assert "sudo" not in cmd
    assert "--share-net" not in cmd
    assert "--pipe" not in cmd
    assert cmd[1:3] == ["--user", "--scope"]


def test_host_network_command_keeps_scope_and_shares_host_network(tmp_path: Path) -> None:
    ws = tmp_path / "work"
    ws.mkdir()
    script = ws / "r.py"
    script.write_text("", encoding="utf-8")

    cmd = build_sandbox_command(
        SandboxConfig(network_mode="host"),
        ws,
        script,
        seccomp_fd=_FD,
    )

    assert cmd[1:3] == ["--user", "--scope"]
    assert "--share-net" in cmd
    assert "/etc/resolv.conf" in cmd
    assert "sudo" not in cmd
    assert "--pipe" not in cmd
    assert "--seccomp" in cmd
    assert any(value.startswith("TasksMax=") for value in cmd)
    assert any(value.startswith("MemoryMax=") for value in cmd)


def test_network_command_uses_service_mode_and_enters_netns(tmp_path: Path) -> None:
    ws = tmp_path / "work"
    ws.mkdir()
    (ws / "r.py").write_text("", encoding="utf-8")
    cmd = build_sandbox_command(
        _net_config(),
        ws,
        ws / "r.py",
        seccomp_fd=3,
        mode="python",
        unit_name="sandbox-net-abc",
        bpf_path="/run/user/1000/vrsandbox/sandbox-net-abc.bpf",
    )
    # Transient user *service* (not scope), so the manager forks outside NNP.
    assert cmd[1] == "--user"
    assert "--pipe" in cmd and "--wait" in cmd
    assert "--unit=sandbox-net-abc" in cmd
    assert "CPUQuota=100%" in cmd
    assert "OpenFile=/run/user/1000/vrsandbox/sandbox-net-abc.bpf:seccomp:read-only" in cmd
    # sudo helper with -C (fd+1) preserving the OpenFile seccomp fd 3, then netns.
    sudo_i = cmd.index("sudo")
    assert cmd[sudo_i + 1] == "-n"
    assert cmd[sudo_i + 2] == "-C" and cmd[sudo_i + 3] == "4"
    assert cmd[sudo_i + 4] == "/usr/local/sbin/code-exec-netns"
    # bwrap retains the entered netns and still keeps every isolation flag.
    assert "--share-net" in cmd
    assert "--unshare-all" in cmd and "--disable-userns" in cmd
    assert cmd[cmd.index("--seccomp") + 1] == "3"
    # DNS hard-bound; ordering sudo < systemd-run payload < bwrap.
    assert "/etc/netns/code-exec/resolv.conf" in cmd
    assert cmd.index("sudo") < cmd.index("--share-net")


def test_network_command_applies_extra_env_and_venv_override(tmp_path: Path) -> None:
    ws = tmp_path / "work"
    ws.mkdir()
    (ws / "r.py").write_text("", encoding="utf-8")
    cfg = _net_config(
        extra_env=(("PLATFORMIO_CORE_DIR", "/work/.pio-core"),),
        python_bin_override="/work/.venv/bin/python3",
    )
    cmd = build_sandbox_command(
        cfg,
        ws,
        ws / "r.py",
        seccomp_fd=3,
        mode="python",
        unit_name="u",
        bpf_path="/x.bpf",
    )
    joined = " ".join(cmd)
    assert "--setenv PLATFORMIO_CORE_DIR /work/.pio-core" in joined
    # The venv interpreter, not the system python, runs the script.
    assert "/work/.venv/bin/python3" in cmd
    assert cmd[-1] == "/work/r.py"


def test_build_command_argv_runs_fixed_command(tmp_path: Path) -> None:
    ws = tmp_path / "work"
    ws.mkdir()
    cmd = build_sandbox_command_argv(SandboxConfig(), ws, ["/usr/bin/env", "true"], seccomp_fd=_FD)
    assert cmd[-2:] == ["/usr/bin/env", "true"]
    assert cmd[cmd.index("--chdir") + 1] == "/work"


@pytest.mark.asyncio
async def test_run_python_forwards_argv_to_workspace_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = tmp_path / "work"
    ws.mkdir()
    script = ws / "run.py"
    script.write_text("", encoding="utf-8")
    seen: dict[str, object] = {}

    async def fake_run(config, workspace_dir, file_path, *, stdin=None, mode="direct", argv=()):
        del config, workspace_dir, file_path, stdin
        seen["mode"] = mode
        seen["argv"] = tuple(argv)
        return runner.SandboxResult(0, "", "", False, 1)

    monkeypatch.setattr(runner, "run_workspace_file_in_sandbox", fake_run)

    await runner.run_python_in_sandbox(SandboxConfig(), ws, script, argv=("one", "two"))

    assert seen == {"mode": "python", "argv": ("one", "two")}


def test_network_sandbox_available_false_when_helper_missing(tmp_path: Path) -> None:
    # No helper on disk -> fail closed before any launch attempt.
    cfg = _net_config(netns_helper_bin=str(tmp_path / "nope"))
    assert network_sandbox_available(cfg) is False


def test_root_owned_file_check_covers_privileged_trust_branches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    security_file = (tmp_path / "trusted" / "helper").absolute()

    def fake_stat(mode: int, uid: int = 0) -> os.stat_result:
        return os.stat_result((mode, 1, 1, 1, uid, 0, 0, 0, 0, 0))

    stats = {security_file: fake_stat(stat.S_IFREG | 0o755)}
    parent = security_file.parent
    while True:
        stats[parent] = fake_stat(stat.S_IFDIR | 0o755)
        if parent == parent.parent:
            break
        parent = parent.parent

    def fake_lstat(path: Path) -> os.stat_result:
        return stats[path]

    monkeypatch.setattr(Path, "lstat", fake_lstat)

    assert runner._trusted_root_owned_file(security_file, executable=True) is True

    stats[security_file] = fake_stat(stat.S_IFREG | 0o775)
    assert runner._trusted_root_owned_file(security_file, executable=True) is False

    stats[security_file] = fake_stat(stat.S_IFLNK | 0o777)
    assert runner._trusted_root_owned_file(security_file, executable=True) is False

    stats[security_file] = fake_stat(stat.S_IFREG | 0o644)
    assert runner._trusted_root_owned_file(security_file, executable=True) is False

    stats[security_file] = fake_stat(stat.S_IFREG | 0o755)
    stats[security_file.parent] = fake_stat(stat.S_IFDIR | 0o777)
    assert runner._trusted_root_owned_file(security_file, executable=True) is False


@pytest.mark.asyncio
async def test_public_runner_rejects_non_linux_posix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "work"
    workspace.mkdir()
    script = workspace / "run.py"
    script.write_text("print('no')", encoding="utf-8")
    monkeypatch.setattr(runner.sys, "platform", "darwin")

    with pytest.raises(runner.SandboxUnavailableError, match="only on Linux"):
        await runner.run_workspace_file_in_sandbox(SandboxConfig(), workspace, script)


@pytest.mark.asyncio
async def test_public_runner_rejects_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "work"
    workspace.mkdir()
    script = workspace / "run.py"
    script.write_text("print('no')", encoding="utf-8")
    monkeypatch.setattr(runner.sys, "platform", "linux")
    monkeypatch.setattr(runner, "_running_as_root", lambda: True)

    with pytest.raises(SandboxUnavailableError, match="refusing to run as root"):
        await runner.run_workspace_file_in_sandbox(SandboxConfig(), workspace, script)


def test_sandbox_availability_rejects_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner.sys, "platform", "linux")
    monkeypatch.setattr(runner, "_running_as_root", lambda: True)
    monkeypatch.setattr(
        runner,
        "_probe_sandbox_start",
        lambda _config: pytest.fail("root must be rejected before the launch probe"),
    )

    assert sandbox_available(SandboxConfig()) is False
    assert network_sandbox_available(SandboxConfig(network_mode="host")) is False


def test_workspace_execute_probe_rejects_noexec_mount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner.os, "access", lambda _path, _mode: False)

    assert (
        runner._workspace_exec_boundary_safe(
            SandboxConfig(workspace_probe_root=str(tmp_path / "workspaces"))
        )
        is False
    )
    assert list((tmp_path / "workspaces").iterdir()) == []


def test_workspace_execute_probe_accepts_and_cleans_executable_file(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspaces"

    assert (
        runner._workspace_exec_boundary_safe(
            SandboxConfig(workspace_probe_root=str(workspace_root))
        )
        is True
    )
    assert list(workspace_root.iterdir()) == []


def test_netns_helper_template_rejects_root_sudo_caller() -> None:
    helper = (
        Path(__file__).parents[1] / "deploy" / "code-exec-netns" / "code-exec-netns-helper.template"
    ).read_text(encoding="utf-8")

    assert '[ "$SUDO_UID" -ne 0 ]' in helper
    assert '[ "$SUDO_GID" -ne 0 ]' in helper
    assert "refusing a root sudo caller" in helper


def test_network_sandbox_available_false_without_egress_flag() -> None:
    assert network_sandbox_available(SandboxConfig()) is False


def test_host_network_availability_uses_scope_egress_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    monkeypatch.setattr(runner.sys, "platform", "linux")
    monkeypatch.setattr(runner, "_running_as_root", lambda: False)
    monkeypatch.setattr(runner.shutil, "which", lambda _binary: "/usr/bin/mock")

    def fake_probe(
        config: SandboxConfig,
        *,
        snippet: str,
        timeout_seconds: float,
    ) -> bool:
        seen.update(config=config, snippet=snippet, timeout_seconds=timeout_seconds)
        return True

    monkeypatch.setattr(runner, "_probe_sandbox_start", fake_probe)

    config = SandboxConfig(network_mode="host", python_bin=sys.executable)
    assert network_sandbox_available(config) is True
    assert seen["config"] is config
    assert "socket.getaddrinfo('example.com', 443" in str(seen["snippet"])
    assert seen["timeout_seconds"] == 20.0


def test_host_network_availability_rejects_noexec_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner.sys, "platform", "linux")
    monkeypatch.setattr(runner, "_running_as_root", lambda: False)
    monkeypatch.setattr(runner.shutil, "which", lambda _binary: "/usr/bin/mock")
    monkeypatch.setattr(runner, "_host_core_dump_boundary_safe", lambda: True)
    monkeypatch.setattr(runner, "_workspace_exec_boundary_safe", lambda _config: False)
    monkeypatch.setattr(
        runner,
        "_probe_sandbox_start",
        lambda *_args, **_kwargs: pytest.fail("noexec must fail before the launch probe"),
    )

    assert (
        network_sandbox_available(
            SandboxConfig(
                network_mode="host",
                python_bin=sys.executable,
                workspace_probe_root=str(tmp_path),
            )
        )
        is False
    )


def test_network_probe_checks_dns_and_tls_before_success() -> None:
    snippet = runner._network_probe_snippet("")

    assert "socket.getaddrinfo('example.com', 443" in snippet
    assert "ssl.create_default_context().wrap_socket" in snippet
    assert "venv.EnvBuilder(with_pip=True, symlinks=False)" in snippet
    assert "sys.exit(14)" in snippet
    assert "sys.exit(12)" in snippet
    assert "sys.exit(13)" in snippet
    assert snippet.index("socket.getaddrinfo") < snippet.index("ssl.create_default_context")
    compile(snippet, "<network-probe>", "exec")


def test_network_probe_timeout_stops_transient_unit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, str] = {}
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_write_bpf(unit_name: str) -> Path:
        path = tmp_path / f"{unit_name}.bpf"
        path.write_bytes(b"bpf")
        return path

    def fake_build(*_args: object, **kwargs: object) -> list[str]:
        unit_name = kwargs["unit_name"]
        assert isinstance(unit_name, str)
        captured["unit_name"] = unit_name
        return ["probe"]

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append((command, kwargs))
        if command == ["probe"]:
            raise subprocess.TimeoutExpired(command, timeout=20.0)
        return subprocess.CompletedProcess(command, 0, stderr=b"")

    monkeypatch.setattr(runner, "_write_bpf_file", fake_write_bpf)
    monkeypatch.setattr(runner, "_runtime_bpf_dir", lambda: tmp_path)
    monkeypatch.setattr(runner, "build_sandbox_command_argv", fake_build)
    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    assert runner._probe_network_sandbox_start(_net_config()) is False

    unit_name = captured["unit_name"]
    assert len(calls) == 2
    assert calls[1][0] == [
        "systemctl",
        "--user",
        "stop",
        f"{unit_name}.service",
    ]
    assert calls[1][1]["timeout"] == 5.0
    assert calls[1][1]["check"] is False
    assert not (tmp_path / f"{unit_name}.bpf").exists()
    assert not (tmp_path / unit_name).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["active", "activating", "deactivating", None])
async def test_network_unit_stop_requires_confirmed_inactive_state(
    state: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    class StopProcess:
        returncode = 0

        async def wait(self) -> int:
            return 0

    async def fake_subprocess(*args: object, **kwargs: object) -> StopProcess:
        del args, kwargs
        return StopProcess()

    unit_state = AsyncMock(return_value=state)
    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", fake_subprocess)
    monkeypatch.setattr(runner, "_user_unit_state", unit_state)
    monkeypatch.setattr(runner, "_SYSTEMCTL_TIMEOUT_SECONDS", 0.001)

    with pytest.raises(SandboxTeardownError, match="could not be stopped safely"):
        await runner._stop_user_unit("network-unit")

    assert unit_state.await_count >= 1


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["inactive", "failed", "unknown"])
async def test_network_unit_stop_accepts_terminal_inactive_state(
    state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    class StopProcess:
        returncode = 0

        async def wait(self) -> int:
            return 0

    async def fake_subprocess(*args: object, **kwargs: object) -> StopProcess:
        del args, kwargs
        return StopProcess()

    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", fake_subprocess)
    monkeypatch.setattr(runner, "_user_unit_state", AsyncMock(return_value=state))

    await runner._stop_user_unit("network-unit")


@pytest.mark.asyncio
async def test_network_unit_stop_polls_transient_state_until_inactive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StopProcess:
        returncode = 0

        async def wait(self) -> int:
            return 0

    async def fake_subprocess(*args: object, **kwargs: object) -> StopProcess:
        del args, kwargs
        return StopProcess()

    unit_state = AsyncMock(side_effect=["deactivating", "inactive"])
    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", fake_subprocess)
    monkeypatch.setattr(runner, "_user_unit_state", unit_state)
    monkeypatch.setattr(runner.asyncio, "sleep", AsyncMock())

    await runner._stop_user_unit("network-unit")

    assert unit_state.await_count == 2


@pytest.mark.asyncio
async def test_network_unit_query_failure_becomes_teardown_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SystemctlProcess:
        returncode = 0

        async def wait(self) -> int:
            return 0

        async def communicate(self) -> tuple[bytes, bytes]:
            raise RuntimeError("dbus disappeared")

        def kill(self) -> None:
            return

    async def fake_subprocess(*args: object, **kwargs: object) -> SystemctlProcess:
        del args, kwargs
        return SystemctlProcess()

    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", fake_subprocess)
    monkeypatch.setattr(runner, "_SYSTEMCTL_TIMEOUT_SECONDS", 0.001)

    with pytest.raises(SandboxTeardownError, match="state unavailable"):
        await runner._stop_user_unit("network-unit")


@pytest.mark.asyncio
async def test_cancelled_unconfirmed_unit_stop_poisons_and_wakes_netns_waiter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop_started = asyncio.Event()
    finish_stop = asyncio.Event()

    async def unconfirmed_stop(unit_name: str) -> None:
        del unit_name
        stop_started.set()
        await finish_stop.wait()
        raise SandboxTeardownError("unit remained active")

    monkeypatch.setattr(runner, "_stop_user_unit_confirmed", unconfirmed_stop)
    lease = NetnsLease()

    async def cleanup_while_leased() -> None:
        async with lease:
            await runner._stop_user_unit("network-unit")

    cleanup = asyncio.create_task(cleanup_while_leased())
    await stop_started.wait()
    waiter = asyncio.create_task(lease.acquire())
    cleanup.cancel()
    await asyncio.sleep(0)
    assert not cleanup.done()
    assert not waiter.done()

    finish_stop.set()
    with pytest.raises(SandboxTeardownError, match="unit remained active"):
        await cleanup
    with pytest.raises(NetnsLeasePoisonedError, match="unavailable until restart"):
        await waiter

    assert lease.poisoned
    assert not lease.locked()


@pytest.mark.asyncio
async def test_cancelled_network_spawn_gets_process_handle_before_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawn_started = asyncio.Event()
    allow_spawn = asyncio.Event()
    cleanup_process: list[object | None] = []
    process = object()

    async def spawn(*args: object, **kwargs: object) -> object:
        del args, kwargs
        spawn_started.set()
        await allow_spawn.wait()
        return process

    async def cleanup(
        quota_task: asyncio.Task[None] | None,
        proc: object | None,
        unit_name: str | None,
    ) -> None:
        del quota_task
        assert unit_name == "network-unit"
        cleanup_process.append(proc)

    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(runner, "_finish_run_cleanup", cleanup)

    run = asyncio.create_task(
        runner._run_built_command(
            SandboxConfig(network_mode="netns"),
            tmp_path,
            ["systemd-run"],
            seccomp_fd=None,
            unit_name="network-unit",
            stdin=None,
        )
    )
    await asyncio.wait_for(spawn_started.wait(), timeout=1.0)
    run.cancel()
    await asyncio.sleep(0)
    assert not run.done()

    allow_spawn.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(run, timeout=1.0)
    assert cleanup_process == [process]


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="cleanup path uses os.killpg")
async def test_network_cleanup_reaps_launcher_before_final_unit_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class LauncherProcess:
        pid = 1234

        def __init__(self) -> None:
            self.returncode: int | None = None

        async def wait(self) -> int:
            events.append("reap-launcher")
            self.returncode = -9
            return -9

    process = LauncherProcess()

    def killpg(pid: int, sig: int) -> None:
        assert pid == process.pid
        assert sig == signal.SIGKILL
        events.append("kill-launcher")

    async def stop_unit(unit_name: str) -> None:
        assert unit_name == "network-unit"
        events.append("stop-unit")

    monkeypatch.setattr(runner.os, "killpg", killpg)
    monkeypatch.setattr(runner, "_stop_user_unit", stop_unit)

    await runner._cleanup_process_group(
        process,  # type: ignore[arg-type]
        "network-unit",
    )

    assert events == ["kill-launcher", "reap-launcher", "stop-unit"]


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="cleanup path uses os.killpg")
async def test_network_cleanup_fails_closed_when_launcher_cannot_be_reaped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StuckLauncherProcess:
        pid = 1234
        returncode: int | None = None

        async def wait(self) -> int:
            raise TimeoutError

    stop_unit = AsyncMock()
    monkeypatch.setattr(runner.os, "killpg", lambda pid, sig: None)
    monkeypatch.setattr(runner, "_stop_user_unit", stop_unit)

    with pytest.raises(SandboxTeardownError, match="launcher did not exit"):
        await runner._cleanup_process_group(
            StuckLauncherProcess(),  # type: ignore[arg-type]
            "network-unit",
        )

    stop_unit.assert_awaited_once_with("network-unit")


def test_workspace_scan_failure_is_not_reported_as_quota_excess() -> None:
    note = runner._workspace_failure_note("workspace tree could not be inspected safely")

    assert note == (
        "Execution stopped because workspace accounting could not be completed safely: "
        "workspace tree could not be inspected safely."
    )


def test_workspace_limit_failure_is_reported_as_quota_excess() -> None:
    note = runner._workspace_failure_note("11 files exceeds 10 files")

    assert note == (
        "Execution stopped because the workspace quota was exceeded: 11 files exceeds 10 files."
    )


def test_workspace_quota_monitor_defaults_to_five_second_polling() -> None:
    cfg = SandboxConfig()

    assert cfg.workspace_quota_poll_seconds == 5.0
    assert cfg.workspace_quota_scan_retries == 4


def test_workspace_quota_retries_transient_disappearing_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scans = [
        runner._WorkspaceUsage(
            bytes=0,
            files=0,
            scan_error=runner._WorkspaceScanError(
                area="workspace", errno=errno.ENOENT, relative_path="repo/.tmp-file"
            ),
        ),
        runner._WorkspaceUsage(bytes=10, files=1),
    ]
    monkeypatch.setattr(runner, "_workspace_usage", lambda *_args: scans.pop(0))

    reason = runner._workspace_quota_reason(SandboxConfig(), tmp_path)

    assert reason is None
    assert scans == []


def test_workspace_quota_fails_after_four_transient_scan_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    attempts = 0

    def missing_entry(*_args) -> runner._WorkspaceUsage:
        nonlocal attempts
        attempts += 1
        return runner._WorkspaceUsage(
            bytes=0,
            files=0,
            scan_error=runner._WorkspaceScanError(
                area="workspace", errno=errno.ENOENT, relative_path="repo/.tmp-file"
            ),
        )

    monkeypatch.setattr(runner, "_workspace_usage", missing_entry)
    monkeypatch.setattr(runner.time, "sleep", lambda _delay: None)

    reason = runner._workspace_quota_reason(SandboxConfig(), tmp_path)

    assert attempts == 4
    assert reason == "workspace tree could not be inspected safely"
    assert "errno=2 path=repo/.tmp-file attempt=4/4" in caplog.text


def test_workspace_quota_does_not_retry_permission_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts = 0

    def denied(*_args) -> runner._WorkspaceUsage:
        nonlocal attempts
        attempts += 1
        return runner._WorkspaceUsage(
            bytes=0,
            files=0,
            scan_error=runner._WorkspaceScanError(
                area="environment", errno=errno.EACCES, relative_path=".venv/locked"
            ),
        )

    monkeypatch.setattr(runner, "_workspace_usage", denied)

    reason = runner._workspace_quota_reason(SandboxConfig(), tmp_path)

    assert attempts == 1
    assert reason == "environment tree could not be inspected safely"


@pytest.mark.asyncio
async def test_workspace_quota_monitor_uses_configured_poll_interval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Proc:
        returncode: int | None = None

    proc = Proc()
    sleeps: list[float] = []
    monkeypatch.setattr(runner, "_workspace_quota_reason", lambda *_args: None)

    async def finish_after_sleep(delay: float) -> None:
        sleeps.append(delay)
        proc.returncode = 0

    monkeypatch.setattr(runner.asyncio, "sleep", finish_after_sleep)
    cfg = SandboxConfig(workspace_quota_poll_seconds=7.5)

    await runner._monitor_workspace_quota(
        proc,  # type: ignore[arg-type]
        tmp_path,
        cfg,
        runner._QuotaState(),
    )

    assert sleeps == [7.5]


def test_workspace_usage_splits_env_dir_bytes(tmp_path: Path) -> None:
    ws = tmp_path / "work"
    (ws / ".venv" / "lib").mkdir(parents=True)
    (ws / "notes.txt").write_bytes(b"x" * 100)
    (ws / ".venv" / "lib" / "big.so").write_bytes(b"y" * 5000)
    cfg = SandboxConfig(
        env_dir_names=(".venv", ".pio"),
        max_env_bytes=1_000_000,
        max_env_files=100,
    )
    usage = runner._workspace_usage(cfg, ws)
    assert usage.bytes == 100  # only the doc file counts toward doc bytes
    assert usage.env_bytes >= 5000  # the .so plus env dir inodes, never doc bytes
    assert usage.files == 1
    assert usage.env_files == 3  # .venv, lib, and big.so all consume entries


def test_workspace_quota_reason_flags_env_over_allowance(tmp_path: Path) -> None:
    ws = tmp_path / "work"
    (ws / ".venv").mkdir(parents=True)
    (ws / ".venv" / "big").write_bytes(b"y" * 5000)
    cfg = SandboxConfig(env_dir_names=(".venv",), max_env_bytes=1000)
    reason = runner._workspace_quota_reason(cfg, ws)
    assert reason is not None and reason.startswith("environment uses")


def test_workspace_quota_counts_zero_byte_environment_entries(tmp_path: Path) -> None:
    ws = tmp_path / "work"
    env = ws / ".venv" / "attack"
    env.mkdir(parents=True)
    for index in range(20):
        (env / str(index)).touch()
    cfg = SandboxConfig(
        env_dir_names=(".venv",),
        max_env_bytes=1_000_000,
        max_env_files=10,
    )

    usage = runner._workspace_usage(cfg, ws)
    reason = runner._workspace_quota_reason(cfg, ws)

    # The walk terminates at limit + 1 rather than traversing the complete tree.
    assert usage.env_files == 11
    assert reason == "environment has 11 entries; limit is 10 entries"


def test_workspace_usage_records_scan_errno_and_relative_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = tmp_path / "work"
    transient = ws / "repo" / ".tmp-file"
    transient.mkdir(parents=True)
    real_scandir = os.scandir

    def missing_transient(path: str | os.PathLike[str]):
        if Path(path) == transient:
            raise FileNotFoundError(errno.ENOENT, "simulated rename race", path)
        return real_scandir(path)

    monkeypatch.setattr(runner.os, "scandir", missing_transient)

    usage = runner._workspace_usage(SandboxConfig(), ws)

    assert usage.scan_error == runner._WorkspaceScanError(
        area="workspace", errno=errno.ENOENT, relative_path="repo/.tmp-file"
    )


def test_workspace_usage_records_scandir_iteration_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = tmp_path / "work"
    ws.mkdir()

    class StaleScandir:
        def __enter__(self) -> StaleScandir:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def __iter__(self) -> StaleScandir:
            return self

        def __next__(self) -> os.DirEntry[str]:
            raise OSError(errno.ESTALE, "simulated stale readdir")

    monkeypatch.setattr(runner.os, "scandir", lambda _path: StaleScandir())

    usage = runner._workspace_usage(SandboxConfig(), ws)

    assert usage.scan_error == runner._WorkspaceScanError(
        area="workspace", errno=errno.ESTALE, relative_path="."
    )


def test_workspace_quota_fails_closed_when_env_tree_cannot_be_scanned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = tmp_path / "work"
    locked = ws / ".venv" / "locked"
    locked.mkdir(parents=True)
    (locked / "hidden").touch()
    real_scandir = os.scandir

    def deny_locked(path: str | os.PathLike[str]):
        if Path(path) == locked:
            raise PermissionError("simulated mode-000 directory")
        return real_scandir(path)

    monkeypatch.setattr(runner.os, "scandir", deny_locked)
    cfg = SandboxConfig(
        env_dir_names=(".venv",),
        max_env_bytes=1_000_000,
        max_env_files=100,
    )

    usage = runner._workspace_usage(cfg, ws)
    reason = runner._workspace_quota_reason(cfg, ws)

    assert usage.scan_error is not None
    assert usage.scan_error.area == "environment"
    assert usage.scan_error.errno is None
    assert usage.scan_error.relative_path == ".venv/locked"
    assert reason == "environment tree could not be inspected safely"


def test_build_command_wraps_everything_in_a_cgroup_scope(tmp_path: Path) -> None:
    ws = tmp_path / "work"
    ws.mkdir()
    (ws / "r.py").write_text("", encoding="utf-8")
    cfg = SandboxConfig(
        max_tasks=32,
        max_total_memory_mb=256,
        cpu_quota_percent=75,
        tmp_size_mb=16,
    )
    cmd = build_sandbox_command(cfg, ws, ws / "r.py", seccomp_fd=_FD)
    joined = " ".join(cmd)

    # systemd-run is outermost so the scope caps bound the whole prlimit+bwrap tree.
    assert cmd[0] == cfg.systemd_run_bin
    assert cmd[1:3] == ["--user", "--scope"]
    assert cmd.index(cfg.systemd_run_bin) < cmd.index(cfg.prlimit_bin) < cmd.index(cfg.bwrap_bin)
    # Whole-tree pid, real-memory, and aggregate CPU caps; swap stays off.
    assert "TasksMax=32" in cmd
    assert "MemoryMax=256M" in cmd
    assert "MemorySwapMax=0" in cmd
    assert "CPUQuota=75%" in cmd
    # Failed scopes (e.g. OOM kills) are reaped instead of accumulating.
    assert "--collect" in cmd
    # The private /tmp is a size-capped tmpfs (writes there are RAM).
    assert f"--size {16 * 1024 * 1024} --tmpfs /tmp" in joined


def test_build_command_uses_persisted_systemd_scope_name(tmp_path: Path) -> None:
    ws = tmp_path / "work"
    ws.mkdir()
    script = ws / "r.py"
    script.write_text("", encoding="utf-8")

    command = build_sandbox_command(
        SandboxConfig(),
        ws,
        script,
        seccomp_fd=_FD,
        unit_name="coding-job-abc.scope",
    )

    assert "--unit=coding-job-abc.scope" in command


def test_build_command_can_run_shell_and_direct_modes(tmp_path: Path) -> None:
    ws = tmp_path / "work"
    ws.mkdir()
    script = ws / "tool"
    script.write_text("echo hi", encoding="utf-8")

    shell_cmd = build_sandbox_command(
        SandboxConfig(),
        ws,
        script,
        seccomp_fd=_FD,
        mode="shell",
        argv=("one", "two"),
    )
    direct_cmd = build_sandbox_command(
        SandboxConfig(),
        ws,
        script,
        seccomp_fd=_FD,
        mode="direct",
        argv=("arg",),
    )

    assert shell_cmd[-4:] == ["/bin/sh", "/work/tool", "one", "two"]
    assert direct_cmd[-2:] == ["/work/tool", "arg"]


def test_build_command_masks_host_python_package_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = tmp_path / "work"
    ws.mkdir()
    script = ws / "run.py"
    script.write_text("print('x')", encoding="utf-8")

    monkeypatch.setattr(
        runner,
        "_system_python_package_dirs",
        lambda: (
            "/usr/lib/python3.11/dist-packages",
            "/usr/local/lib/python3.11/site-packages",
        ),
    )

    cmd = build_sandbox_command(SandboxConfig(), ws, script, seccomp_fd=_FD)
    joined = " ".join(cmd)

    assert cmd.index("/usr") < cmd.index("/usr/lib/python3.11/dist-packages")
    assert (
        "--tmpfs /usr/lib/python3.11/dist-packages --remount-ro /usr/lib/python3.11/dist-packages"
    ) in joined
    assert (
        "--tmpfs /usr/local/lib/python3.11/site-packages "
        "--remount-ro /usr/local/lib/python3.11/site-packages"
    ) in joined


def test_build_command_applies_rlimits(tmp_path: Path) -> None:
    ws = tmp_path / "work"
    ws.mkdir()
    (ws / "r.py").write_text("", encoding="utf-8")
    cmd = build_sandbox_command(
        SandboxConfig(max_memory_mb=256, max_cpu_seconds=5, max_fsize_mb=10, max_open_files=64),
        ws,
        ws / "r.py",
        seccomp_fd=_FD,
    )
    assert f"--as={256 * 1024 * 1024}" in cmd
    assert "--cpu=5" in cmd
    assert "--core=0:0" in cmd
    assert f"--fsize={10 * 1024 * 1024}" in cmd
    assert "--nofile=64" in cmd
    # prlimit wraps bwrap (prlimit appears before bwrap in argv).
    assert cmd.index(SandboxConfig().prlimit_bin) < cmd.index(SandboxConfig().bwrap_bin)


def test_build_command_adds_extra_ro_binds_and_mpl_backend(tmp_path: Path) -> None:
    ws = tmp_path / "work"
    ws.mkdir()
    (ws / "r.py").write_text("", encoding="utf-8")
    cfg = SandboxConfig(extra_ro_binds=("/home/x/venv", "/etc/fonts"))
    cmd = build_sandbox_command(cfg, ws, ws / "r.py", seccomp_fd=_FD)
    joined = " ".join(cmd)
    # Headless matplotlib backend is set.
    assert cmd[cmd.index("MPLBACKEND") + 1] == "Agg"
    # Each extra path is bound read-only (tolerant of a missing path) at itself.
    assert "--ro-bind-try /home/x/venv /home/x/venv" in joined
    assert "--ro-bind-try /etc/fonts /etc/fonts" in joined
    # Extra binds precede the writable /work mount.
    assert cmd.index("/home/x/venv") < cmd.index("/work")


def test_build_command_rejects_script_outside_workspace(tmp_path: Path) -> None:
    ws = tmp_path / "work"
    ws.mkdir()
    outside = tmp_path / "evil.py"
    outside.write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="inside the workspace"):
        build_sandbox_command(SandboxConfig(), ws, outside, seccomp_fd=_FD)


def test_sandbox_unavailable_with_bogus_binaries() -> None:
    cfg = SandboxConfig(bwrap_bin="definitely-not-a-binary-xyz", prlimit_bin="nope-xyz")
    assert sandbox_available(cfg) is False


def test_host_core_dump_boundary_rejects_piped_handler() -> None:
    assert runner._core_pattern_is_safe("|/usr/lib/systemd/systemd-coredump %P") is False


def test_host_core_dump_boundary_accepts_file_pattern() -> None:
    assert runner._core_pattern_is_safe("core.%p") is True


@pytest.mark.asyncio
async def test_run_fails_closed_when_host_core_handler_is_piped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner, "_host_core_dump_boundary_safe", lambda: False)

    result = await runner._run_built_command(
        SandboxConfig(),
        tmp_path,
        ["must-not-run"],
        seccomp_fd=None,
        stdin=None,
    )

    assert result.exit_code == 1
    assert "core-dump handler is not safely bounded" in result.stderr


def test_sandbox_available_probes_bwrap_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stderr=b"")

    monkeypatch.setattr(runner.sys, "platform", "linux")
    monkeypatch.setattr(runner, "_running_as_root", lambda: False)
    monkeypatch.setattr(runner.shutil, "which", lambda _binary: "/usr/bin/mock")
    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    monkeypatch.setattr(runner, "open_bpf_fd", lambda: os.open(os.devnull, os.O_RDONLY))
    monkeypatch.setattr(runner, "_host_core_dump_boundary_safe", lambda: True)

    assert sandbox_available(SandboxConfig(python_bin=sys.executable)) is True
    assert calls
    assert "--unshare-all" in calls[0]
    assert "--seccomp" in calls[0]


def test_sandbox_unavailable_when_bwrap_probe_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(command, 1, stderr=b"namespace denied")

    monkeypatch.setattr(runner.shutil, "which", lambda _binary: "/usr/bin/mock")
    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    monkeypatch.setattr(runner, "open_bpf_fd", lambda: os.open(os.devnull, os.O_RDONLY))
    monkeypatch.setattr(runner, "_host_core_dump_boundary_safe", lambda: True)

    assert sandbox_available(SandboxConfig(python_bin=sys.executable)) is False


def test_sandbox_unavailable_when_seccomp_cannot_build(monkeypatch: pytest.MonkeyPatch) -> None:
    # A host without libseccomp must not fall back to an unfiltered sandbox.
    def boom() -> int:
        raise seccomp.SeccompUnavailableError("no libseccomp")

    monkeypatch.setattr(runner.shutil, "which", lambda _binary: "/usr/bin/mock")
    monkeypatch.setattr(runner, "open_bpf_fd", boom)
    monkeypatch.setattr(runner, "_host_core_dump_boundary_safe", lambda: True)

    assert sandbox_available(SandboxConfig(python_bin=sys.executable)) is False


# --- seccomp filter --------------------------------------------------------


def test_seccomp_bpf_bytes_is_valid_and_cached() -> None:
    _requires_libseccomp()
    data = seccomp.seccomp_bpf_bytes()
    assert data
    assert len(data) % 8 == 0  # whole sock_filter entries
    assert seccomp.seccomp_bpf_bytes() is data  # built once, cached


def test_seccomp_critical_names_resolve_natively() -> None:
    # Building skips names unknown on the running arch, so a typo in a
    # load-bearing entry would silently weaken the list; pin them here.
    _requires_libseccomp()
    lib = seccomp._load_libseccomp()
    for name in (
        "bpf",
        "keyctl",
        "ptrace",
        "mount",
        "unshare",
        "io_uring_setup",
        "userfaultfd",
        "perf_event_open",
        "personality",
        "init_module",
        "open_by_handle_at",
    ):
        assert name in seccomp.DENIED_SYSCALLS
        assert lib.seccomp_syscall_resolve_name(name.encode("ascii")) >= 0, name


def test_open_bpf_fd_holds_the_program_read_positioned() -> None:
    _requires_libseccomp()
    fd = seccomp.open_bpf_fd()
    try:
        data = os.read(fd, 1 << 20)
    finally:
        os.close(fd)
    assert data == seccomp.seccomp_bpf_bytes()


@pytest.mark.asyncio
async def test_run_rejects_workspace_already_over_total_byte_quota(tmp_path: Path) -> None:
    _requires_libseccomp()
    ws = tmp_path / "work"
    ws.mkdir()
    script = ws / "run.py"
    script.write_text("print('x')", encoding="utf-8")
    (ws / "existing.txt").write_text("x" * 20, encoding="utf-8")

    result = await run_python_in_sandbox(
        SandboxConfig(max_workspace_bytes=10, max_workspace_files=100),
        ws,
        script,
    )

    assert result.exit_code == 1
    assert result.timed_out is False
    assert result.quota_exceeded is True
    assert "Workspace quota exceeded before execution" in result.stderr
    assert "bytes exceeds" in result.stderr


@pytest.mark.asyncio
async def test_run_reports_preflight_accounting_failure_without_quota_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner, "_host_core_dump_boundary_safe", lambda: True)
    monkeypatch.setattr(
        runner,
        "_workspace_quota_reason",
        lambda *_args: "environment tree could not be inspected safely",
    )

    result = await runner._run_built_command(
        SandboxConfig(),
        tmp_path,
        ["must-not-run"],
        seccomp_fd=None,
        stdin=None,
    )

    assert result.exit_code == 1
    assert result.quota_exceeded is False
    assert result.environment_quota_exceeded is False
    assert result.stderr == (
        "Workspace accounting could not be completed safely before execution: "
        "environment tree could not be inspected safely."
    )


@pytest.mark.asyncio
async def test_run_rejects_workspace_already_over_file_count_quota(tmp_path: Path) -> None:
    _requires_libseccomp()
    ws = tmp_path / "work"
    ws.mkdir()
    script = ws / "run.py"
    script.write_text("print('x')", encoding="utf-8")
    (ws / "other.txt").write_text("x", encoding="utf-8")

    result = await run_python_in_sandbox(
        SandboxConfig(max_workspace_bytes=10_000, max_workspace_files=1),
        ws,
        script,
    )

    assert result.exit_code == 1
    assert result.quota_exceeded is True
    assert "Workspace quota exceeded before execution" in result.stderr
    assert "files exceeds" in result.stderr


@pytest.mark.asyncio
async def test_run_rejects_preexisting_environment_entry_overage(tmp_path: Path) -> None:
    _requires_libseccomp()
    ws = tmp_path / "work"
    env = ws / ".venv"
    env.mkdir(parents=True)
    script = ws / "run.py"
    script.write_text("print('must not run')", encoding="utf-8")
    (env / "one").touch()
    (env / "two").touch()

    result = await run_python_in_sandbox(
        SandboxConfig(
            max_workspace_bytes=10_000,
            max_workspace_files=100,
            env_dir_names=(".venv",),
            max_env_bytes=10_000,
            max_env_files=2,
        ),
        ws,
        script,
    )

    assert result.exit_code == 1
    assert result.quota_exceeded is True
    assert result.environment_quota_exceeded is True
    assert "environment has 3 entries; limit is 2 entries" in result.stderr


# --- real execution (gated on a host with bwrap) --------------------------


@_requires_sandbox
@pytest.mark.asyncio
async def test_multi_file_allocation_trips_workspace_monitor(tmp_path: Path) -> None:
    ws = tmp_path / "work"
    ws.mkdir()
    script = ws / "allocate.py"
    script.write_text(
        "chunk = b'x' * 65536\n"
        "for index in range(256):\n"
        "    open(f'chunk-{index}', 'wb').write(chunk)\n",
        encoding="utf-8",
    )

    result = await run_python_in_sandbox(
        SandboxConfig(
            max_workspace_bytes=512 * 1024,
            max_workspace_files=300,
            max_fsize_mb=1,
        ),
        ws,
        script,
    )

    assert result.quota_exceeded is True
    assert "workspace quota was exceeded" in result.stderr


@_requires_sandbox
@pytest.mark.asyncio
async def test_runs_script_and_captures_stdout(tmp_path: Path) -> None:
    ws = tmp_path / "work"
    ws.mkdir()
    (ws / "hello.py").write_text("print('hi'); print('sum', 2 + 2)", encoding="utf-8")
    result = await run_python_in_sandbox(SandboxConfig(), ws, ws / "hello.py")
    assert result.exit_code == 0
    assert result.timed_out is False
    assert "hi" in result.stdout
    assert "sum 4" in result.stdout


@_requires_sandbox
@pytest.mark.asyncio
async def test_core_dump_limit_is_hard_zero(tmp_path: Path) -> None:
    ws = tmp_path / "work"
    ws.mkdir()
    (ws / "core.py").write_text(
        "import resource\n"
        "print(resource.getrlimit(resource.RLIMIT_CORE))\n"
        "try:\n"
        "    resource.setrlimit(resource.RLIMIT_CORE, "
        "(resource.RLIM_INFINITY, resource.RLIM_INFINITY))\n"
        "except (ValueError, OSError):\n"
        "    print('core blocked')\n"
        "else:\n"
        "    print('CORE ENABLED')\n",
        encoding="utf-8",
    )

    result = await run_python_in_sandbox(SandboxConfig(), ws, ws / "core.py")

    assert result.exit_code == 0, result.stderr
    assert "(0, 0)" in result.stdout
    assert "core blocked" in result.stdout
    assert "CORE ENABLED" not in result.stdout


@_requires_sandbox
@pytest.mark.asyncio
async def test_actual_crash_cannot_create_a_core_dump(tmp_path: Path) -> None:
    ws = tmp_path / "work"
    ws.mkdir()
    (ws / "crash.py").write_text(
        "import os\nprint('about to crash', flush=True)\nos.abort()\n",
        encoding="utf-8",
    )

    result = await run_python_in_sandbox(SandboxConfig(), ws, ws / "crash.py")

    assert result.exit_code not in (0, None)
    assert "about to crash" in result.stdout
    assert not list(ws.glob("core"))
    assert not list(ws.glob("core.*"))


@_requires_sandbox
@pytest.mark.asyncio
async def test_runs_workspace_file_with_shell_mode_and_argv(tmp_path: Path) -> None:
    ws = tmp_path / "work"
    ws.mkdir()
    script = ws / "hello.sh"
    script.write_text(
        "printf 'arg=%s\\n' \"$1\"\nread line\nprintf 'stdin=%s\\n' \"$line\"\n",
        encoding="utf-8",
    )

    result = await run_workspace_file_in_sandbox(
        SandboxConfig(),
        ws,
        script,
        mode="shell",
        argv=("ping",),
        stdin="pong\n",
    )

    assert result.exit_code == 0
    assert "arg=ping" in result.stdout
    assert "stdin=pong" in result.stdout


@_requires_sandbox
@pytest.mark.asyncio
async def test_system_package_dirs_are_masked_inside_sandbox(tmp_path: Path) -> None:
    masked_paths = runner._system_python_package_dirs()
    if not masked_paths:
        pytest.skip("host has no system Python package dirs to mask")

    ws = tmp_path / "work"
    ws.mkdir()
    (ws / "packages.py").write_text(
        "import json, os\n"
        f"paths = {masked_paths!r}\n"
        "print(json.dumps({p: os.listdir(p) for p in paths if os.path.isdir(p)}))",
        encoding="utf-8",
    )

    result = await run_python_in_sandbox(SandboxConfig(), ws, ws / "packages.py")

    assert result.exit_code == 0, result.stderr
    visible = json.loads(result.stdout)
    assert visible
    assert all(entries == [] for entries in visible.values())


@_requires_sandbox
@pytest.mark.asyncio
async def test_script_can_write_only_to_workspace(tmp_path: Path) -> None:
    ws = tmp_path / "work"
    ws.mkdir()
    (ws / "w.py").write_text(
        "open('out.txt','w').write('ok')\n"
        "try:\n open('/usr/x','w'); print('USR_WRITE')\n"
        "except OSError: print('usr blocked')\n",
        encoding="utf-8",
    )
    result = await run_python_in_sandbox(SandboxConfig(), ws, ws / "w.py")
    assert (ws / "out.txt").read_text() == "ok"  # workspace write landed on host
    assert "usr blocked" in result.stdout
    assert "USR_WRITE" not in result.stdout


@_requires_sandbox
@pytest.mark.asyncio
async def test_network_is_blocked(tmp_path: Path) -> None:
    ws = tmp_path / "work"
    ws.mkdir()
    (ws / "net.py").write_text(
        "import socket\n"
        "socket.setdefaulttimeout(3)\n"
        "try:\n socket.create_connection(('1.1.1.1', 53)); print('NET_OK')\n"
        "except OSError as e: print('net blocked')\n",
        encoding="utf-8",
    )
    result = await run_python_in_sandbox(SandboxConfig(), ws, ws / "net.py")
    assert "net blocked" in result.stdout
    assert "NET_OK" not in result.stdout


@_requires_sandbox
@pytest.mark.asyncio
async def test_host_filesystem_is_invisible(tmp_path: Path) -> None:
    ws = tmp_path / "work"
    ws.mkdir()
    (ws / "fs.py").write_text(
        "import os\n"
        "print('repo', os.path.exists('/home/x/repo'))\n"
        "print('passwd', os.path.exists('/etc/passwd'))\n",
        encoding="utf-8",
    )
    result = await run_python_in_sandbox(SandboxConfig(), ws, ws / "fs.py")
    assert "repo False" in result.stdout
    assert "passwd False" in result.stdout


@_requires_sandbox
@pytest.mark.asyncio
async def test_wall_timeout_kills_runaway(tmp_path: Path) -> None:
    ws = tmp_path / "work"
    ws.mkdir()
    (ws / "loop.py").write_text("while True: pass", encoding="utf-8")
    result = await run_python_in_sandbox(
        SandboxConfig(wall_timeout_seconds=2.0, max_cpu_seconds=30), ws, ws / "loop.py"
    )
    assert result.timed_out is True
    assert result.exit_code is None
    assert result.duration_ms < 10_000  # killed promptly, not left running


@_requires_sandbox
@pytest.mark.asyncio
async def test_output_plus_timeout_does_not_hang(tmp_path: Path) -> None:
    # A script that floods stdout AND never exits must still return promptly
    # (bounded reads + clean cancel), not wedge on a half-drained transport.
    ws = tmp_path / "work"
    ws.mkdir()
    (ws / "spam.py").write_text(
        "import sys\nwhile True:\n sys.stdout.write('x' * 4096); sys.stdout.flush()",
        encoding="utf-8",
    )
    result = await asyncio.wait_for(
        run_python_in_sandbox(
            SandboxConfig(wall_timeout_seconds=2.0, max_output_bytes=4096), ws, ws / "spam.py"
        ),
        timeout=20,  # the call itself must return well under this
    )
    assert result.timed_out is True
    assert len(result.stdout) <= 4096 + 200  # capped, not the whole flood


@_requires_sandbox
@pytest.mark.asyncio
async def test_high_volume_output_is_capped_not_buffered(tmp_path: Path) -> None:
    # Printing far more than the cap must not buffer it all in the bot process;
    # the returned output is bounded to the cap.
    ws = tmp_path / "work"
    ws.mkdir()
    (ws / "big.py").write_text(
        "import sys\nfor _ in range(20000): sys.stdout.write('y' * 4096)\nprint('DONE')",
        encoding="utf-8",
    )
    result = await asyncio.wait_for(
        run_python_in_sandbox(
            SandboxConfig(wall_timeout_seconds=10.0, max_output_bytes=10_000), ws, ws / "big.py"
        ),
        timeout=20,
    )
    # The script writes ~80 MB and exits 0: it must be reaped as a clean exit
    # (not misreported as a timeout), with output bounded to the cap.
    assert result.timed_out is False
    assert result.exit_code == 0
    assert len(result.stdout) <= 10_000 + 65_536  # cap + at most one chunk's slack


@_requires_sandbox
@pytest.mark.asyncio
async def test_memory_limit_is_enforced(tmp_path: Path) -> None:
    ws = tmp_path / "work"
    ws.mkdir()
    (ws / "mem.py").write_text(
        "try:\n b = bytearray(2 * 1024 * 1024 * 1024); print('ALLOC_OK')\n"
        "except MemoryError: print('mem blocked')\n",
        encoding="utf-8",
    )
    result = await run_python_in_sandbox(SandboxConfig(max_memory_mb=256), ws, ws / "mem.py")
    assert "mem blocked" in result.stdout
    assert "ALLOC_OK" not in result.stdout


@_requires_sandbox
@pytest.mark.asyncio
async def test_fork_count_is_capped_by_the_scope(tmp_path: Path) -> None:
    # RLIMIT_NPROC cannot cap forks on a shared uid; the scope's TasksMax can.
    # Children stay alive briefly so the concurrent-task cap actually binds.
    ws = tmp_path / "work"
    ws.mkdir()
    (ws / "forks.py").write_text(
        "import os, time\n"
        "ok = blocked = 0\n"
        "for _ in range(30):\n"
        "    try:\n"
        "        pid = os.fork()\n"
        "    except OSError:\n"
        "        blocked += 1\n"
        "        continue\n"
        "    if pid == 0:\n"
        "        time.sleep(2)\n"
        "        os._exit(0)\n"
        "    ok += 1\n"
        "print('ok', ok, 'blocked', blocked)\n",
        encoding="utf-8",
    )
    result = await run_python_in_sandbox(SandboxConfig(max_tasks=10), ws, ws / "forks.py")
    assert result.exit_code == 0, result.stderr
    parts = result.stdout.split()
    ok, blocked = int(parts[1]), int(parts[3])
    assert ok < 30
    assert blocked > 0


@_requires_sandbox
@pytest.mark.asyncio
async def test_total_memory_across_forks_is_capped_by_the_scope(tmp_path: Path) -> None:
    # RLIMIT_AS is per process; MemoryMax on the scope caps what the tree
    # actually touches. The hog is OOM-killed before it can report success.
    ws = tmp_path / "work"
    ws.mkdir()
    (ws / "hog.py").write_text(
        "data = bytearray(300 * 1024 * 1024)\nprint('ALLOC_OK')\n",
        encoding="utf-8",
    )
    result = await run_python_in_sandbox(
        SandboxConfig(max_total_memory_mb=128, max_memory_mb=2048), ws, ws / "hog.py"
    )
    assert "ALLOC_OK" not in result.stdout
    assert result.exit_code not in (0, None)


@_requires_sandbox
@pytest.mark.asyncio
async def test_nested_user_namespace_creation_is_blocked(tmp_path: Path) -> None:
    # Cutting nested unprivileged userns removes the largest class of kernel
    # LPEs reachable from inside the sandbox. Code still runs; the unshare fails.
    ws = tmp_path / "work"
    ws.mkdir()
    (ws / "userns.py").write_text(
        "import ctypes, os\n"
        "print('ran')\n"
        "rc = ctypes.CDLL(None, use_errno=True).unshare(0x10000000)  # CLONE_NEWUSER\n"
        "print('NESTED_OK' if rc == 0 else 'nested blocked')\n",
        encoding="utf-8",
    )
    result = await run_python_in_sandbox(SandboxConfig(), ws, ws / "userns.py")
    assert "ran" in result.stdout
    assert "nested blocked" in result.stdout
    assert "NESTED_OK" not in result.stdout


@_requires_sandbox
@pytest.mark.asyncio
async def test_denied_syscalls_return_eperm_inside_sandbox(tmp_path: Path) -> None:
    # Both calls always succeed for an unprivileged process (personality's
    # 0xffffffff is a pure query; unshare(0) is a no-op), so EPERM proves the
    # seccomp filter itself blocked them, not capabilities or namespaces.
    ws = tmp_path / "work"
    ws.mkdir()
    (ws / "sec.py").write_text(
        "import ctypes, errno\n"
        "libc = ctypes.CDLL(None, use_errno=True)\n"
        "rc = libc.personality(ctypes.c_ulong(0xFFFFFFFF))\n"
        "eperm = rc == -1 and ctypes.get_errno() == errno.EPERM\n"
        "print('personality', 'blocked' if eperm else 'OPEN')\n"
        "ctypes.set_errno(0)\n"
        "rc = libc.unshare(0)\n"
        "eperm = rc == -1 and ctypes.get_errno() == errno.EPERM\n"
        "print('unshare', 'blocked' if eperm else 'OPEN')\n",
        encoding="utf-8",
    )
    result = await run_python_in_sandbox(SandboxConfig(), ws, ws / "sec.py")
    assert result.exit_code == 0, result.stderr
    assert "personality blocked" in result.stdout
    assert "unshare blocked" in result.stdout
    assert "OPEN" not in result.stdout


@_requires_sandbox
@pytest.mark.asyncio
async def test_tmpfs_tmp_is_size_capped(tmp_path: Path) -> None:
    ws = tmp_path / "work"
    ws.mkdir()
    (ws / "tmpfill.py").write_text(
        "try:\n"
        "    with open('/tmp/fill', 'wb') as f:\n"
        "        f.write(b'x' * (8 * 1024 * 1024))\n"
        "    print('TMP_OK')\n"
        "except OSError:\n"
        "    print('tmp blocked')\n",
        encoding="utf-8",
    )
    result = await run_python_in_sandbox(SandboxConfig(tmp_size_mb=1), ws, ws / "tmpfill.py")
    assert "tmp blocked" in result.stdout
    assert "TMP_OK" not in result.stdout
