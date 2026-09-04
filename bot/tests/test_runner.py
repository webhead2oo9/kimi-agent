import asyncio
import json
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

from skills.runner import _build_env, _collect_output_files, validate_script_path
from tests.skill_runner_helpers import run_script_with_direct_test_command

# These tests spawn real /bin/sh children and assert POSIX process-group
# kill/cleanup semantics; neither exists on Windows.
_requires_posix_processes = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX shell children / process groups"
)


def test_build_env_ignores_reserved_names_in_secrets() -> None:
    # A declared secret must not be able to clobber PATH / LD_PRELOAD etc. and
    # hijack the reviewed script's interpreter environment.
    env = _build_env(
        {"API_KEY": "secret", "PATH": "/evil", "LD_PRELOAD": "/evil.so"},
        workspace_dir="/tmp/ws",
    )
    assert env["API_KEY"] == "secret"
    assert env["PATH"] != "/evil"
    assert "LD_PRELOAD" not in env
    assert env["WORKSPACE_DIR"] == "/tmp/ws"


def test_validate_script_path_valid() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        skill_dir = Path(tmp)
        script = skill_dir / "scripts" / "run.py"
        script.parent.mkdir()
        script.touch()
        result = validate_script_path("scripts/run.py", skill_dir)
        assert result == script.resolve()


def test_validate_script_path_traversal() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        skill_dir = Path(tmp) / "skill"
        skill_dir.mkdir()
        with pytest.raises(ValueError, match="escapes skill directory"):
            validate_script_path("../../etc/passwd", skill_dir)


def test_validate_script_path_missing() -> None:
    with tempfile.TemporaryDirectory() as tmp, pytest.raises(FileNotFoundError):
        validate_script_path("scripts/nope.py", Path(tmp))


@pytest.mark.asyncio
async def test_run_script_python() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        skill_dir = Path(tmp)
        scripts = skill_dir / "scripts"
        scripts.mkdir()
        script = scripts / "echo.py"
        script.write_text(
            "import json, sys\n"
            "args = json.loads(sys.stdin.read())\n"
            "print(json.dumps({'received': args}))\n",
            encoding="utf-8",
        )

        result = await run_script_with_direct_test_command(
            script_path="scripts/echo.py",
            skill_dir=skill_dir,
            arguments={"query": "hello"},
            secrets={},
            timeout=10,
        )
        parsed = json.loads(result.stdout)
        assert parsed["received"]["query"] == "hello"
        assert result.return_code == 0


@pytest.mark.asyncio
@_requires_posix_processes
async def test_run_script_allows_child_that_exits_without_reading_stdin() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        skill_dir = Path(tmp)
        scripts = skill_dir / "scripts"
        scripts.mkdir()
        script = scripts / "quick.sh"
        script.write_text("printf '%s\\n' '{\"ok\":true}'\n", encoding="utf-8")

        result = await run_script_with_direct_test_command(
            script_path="scripts/quick.sh",
            skill_dir=skill_dir,
            arguments={"payload": "x" * 2_000_000},
            secrets={},
            timeout=10,
        )

        assert json.loads(result.stdout) == {"ok": True}
        assert result.return_code == 0


@pytest.mark.asyncio
async def test_run_script_timeout() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        skill_dir = Path(tmp)
        scripts = skill_dir / "scripts"
        scripts.mkdir()
        script = scripts / "hang.py"
        script.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")

        result = await run_script_with_direct_test_command(
            script_path="scripts/hang.py",
            skill_dir=skill_dir,
            arguments={},
            secrets={},
            timeout=1,
        )
        assert result.timed_out is True


@pytest.mark.asyncio
@_requires_posix_processes
async def test_run_script_timeout_kills_child_processes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        skill_dir = root / "skill"
        skill_dir.mkdir()
        scripts = skill_dir / "scripts"
        scripts.mkdir()
        marker = root / "child-survived.txt"
        child_code = (
            "import pathlib, time\n"
            "time.sleep(1.0)\n"
            f"pathlib.Path({str(marker)!r}).write_text('survived')\n"
        )
        script = scripts / "spawn_child.py"
        script.write_text(
            textwrap.dedent(
                f"""
                import subprocess
                import sys
                import time

                subprocess.Popen([sys.executable, "-c", {child_code!r}])
                time.sleep(60)
                """
            ).strip(),
            encoding="utf-8",
        )

        result = await run_script_with_direct_test_command(
            script_path="scripts/spawn_child.py",
            skill_dir=skill_dir,
            arguments={},
            secrets={},
            timeout=0.2,
        )
        assert result.timed_out is True

        await asyncio.sleep(1.5)
        assert not marker.exists()


@pytest.mark.asyncio
@_requires_posix_processes
async def test_run_script_success_cleans_up_child_processes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        skill_dir = root / "skill"
        skill_dir.mkdir()
        scripts = skill_dir / "scripts"
        scripts.mkdir()
        marker = root / "child-survived.txt"
        child_code = (
            "import pathlib, time\n"
            "time.sleep(1.0)\n"
            f"pathlib.Path({str(marker)!r}).write_text('survived')\n"
        )
        script = scripts / "spawn_child_success.py"
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
                )
                print("parent done")
                """
            ).strip(),
            encoding="utf-8",
        )

        result = await run_script_with_direct_test_command(
            script_path="scripts/spawn_child_success.py",
            skill_dir=skill_dir,
            arguments={},
            secrets={},
            timeout=10,
        )
        assert result.return_code == 0
        assert result.stdout == "parent done"

        await asyncio.sleep(1.5)
        assert not marker.exists()


@pytest.mark.asyncio
async def test_run_script_scrubs_secrets() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        skill_dir = Path(tmp)
        scripts = skill_dir / "scripts"
        scripts.mkdir()
        script = scripts / "leak.py"
        script.write_text("print('The secret is sk-supersecret123')\n", encoding="utf-8")

        result = await run_script_with_direct_test_command(
            script_path="scripts/leak.py",
            skill_dir=skill_dir,
            arguments={},
            secrets={"API_KEY": "sk-supersecret123"},
            timeout=10,
        )
        assert "sk-supersecret123" not in result.stdout
        assert "[REDACTED]" in result.stdout


@pytest.mark.asyncio
@_requires_posix_processes
async def test_run_script_scrubs_secret_after_multibyte_prefix_when_truncated() -> None:
    secret = "SUPERSECRETTOKEN123456789"
    with tempfile.TemporaryDirectory() as tmp:
        skill_dir = Path(tmp)
        scripts = skill_dir / "scripts"
        scripts.mkdir()
        script = scripts / "leak_after_unicode.py"
        payload = "中" * 11 + secret
        script.write_text(
            f"print({payload!r})\n",
            encoding="utf-8",
        )

        result = await run_script_with_direct_test_command(
            script_path="scripts/leak_after_unicode.py",
            skill_dir=skill_dir,
            arguments={},
            secrets={"API_KEY": secret},
            timeout=10,
            max_output_chars=30,
        )

        assert secret not in result.stdout
        assert "SUPERSECRET" not in result.stdout
        assert result.stdout == ("中" * 11) + "[REDACTED]"


@pytest.mark.asyncio
async def test_run_script_scrubs_stderr_on_failure() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        skill_dir = Path(tmp)
        scripts = skill_dir / "scripts"
        scripts.mkdir()
        script = scripts / "fail.py"
        script.write_text(
            "import sys\nprint('error with sk-secret99', file=sys.stderr)\nsys.exit(1)\n",
            encoding="utf-8",
        )

        result = await run_script_with_direct_test_command(
            script_path="scripts/fail.py",
            skill_dir=skill_dir,
            arguments={},
            secrets={"KEY": "sk-secret99"},
            timeout=10,
        )
        assert result.return_code == 1
        assert "sk-secret99" not in result.stderr
        assert "[REDACTED]" in result.stderr


@pytest.mark.asyncio
async def test_run_script_truncates_large_output() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        skill_dir = Path(tmp)
        scripts = skill_dir / "scripts"
        scripts.mkdir()
        script = scripts / "large.py"
        script.write_text("print('x' * 100)\n", encoding="utf-8")

        result = await run_script_with_direct_test_command(
            script_path="scripts/large.py",
            skill_dir=skill_dir,
            arguments={},
            secrets={},
            timeout=10,
            max_output_chars=20,
        )
        assert result.stdout.startswith("x" * 20)
        assert "[TRUNCATED" in result.stdout


@pytest.mark.asyncio
async def test_run_script_reports_output_files_from_workspace_job_dir() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        skill_dir = Path(tmp) / "skill"
        skill_dir.mkdir()
        scripts = skill_dir / "scripts"
        scripts.mkdir()
        workspace = Path(tmp) / "workspaces" / "user123" / "jobs" / "job-1"
        workspace.mkdir(parents=True)
        script = scripts / "write_file.py"
        script.write_text(
            "import os\n"
            "from pathlib import Path\n"
            "out = Path(os.environ['WORKSPACE_DIR']) / 'result.txt'\n"
            "out.write_text('hello')\n"
            "print('wrote file')\n",
            encoding="utf-8",
        )

        result = await run_script_with_direct_test_command(
            script_path="scripts/write_file.py",
            skill_dir=skill_dir,
            arguments={},
            secrets={},
            timeout=10,
            workspace_dir=str(workspace),
        )
        assert result.return_code == 0
        assert result.output_files == [str((workspace / "result.txt").resolve())]
        assert result.output_files_omitted == 0


@pytest.mark.asyncio
async def test_run_script_caps_output_files_from_workspace_job_dir() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        skill_dir = Path(tmp) / "skill"
        skill_dir.mkdir()
        scripts = skill_dir / "scripts"
        scripts.mkdir()
        workspace = Path(tmp) / "workspaces" / "user123" / "jobs" / "job-1"
        workspace.mkdir(parents=True)
        script = scripts / "write_many_files.py"
        script.write_text(
            "import os\n"
            "from pathlib import Path\n"
            "root = Path(os.environ['WORKSPACE_DIR'])\n"
            "for i in range(12):\n"
            "    (root / f'{i:02d}.txt').write_text(str(i))\n"
            "print('wrote files')\n",
            encoding="utf-8",
        )

        result = await run_script_with_direct_test_command(
            script_path="scripts/write_many_files.py",
            skill_dir=skill_dir,
            arguments={},
            secrets={},
            timeout=10,
            workspace_dir=str(workspace),
        )

        assert result.return_code == 0
        assert len(result.output_files) == 10
        assert result.output_files_omitted == 2


def test_collect_output_files_omits_files_over_byte_cap(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    small = workspace / "small.txt"
    small.write_text("ok", encoding="utf-8")
    large = workspace / "large.txt"
    large.write_text("too large", encoding="utf-8")

    files, omitted = _collect_output_files(
        str(workspace),
        max_files=10,
        max_file_bytes=4,
    )

    assert files == [str(small.resolve())]
    assert omitted == 1


def test_collect_output_files_stops_after_scan_cap(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for index in range(5):
        (workspace / f"{index}.txt").write_text("ok", encoding="utf-8")

    files, omitted = _collect_output_files(
        str(workspace),
        max_files=10,
        max_scan_entries=3,
    )

    assert len(files) == 3
    assert omitted == 1


@pytest.mark.asyncio
async def test_run_script_scrubs_secret_from_text_output_file_and_keeps_it() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        skill_dir = Path(tmp) / "skill"
        skill_dir.mkdir()
        scripts = skill_dir / "scripts"
        scripts.mkdir()
        workspace = Path(tmp) / "workspaces" / "user123" / "jobs" / "job-1"
        workspace.mkdir(parents=True)
        script = scripts / "write_secret.py"
        script.write_text(
            "import os\n"
            "from pathlib import Path\n"
            "out = Path(os.environ['WORKSPACE_DIR']) / 'report.txt'\n"
            "out.write_text(f\"key={os.environ['API_KEY']} done\")\n"
            "print('wrote file')\n",
            encoding="utf-8",
        )

        result = await run_script_with_direct_test_command(
            script_path="scripts/write_secret.py",
            skill_dir=skill_dir,
            arguments={},
            secrets={"API_KEY": "sk-secret"},
            timeout=10,
            workspace_dir=str(workspace),
        )
        assert result.return_code == 0
        assert len(result.output_files) == 1
        content = Path(result.output_files[0]).read_text(encoding="utf-8")
        assert "sk-secret" not in content
        assert "[REDACTED]" in content


@pytest.mark.asyncio
async def test_run_script_keeps_clean_output_file_when_secret_injected() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        skill_dir = Path(tmp) / "skill"
        skill_dir.mkdir()
        scripts = skill_dir / "scripts"
        scripts.mkdir()
        workspace = Path(tmp) / "workspaces" / "user123" / "jobs" / "job-1"
        workspace.mkdir(parents=True)
        script = scripts / "write_clean.py"
        script.write_text(
            "import os\n"
            "from pathlib import Path\n"
            "out = Path(os.environ['WORKSPACE_DIR']) / 'result.txt'\n"
            "out.write_text('no secrets here')\n",
            encoding="utf-8",
        )

        result = await run_script_with_direct_test_command(
            script_path="scripts/write_clean.py",
            skill_dir=skill_dir,
            arguments={},
            secrets={"API_KEY": "sk-secret"},
            timeout=10,
            workspace_dir=str(workspace),
        )
        assert result.return_code == 0
        assert len(result.output_files) == 1
        assert result.output_files_omitted == 0
        assert Path(result.output_files[0]).read_text(encoding="utf-8") == "no secrets here"


@pytest.mark.asyncio
async def test_run_script_drops_binary_output_file_containing_secret() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        skill_dir = Path(tmp) / "skill"
        skill_dir.mkdir()
        scripts = skill_dir / "scripts"
        scripts.mkdir()
        workspace = Path(tmp) / "workspaces" / "user123" / "jobs" / "job-1"
        workspace.mkdir(parents=True)
        script = scripts / "write_binary_secret.py"
        script.write_text(
            "import os\n"
            "from pathlib import Path\n"
            "out = Path(os.environ['WORKSPACE_DIR']) / 'image.bin'\n"
            "out.write_bytes(b'\\x89PNG\\x00' + os.environ['API_KEY'].encode() + b'\\xff\\xfe')\n",
            encoding="utf-8",
        )

        result = await run_script_with_direct_test_command(
            script_path="scripts/write_binary_secret.py",
            skill_dir=skill_dir,
            arguments={},
            secrets={"API_KEY": "sk-secret"},
            timeout=10,
            workspace_dir=str(workspace),
        )
        assert result.return_code == 0
        assert result.output_files == []
        assert result.output_files_omitted == 1
