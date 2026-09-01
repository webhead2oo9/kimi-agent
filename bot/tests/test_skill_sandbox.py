from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from skills import sandbox as sandbox_module
from skills.runner import run_script
from config.settings import Settings
from skills.registration import build_script_sandbox_limits
from skills.sandbox import (
    SandboxRuntime,
    SandboxUnavailableError,
    ScriptSandboxLimits,
    build_sandbox_command,
    detect_sandbox_runtime,
    validate_sandbox_runtime,
)


def _command_fixture(tmp_path: Path, *, allow_network: bool = False) -> list[str]:
    skill = tmp_path / "skill"
    workspace = tmp_path / "workspace"
    interpreter = tmp_path / "runtime" / "python"
    script = skill / "scripts" / "run.py"
    workspace.mkdir()
    interpreter.parent.mkdir()
    interpreter.touch()
    script.parent.mkdir(parents=True)
    script.touch()
    return build_sandbox_command(
        runtime=SandboxRuntime(bwrap="/usr/bin/bwrap", prlimit="/usr/bin/prlimit"),
        limits=ScriptSandboxLimits(
            memory_bytes=123,
            cpu_seconds=7,
            file_size_bytes=456,
            open_files=32,
            processes=8,
            tmpfs_bytes=789,
        ),
        interpreter=interpreter,
        resolved_script=script,
        skill_dir=skill,
        workspace_dir=workspace,
        allow_network=allow_network,
    )


def test_build_sandbox_command_isolates_by_default(tmp_path: Path) -> None:
    command = _command_fixture(tmp_path)

    assert command[:8] == [
        "/usr/bin/prlimit",
        "--as=123",
        "--cpu=7",
        "--fsize=456",
        "--nofile=32",
        "--nproc=8",
        "--core=0",
        "--",
    ]
    assert "--unshare-all" in command
    assert "--unshare-user" in command
    assert "--disable-userns" in command
    assert "--cap-drop" in command
    assert "--share-net" not in command
    assert ["--ro-bind", str((tmp_path / "skill").resolve()), "/skill"] == command[
        command.index(str((tmp_path / "skill").resolve())) - 1 : command.index(
            str((tmp_path / "skill").resolve())
        )
        + 2
    ]
    assert ["--bind", str((tmp_path / "workspace").resolve()), "/workspace"] == command[
        command.index(str((tmp_path / "workspace").resolve())) - 1 : command.index(
            str((tmp_path / "workspace").resolve())
        )
        + 2
    ]
    assert command[-1] == "/skill/scripts/run.py"


def test_build_sandbox_command_network_is_explicit(tmp_path: Path) -> None:
    command = _command_fixture(tmp_path, allow_network=True)

    assert "--share-net" in command
    assert "/etc/resolv.conf" in command
    assert "/etc/ssl/certs" in command


def test_detect_sandbox_runtime_rejects_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("skills.sandbox.sys.platform", "linux")
    monkeypatch.setattr("skills.sandbox.os.geteuid", lambda: 0, raising=False)

    with pytest.raises(SandboxUnavailableError, match="unprivileged service account"):
        detect_sandbox_runtime()


def test_detect_sandbox_runtime_rejects_missing_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("skills.sandbox.sys.platform", "linux")
    monkeypatch.setattr("skills.sandbox.os.geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr("skills.sandbox.shutil.which", lambda _name: None)

    with pytest.raises(SandboxUnavailableError, match="bwrap, prlimit"):
        detect_sandbox_runtime()


def test_configured_executable_tools_require_successful_startup_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import tools as app_tools

    monkeypatch.setattr(
        app_tools,
        "scan_skills",
        lambda _path: {"executable": SimpleNamespace(tools=[object()])},
    )
    monkeypatch.setattr(
        app_tools,
        "validate_sandbox_runtime",
        lambda _limits: (_ for _ in ()).throw(SandboxUnavailableError("probe denied")),
    )

    with pytest.raises(SandboxUnavailableError, match="probe denied"):
        app_tools._validate_executable_skill_sandbox(tmp_path, ScriptSandboxLimits())


def test_instruction_only_store_does_not_require_linux_sandbox(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import tools as app_tools

    monkeypatch.setattr(
        app_tools,
        "scan_skills",
        lambda _path: {"instructions": SimpleNamespace(tools=[])},
    )
    monkeypatch.setattr(
        app_tools,
        "validate_sandbox_runtime",
        lambda _limits: (_ for _ in ()).throw(AssertionError("should not probe")),
    )

    assert app_tools._validate_executable_skill_sandbox(tmp_path, ScriptSandboxLimits()) is False


def test_runtime_mounts_cover_every_symlink_hop(tmp_path: Path) -> None:
    """uv reaches its interpreter through a version-alias directory symlink;
    mounting only the resolved target leaves that hop missing inside the jail
    and execvp fails with ENOENT despite every visible component existing."""
    store = tmp_path / "store"
    real_bin = store / "cpython-1.2.3" / "bin"
    real_bin.mkdir(parents=True)
    (real_bin / "python1.2").write_text("", encoding="utf-8")
    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    try:
        (store / "cpython-1.2").symlink_to(store / "cpython-1.2.3", target_is_directory=True)
        (venv_bin / "python").symlink_to(store / "cpython-1.2" / "bin" / "python1.2")
        (venv_bin / "python3").symlink_to("python")
    except OSError:
        pytest.skip("symlink creation unavailable on this host")

    mounts = sandbox_module._runtime_mounts((venv_bin / "python3").absolute())

    alias_bin = store / "cpython-1.2" / "bin"
    # The stdlib lives next to the executable's path, so the alias tree must
    # be covered as a whole, not just its bin directory.
    alias_lib = store / "cpython-1.2" / "lib"
    for needed in (venv_bin, alias_bin, alias_lib, real_bin):
        assert any(needed == mount or needed.is_relative_to(mount) for mount in mounts), (
            f"{needed} is not covered by {mounts}"
        )


def test_runtime_mounts_refuse_an_interpreter_directly_in_the_home(
    monkeypatch, tmp_path: Path
) -> None:
    """An interpreter parented by the service home would ro-bind the whole
    home - credentials included - into every jail."""
    monkeypatch.setattr(sandbox_module.Path, "home", classmethod(lambda cls: tmp_path))
    interpreter = tmp_path / "python"
    interpreter.write_text("", encoding="utf-8")

    with pytest.raises(SandboxUnavailableError, match="service home"):
        sandbox_module._runtime_mounts(interpreter.absolute())


def test_runtime_mounts_canonicalize_a_symlinked_service_home(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    real_home = tmp_path / "real" / "service-home"
    real_home.mkdir(parents=True)
    logical_home = tmp_path / "logical-home"
    runtime_alias = tmp_path / "runtime-alias"
    executable = tmp_path / "python-runtime" / "bin" / "python"
    executable.parent.mkdir(parents=True)
    executable.write_text("", encoding="utf-8")
    try:
        logical_home.symlink_to(real_home, target_is_directory=True)
        runtime_alias.symlink_to(real_home.parent, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation unavailable on this host")
    monkeypatch.setattr(
        sandbox_module.Path,
        "home",
        classmethod(lambda cls: logical_home),
    )
    monkeypatch.setattr(sandbox_module.sys, "executable", str(executable))
    monkeypatch.setattr(sandbox_module.sys, "prefix", str(runtime_alias))
    monkeypatch.setattr(sandbox_module.sys, "base_prefix", str(executable.parent))

    with pytest.raises(SandboxUnavailableError, match="service home"):
        sandbox_module._runtime_mounts(executable)


def test_runtime_mounts_fail_closed_when_home_cannot_be_resolved(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    missing_home = tmp_path / "missing-home"
    interpreter = tmp_path / "runtime" / "python"
    interpreter.parent.mkdir()
    interpreter.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        sandbox_module.Path,
        "home",
        classmethod(lambda cls: missing_home),
    )

    with pytest.raises(SandboxUnavailableError, match="Could not resolve the service home"):
        sandbox_module._runtime_mounts(interpreter)


def test_runtime_mounts_fail_closed_on_dotdot_across_an_alias(tmp_path: Path) -> None:
    """A relative .. hop that crosses an unresolved directory symlink is
    normalized the way the jail will see it; when that path does not exist on
    the host the layout is refused by name rather than mounted wrongly."""
    srv = tmp_path / "srv"
    (srv / "python-v2").mkdir(parents=True)
    (srv / "python-v2" / "python").write_text("", encoding="utf-8")
    (srv / "python-v1").mkdir()
    aliases = tmp_path / "opt" / "aliases"
    aliases.mkdir(parents=True)
    try:
        (aliases / "current").symlink_to(srv / "python-v1", target_is_directory=True)
        (srv / "python-v1" / "python").symlink_to(Path("..") / "python-v2" / "python")
    except OSError:
        pytest.skip("symlink creation unavailable on this host")

    with pytest.raises(SandboxUnavailableError, match="does not exist"):
        sandbox_module._runtime_mounts((aliases / "current" / "python").absolute())


def test_validate_probe_applies_every_configured_limit(monkeypatch, tmp_path: Path) -> None:
    """The startup probe must exercise the same ceilings real invocations use,
    tmpfs included, or a host that rejects one passes validation and every
    skill call fails."""
    recorded: dict = {}

    class _Done:
        returncode = 0
        stderr = b""

    def capture(command, **kwargs):
        recorded["command"] = command
        return _Done()

    monkeypatch.setattr(sandbox_module.subprocess, "run", capture)
    monkeypatch.setattr(
        sandbox_module,
        "detect_sandbox_runtime",
        lambda: SandboxRuntime(bwrap="bwrap", prlimit="prlimit"),
    )
    monkeypatch.setattr(sandbox_module.shutil, "which", lambda name: "/usr/bin/true")
    monkeypatch.setattr(sandbox_module, "_covered_by_base_mount", lambda path: True)

    limits = ScriptSandboxLimits(
        memory_bytes=111,
        cpu_seconds=22,
        file_size_bytes=333,
        open_files=44,
        processes=55,
        tmpfs_bytes=789,
    )
    validate_sandbox_runtime(limits)

    command = recorded["command"]
    for expected in ("--as=111", "--cpu=22", "--fsize=333", "--nofile=44", "--nproc=55"):
        assert expected in command
    size_index = command.index("--size")
    assert command[size_index + 1] == "789"
    assert command[size_index + 2 : size_index + 4] == ["--tmpfs", "/tmp"]


def _live_linux_sandbox_unavailable() -> bool:
    if sys.platform != "linux" or not shutil.which("bwrap") or not shutil.which("prlimit"):
        return True
    try:
        # The configured ceilings, not the dataclass defaults: a gate that
        # passes at limits real invocations never use certifies nothing. This
        # runs at collection time, so it deliberately reads the live profile
        # (allowlisted in test_settings_isolation.py).
        validate_sandbox_runtime(build_script_sandbox_limits(Settings()))  # type: ignore[call-arg]
    except SandboxUnavailableError:
        return True
    return False


_requires_linux_sandbox = pytest.mark.skipif(
    _live_linux_sandbox_unavailable(),
    reason="requires a working production Linux Bubblewrap sandbox",
)


@pytest.mark.asyncio
@_requires_linux_sandbox
async def test_linux_sandbox_hides_host_and_denies_network(tmp_path: Path) -> None:
    skill = tmp_path / "skill"
    workspace = tmp_path / "workspace"
    other_workspace = tmp_path / "other-user" / "private"
    script = skill / "scripts" / "probe.py"
    script.parent.mkdir(parents=True)
    workspace.mkdir()
    other_workspace.parent.mkdir()
    other_workspace.write_text("private", encoding="utf-8")
    script.write_text(
        textwrap.dedent(
            """
            import json
            import os
            import pathlib
            import socket
            import sys
            import yaml

            args = json.loads(sys.stdin.read())
            network = socket.socket()
            network.settimeout(0.2)
            network_errno = network.connect_ex(("1.1.1.1", 53))
            try:
                pathlib.Path("/skill/changed.txt").write_text("changed")
                skill_writable = True
            except OSError:
                skill_writable = False
            pathlib.Path(os.environ["WORKSPACE_DIR"], "result.txt").write_text("ok")
            print(json.dumps({
                "hostname_visible": pathlib.Path("/etc/hostname").exists(),
                "other_workspace_visible": pathlib.Path(args["other_workspace"]).exists(),
                "parent_proc_visible": pathlib.Path(
                    f"/proc/{args['parent_pid']}/environ"
                ).exists(),
                "network_errno": network_errno,
                "skill_writable": skill_writable,
                "venv_dependency": yaml.safe_load("ok: true")["ok"],
            }))
            """
        ).strip(),
        encoding="utf-8",
    )

    result = await run_script(
        script_path="scripts/probe.py",
        skill_dir=skill,
        arguments={"other_workspace": str(other_workspace), "parent_pid": os.getpid()},
        secrets={},
        workspace_dir=str(workspace),
        timeout=10,
    )

    assert result.return_code == 0, result.stderr
    probe = json.loads(result.stdout)
    assert probe["hostname_visible"] is False
    assert probe["other_workspace_visible"] is False
    assert probe["parent_proc_visible"] is False
    assert probe["network_errno"] != 0
    assert probe["skill_writable"] is False
    assert probe["venv_dependency"] is True
    assert (workspace / "result.txt").read_text(encoding="utf-8") == "ok"


@pytest.mark.asyncio
@_requires_linux_sandbox
async def test_linux_sandbox_explicit_network_opt_in_mounts_resolver_data(
    tmp_path: Path,
) -> None:
    skill = tmp_path / "skill"
    script = skill / "scripts" / "resolve.py"
    script.parent.mkdir(parents=True)
    script.write_text(
        "import json, pathlib, socket\n"
        "print(json.dumps({"
        "'resolver': pathlib.Path('/etc/resolv.conf').is_file(), "
        "'localhost': bool(socket.getaddrinfo('localhost', 80))}))\n",
        encoding="utf-8",
    )

    result = await run_script(
        script_path="scripts/resolve.py",
        skill_dir=skill,
        arguments={},
        secrets={},
        allow_network=True,
        timeout=10,
    )

    assert result.return_code == 0, result.stderr
    assert json.loads(result.stdout) == {"resolver": True, "localhost": True}


@pytest.mark.asyncio
@_requires_linux_sandbox
async def test_linux_sandbox_reaps_detached_descendants(tmp_path: Path) -> None:
    skill = tmp_path / "skill"
    workspace = tmp_path / "workspace"
    script = skill / "scripts" / "detach.py"
    script.parent.mkdir(parents=True)
    workspace.mkdir()
    child_code = (
        "import pathlib,time; time.sleep(0.5); "
        "pathlib.Path('/workspace/child-survived').write_text('bad')"
    )
    script.write_text(
        textwrap.dedent(
            f"""
            import subprocess
            import sys

            subprocess.Popen(
                [sys.executable, "-c", {child_code!r}],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            print("parent done")
            """
        ).strip(),
        encoding="utf-8",
    )

    result = await run_script(
        script_path="scripts/detach.py",
        skill_dir=skill,
        arguments={},
        secrets={},
        workspace_dir=str(workspace),
        timeout=10,
    )

    assert result.return_code == 0, result.stderr
    await asyncio.sleep(1)
    assert not (workspace / "child-survived").exists()
