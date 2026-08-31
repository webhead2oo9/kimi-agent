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


def _live_linux_sandbox_unavailable() -> bool:
    if sys.platform != "linux" or not shutil.which("bwrap") or not shutil.which("prlimit"):
        return True
    try:
        # The configured ceilings, not the dataclass defaults: a gate that
        # passes at limits real invocations never use certifies nothing.
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
