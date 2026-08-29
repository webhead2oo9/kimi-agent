from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import app.tools as app_tools
import web_browser.service as browser_service
from config.settings import Settings
from sandbox.netns_lease import NetnsLease
from tools.registry import ToolRegistry
from tools.workspace.common import UserLocks
from trust.tiers import TrustTier
from web_browser.service import (
    BrowserNetworkMode,
    BrowserService,
    BrowserServiceConfig,
    BrowserServiceError,
    BrowserWorkerTeardownError,
    _SubprocessBrowserWorker,
    _runtime_env,
    _stop_unit,
    _unit_state,
    build_browser_worker_command,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BETTERWRIGHT_INSTALLER = PROJECT_ROOT / "deploy/betterwright/install.sh"


def _run_installer_preflight(tmp_path: Path, target: str) -> subprocess.CompletedProcess[str]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    commands = {
        "id": "#!/bin/sh\necho 0\n",
        "node": "#!/bin/sh\nexit 1\n",
        "npm": "#!/bin/sh\nexit 0\n",
    }
    for name, source in commands.items():
        path = fake_bin / name
        path.write_text(source, encoding="utf-8")
        path.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
        "NODE_BIN": str(fake_bin / "node"),
        "NPM_BIN": str(fake_bin / "npm"),
    }
    return subprocess.run(
        ["sh", str(BETTERWRIGHT_INSTALLER), target],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def _config(
    tmp_path: Path,
    *,
    network_mode: BrowserNetworkMode = "host",
    max_profile_bytes: int = 1024,
) -> BrowserServiceConfig:
    return BrowserServiceConfig(
        runtime_dir=tmp_path / "runtime",
        profiles_dir=tmp_path / "profiles",
        bridge_script=tmp_path / "bridge.mjs",
        network_mode=network_mode,
        netns_helper_bin="/fixed/netns-helper",
        netns_resolv_conf="/fixed/resolv.conf",
        max_profile_bytes=max_profile_bytes,
        idle_ttl_seconds=60,
        require_root_owned_runtime=False,
    )


def test_worker_command_has_filesystem_and_network_boundary(tmp_path: Path) -> None:
    config = _config(tmp_path)
    command = build_browser_worker_command(
        config,
        tmp_path / "profile",
        unit_name="browser-test",
        seccomp_fd=9,
    )

    assert command[:2] == ["systemd-run", "--user"]
    assert "--unshare-all" in command
    assert "--share-net" in command
    assert command[command.index("--seccomp") + 1] == "9"
    assert command[command.index("--setenv") + 2] == "/usr/bin"
    assert command[-2:] == ["/runtime/node", "/bridge.mjs"]


def test_bridge_and_installer_lock_runtime_contract() -> None:
    bridge = (PROJECT_ROOT / "web_browser/bridge.mjs").read_text(encoding="utf-8")
    visual_bridge = (PROJECT_ROOT / "web_browser/visual_bridge.mjs").read_text(encoding="utf-8")
    visual_service = (PROJECT_ROOT / "web_browser/visual_service.py").read_text(encoding="utf-8")
    installer = (PROJECT_ROOT / "deploy/betterwright/install.sh").read_text(encoding="utf-8")
    package = json.loads(
        (PROJECT_ROOT / "deploy/betterwright/package.json").read_text(encoding="utf-8")
    )
    lock = json.loads(
        (PROJECT_ROOT / "deploy/betterwright/package-lock.json").read_text(encoding="utf-8")
    )
    tool = (PROJECT_ROOT / "tools/browser.py").read_text(encoding="utf-8")
    skill = (PROJECT_ROOT / "skills/builtin/browser/SKILL.md").read_text(encoding="utf-8")
    api = (PROJECT_ROOT / "skills/builtin/browser/reference/api.md").read_text(encoding="utf-8")

    dependencies = package["dependencies"]
    assert set(dependencies) == {"betterwright", "mermaid"}
    assert all(
        re.fullmatch(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?", version)
        for version in dependencies.values()
    )
    betterwright_version = dependencies["betterwright"]
    mermaid_version = dependencies["mermaid"]

    assert f"VERSION={betterwright_version}" in installer
    assert f"MERMAID_VERSION={mermaid_version}" in installer
    assert f'_MERMAID_VERSION = "{mermaid_version}"' in visual_service
    assert '"$NPM_BIN" ci' in installer
    assert '"$NPM_BIN" install' not in installer
    assert "node_modules/mermaid/dist/mermaid.min.js" in installer
    assert lock["packages"][""]["dependencies"] == dependencies
    assert lock["packages"]["node_modules/betterwright"]["version"] == betterwright_version
    assert lock["packages"]["node_modules/mermaid"]["version"] == mermaid_version
    assert 'HOME="$STAGING_DIR" BETTERWRIGHT_HOME="$STAGING_DIR"' in installer
    assert 'mv -- "$STAGING_DIR" "$RUNTIME_DIR"' in installer
    assert "allowPrivateNetwork: false" in bridge
    assert "allowLoopback: false" in bridge
    assert "vault: false" in bridge
    assert "visual renderer is offline" in visual_bridge
    assert "request.code" not in visual_bridge
    assert "page.goto" not in visual_bridge
    assert "new DOMParser()" in visual_bridge
    assert ".innerHTML" not in visual_bridge
    assert "document.importNode(svg, true)" in visual_bridge
    assert "page.locator('#visual').setAttribute" not in visual_bridge
    assert "document.querySelector('#visual').setAttribute('aria-label', altText)" in visual_bridge
    assert "await fs.lstat(artifact)" in visual_bridge
    assert "await fs.realpath(artifact)" in visual_bridge
    assert 'resolvedArtifact.startsWith("/work/artifacts/")' in visual_bridge
    assert visual_bridge.count(r"/expression\\s*\\(/i") == 2
    assert visual_bridge.count(r"/url\\s*\\(\\s*(?!#)/i") == 2
    assert "const placeLeft=px>right-160" in visual_bridge
    assert "px+(placeLeft?-11:11)" in visual_bridge
    assert "'text-anchor':placeLeft?'end':'start'" in visual_bridge
    assert "if(data.chart_type === 'bar')" in visual_bridge
    assert "fill:\\`url(#hatch-\\${si})\\`" in visual_bridge
    assert "series.name.slice(0,21)+'...'" in visual_bridge
    assert "data.x_scale" in visual_bridge
    assert "data.y_scale" in visual_bridge
    assert "data.overlap_mode==='count'" in visual_bridge
    assert "JSON.stringify([point.x,point.y])" in visual_bridge
    assert "…" not in visual_bridge
    assert "�" not in visual_bridge
    assert 'downloadPolicy: "deny"' in bridge
    assert "there is no " in tool and "`browser` global" in tool
    assert "There is no `browser` global" in skill
    assert "There is no `browser`" in api
    assert "openPage(url)" in tool
    assert "context.newPage()" not in tool
    assert "Do not call `browser.newPage()`,\n  `context.newPage()`" in skill
    assert "CloakBrowser" not in installer
    assert "FONTCONFIG_FILE" not in installer


def test_betterwright_installer_shell_syntax() -> None:
    subprocess.run(["sh", "-n", str(BETTERWRIGHT_INSTALLER)], check=True)


def test_betterwright_installer_accepts_only_aliases_of_reviewed_target(tmp_path: Path) -> None:
    lexical = _run_installer_preflight(tmp_path, "/opt/kimi/../kimi/betterwright")
    alias = tmp_path / "runtime-alias"
    alias.symlink_to("/opt/kimi/betterwright", target_is_directory=True)
    symlinked = _run_installer_preflight(tmp_path, str(alias))

    for result in (lexical, symlinked):
        assert result.returncode == 2
        assert "BetterWright requires Node >=22.18.0" in result.stderr
        assert "refusing runtime directory" not in result.stderr


def test_betterwright_installer_rejects_other_target_before_removal(tmp_path: Path) -> None:
    victim = tmp_path / "victim"
    victim.mkdir()
    marker = victim / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    alias = tmp_path / "runtime-alias"
    alias.symlink_to(victim, target_is_directory=True)

    result = _run_installer_preflight(tmp_path, str(alias))

    assert result.returncode == 2
    assert "refusing runtime directory outside /opt/kimi/betterwright" in result.stderr
    assert marker.read_text(encoding="utf-8") == "keep"


def test_visual_math_node_suite() -> None:
    result = subprocess.run(
        ["node", "--test", str(PROJECT_ROOT / "tests/js/visual_math.test.mjs")],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_netns_command_uses_only_fixed_helper_and_resolver(tmp_path: Path) -> None:
    config = _config(tmp_path, network_mode="netns")
    command = build_browser_worker_command(
        config,
        tmp_path / "profile",
        unit_name="browser-test",
        seccomp_fd=3,
        bpf_path=tmp_path / "policy.bpf",
    )

    assert "/fixed/netns-helper" in command
    assert command[
        command.index("/fixed/resolv.conf") - 1 : command.index("/fixed/resolv.conf") + 2
    ] == ["--ro-bind", "/fixed/resolv.conf", "/etc/resolv.conf"]
    assert not any("host" in part for part in command)


def test_runtime_env_derives_user_manager_address_when_session_env_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_os = SimpleNamespace(name="posix", environ={}, getuid=lambda: 1234)
    monkeypatch.setattr(browser_service, "os", fake_os)

    assert _runtime_env() == {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "XDG_RUNTIME_DIR": "/run/user/1234",
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1234/bus",
    }


class _ReadStream:
    def __init__(self, *lines: bytes, before_read: Any | None = None) -> None:
        self._lines = iter((*lines, b""))
        self._before_read = before_read

    async def readline(self) -> bytes:
        if self._before_read is not None:
            self._before_read()
        return next(self._lines)


class _FakeProcess:
    def __init__(self, *, stdout: _ReadStream | None = None, returncode: int | None = 0) -> None:
        self.stdin: Any | None = None
        self.stdout = stdout
        self.stderr = _ReadStream()
        self.returncode = returncode

    async def communicate(self) -> tuple[bytes, bytes]:
        output = bytearray()
        if self.stdout is not None:
            while line := await self.stdout.readline():
                output.extend(line)
        return bytes(output), b""

    async def wait(self) -> int:
        return self.returncode or 0


@pytest.mark.asyncio
async def test_worker_close_tolerates_process_exit_before_terminate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Stdin:
        def close(self) -> None:
            pass

        async def wait_closed(self) -> None:
            pass

    class _RacingProcess(_FakeProcess):
        def __init__(self) -> None:
            super().__init__(returncode=None)
            self.stderr = _ReadStream()
            self.stdin = _Stdin()
            self.terminate_calls = 0
            self.kill_calls = 0

        def terminate(self) -> None:
            self.terminate_calls += 1
            raise ProcessLookupError

        def kill(self) -> None:
            self.kill_calls += 1

    process = _RacingProcess()
    real_wait_for = browser_service.asyncio.wait_for
    wait_calls = 0

    async def timeout_graceful_wait(awaitable: Any, timeout: float) -> Any:
        nonlocal wait_calls
        wait_calls += 1
        if wait_calls == 1:
            awaitable.close()
            raise TimeoutError
        return await real_wait_for(awaitable, timeout)

    async def stop_unit(config: BrowserServiceConfig, unit_name: str) -> None:
        del config, unit_name

    monkeypatch.setattr(browser_service.asyncio, "wait_for", timeout_graceful_wait)
    monkeypatch.setattr(browser_service, "_stop_unit", stop_unit)
    worker = _SubprocessBrowserWorker(
        _config(tmp_path),
        tmp_path / "profile",
        process,  # type: ignore[arg-type]
        "browser-race",
    )

    await worker.close()

    assert process.terminate_calls == 1
    assert process.kill_calls == 0


@pytest.mark.asyncio
async def test_netns_seccomp_file_survives_until_worker_is_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bpf_path = tmp_path / "worker.bpf"

    def write_policy(unit_name: str) -> Path:
        del unit_name
        bpf_path.write_bytes(b"policy")
        return bpf_path

    def require_policy() -> None:
        assert bpf_path.is_file()

    process = _FakeProcess(
        stdout=_ReadStream(b"__BW_READY__{}\n", before_read=require_policy),
        returncode=None,
    )

    async def create_process(*command: str, **kwargs: Any) -> _FakeProcess:
        del command, kwargs
        return process

    monkeypatch.setattr(browser_service, "_write_seccomp_file", write_policy)
    monkeypatch.setattr(browser_service.asyncio, "create_subprocess_exec", create_process)

    worker = await _SubprocessBrowserWorker.create(
        _config(tmp_path, network_mode="netns"), "owner", tmp_path / "profile"
    )

    assert worker.process is process
    assert not bpf_path.exists()


@pytest.mark.asyncio
async def test_unit_state_distinguishes_confirmed_missing_unit_from_query_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    responses = iter(
        (
            _FakeProcess(stdout=_ReadStream(b"unknown\n"), returncode=4),
            _FakeProcess(stdout=_ReadStream(), returncode=1),
        )
    )

    async def create_process(*command: str, **kwargs: Any) -> _FakeProcess:
        del command, kwargs
        return next(responses)

    monkeypatch.setattr(browser_service.asyncio, "create_subprocess_exec", create_process)

    assert await _unit_state(_config(tmp_path), "missing") == "unknown"
    assert await _unit_state(_config(tmp_path), "unknown") is None


@pytest.mark.asyncio
async def test_stop_unit_fails_closed_when_unit_state_cannot_be_queried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def create_process(*command: str, **kwargs: Any) -> _FakeProcess:
        del command, kwargs
        return _FakeProcess()

    async def unknown_state(config: BrowserServiceConfig, unit_name: str) -> None:
        del config, unit_name

    async def no_sleep(delay: float) -> None:
        del delay

    monkeypatch.setattr(browser_service.asyncio, "create_subprocess_exec", create_process)
    monkeypatch.setattr(browser_service, "_unit_state", unknown_state)
    monkeypatch.setattr(browser_service.asyncio, "sleep", no_sleep)

    with pytest.raises(BrowserWorkerTeardownError, match="could not be confirmed"):
        await _stop_unit(_config(tmp_path), "unknown")


class _Worker:
    def __init__(self, home: Path) -> None:
        self.home = home
        self.alive = True
        self.closed = False

    async def call(self, *, code: str, session: str) -> dict[str, Any]:
        return {"ok": True, "result": f"{session}:{code}"}

    async def close(self) -> None:
        self.alive = False
        self.closed = True


@pytest.mark.asyncio
async def test_service_switches_workers_without_sharing_profiles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workers: list[_Worker] = []

    async def factory(config: BrowserServiceConfig, owner: str, home: Path) -> _Worker:
        del config, owner
        worker = _Worker(home)
        workers.append(worker)
        return worker

    service = BrowserService(_config(tmp_path), worker_factory=factory)
    monkeypatch.setattr(service, "availability_error", lambda: None)

    await service.acquire_turn("user-a", "turn-a")
    result = await service.run(owner_id="user-a", turn_id="turn-a", session="root", code="1")
    await service.release_turn("user-a", "turn-a")
    await service.acquire_turn("user-b", "turn-b")
    await service.run(owner_id="user-b", turn_id="turn-b", session="root", code="2")

    assert result["ok"] is True
    assert workers[0].closed is True
    assert workers[0].home != workers[1].home
    assert "user-a" not in workers[0].home.name
    await service.release_turn("user-b", "turn-b")
    await service.close()


@pytest.mark.asyncio
async def test_profile_quota_resets_oversized_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def factory(config: BrowserServiceConfig, owner: str, home: Path) -> _Worker:
        del config, owner
        (home / "large").write_bytes(b"x" * 32)
        return _Worker(home)

    service = BrowserService(_config(tmp_path, max_profile_bytes=16), worker_factory=factory)
    monkeypatch.setattr(service, "availability_error", lambda: None)
    await service.acquire_turn("42", "turn")

    with pytest.raises(BrowserServiceError, match="storage limit"):
        await service.run(owner_id="42", turn_id="turn", session="root", code="1")

    assert not service.profile_home("42").exists()
    await service.release_turn("42", "turn")
    await service.close()


@pytest.mark.asyncio
async def test_profile_sweep_and_privacy_deletion(tmp_path: Path) -> None:
    service = BrowserService(_config(tmp_path))
    stale = service.profile_home("stale")
    stale.mkdir(parents=True)
    marker = stale / ".last_used"
    marker.touch()
    old = time.time() - service.config.profile_ttl_seconds - 5
    os.utime(marker, (old, old))
    current = service.profile_home("current")
    current.mkdir(parents=True)
    (current / ".last_used").touch()

    assert await service.sweep_expired() == 1
    assert not stale.exists()
    assert await service.delete_user_data("current") == 1
    assert not current.exists()


def test_app_registers_browser_only_after_successful_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        browser_enabled=True,
        browser_runtime_dir=str(tmp_path / "runtime"),
        browser_profiles_dir=str(tmp_path / "profiles"),
    )
    registry = ToolRegistry()
    monkeypatch.setattr(BrowserService, "availability_error", lambda self: None)
    monkeypatch.setattr(app_tools, "sandbox_available", lambda config: True)

    app_tools._register_browser(
        settings,
        registry,
        app_tools.WorkspaceManager(tmp_path / "workspace"),
        workspace_locks=UserLocks(),
        netns_lease=NetnsLease(),
    )

    names = {schema["name"] for schema in registry.get_tool_schemas(TrustTier.MEMBER, set(), "42")}
    assert "browser" in names
    assert not registry.is_registered("render_chart")
    assert not registry.is_registered("render_diagram")


def test_app_registers_visual_only_after_browser_and_visual_runtime_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        browser_enabled=True,
        browser_runtime_dir=str(tmp_path / "runtime"),
        browser_profiles_dir=str(tmp_path / "profiles"),
    )
    registry = ToolRegistry()
    monkeypatch.setattr(BrowserService, "availability_error", lambda self: None)
    monkeypatch.setattr(app_tools.VisualService, "availability_error", lambda self: None)
    monkeypatch.setattr(app_tools, "sandbox_available", lambda config: True)

    app_tools._register_browser(
        settings,
        registry,
        app_tools.WorkspaceManager(tmp_path / "workspace"),
        workspace_locks=UserLocks(),
        netns_lease=NetnsLease(),
    )

    assert registry.is_registered("browser")
    chart = registry.get_searchable_entry("render_chart", TrustTier.MEMBER)
    diagram = registry.get_searchable_entry("render_diagram", TrustTier.MEMBER)
    assert chart is not None
    assert diagram is not None
    assert chart.category == diagram.category == "Visuals"
