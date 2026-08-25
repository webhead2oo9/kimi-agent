"""Sandboxed, per-user BetterWright worker lifecycle."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import shutil
import stat
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import uuid4

from sandbox.netns_lease import (
    NetnsLease,
    NetnsLeasePoisonedError,
    NetnsLeaseSafetyError,
)
from sandbox.seccomp import SeccompUnavailableError, open_bpf_fd, seccomp_bpf_bytes

log = logging.getLogger(__name__)

type BrowserNetworkMode = Literal["host", "netns"]

_READY_PREFIX = "__BW_READY__"
_RESULT_PREFIX = "__BW_RESULT__"
_NETNS_SECCOMP_FD = 3  # SD_LISTEN_FDS_START, where systemd's OpenFile= lands


class BrowserServiceError(RuntimeError):
    pass


class BrowserWorkerTeardownError(BrowserServiceError, NetnsLeaseSafetyError):
    pass


@dataclass(frozen=True)
class BrowserServiceConfig:
    runtime_dir: Path
    profiles_dir: Path
    bridge_script: Path
    network_mode: BrowserNetworkMode = "host"
    node_bin: str = "/runtime/node"
    bwrap_bin: str = "bwrap"
    prlimit_bin: str = "prlimit"
    systemd_run_bin: str = "systemd-run"
    systemctl_bin: str = "systemctl"
    sudo_bin: str = "sudo"
    netns_helper_bin: str = ""
    netns_resolv_conf: str = ""
    call_timeout_seconds: float = 30.0
    start_timeout_seconds: float = 20.0
    idle_ttl_seconds: float = 120.0
    worker_max_lifetime_seconds: int = 3600
    profile_ttl_seconds: int = 604800
    max_profile_bytes: int = 512 * 1024 * 1024
    max_total_memory_mb: int = 2048
    max_tasks: int = 256
    cpu_quota_percent: int = 200
    tmp_size_mb: int = 512
    max_fsize_mb: int = 128
    max_open_files: int = 1024
    timezone: str = "UTC"
    locale: str = "en-US"
    require_root_owned_runtime: bool = True

    @property
    def betterwright_module(self) -> Path:
        return self.runtime_dir / "betterwright-entry.mjs"

    @property
    def chromium_binary(self) -> Path:
        return self.runtime_dir / ".betterwright" / "chromium" / "linux-x64" / "betterchromium"


class BrowserWorker(Protocol):
    home: Path

    @property
    def alive(self) -> bool: ...

    async def call(self, *, code: str, session: str) -> dict[str, Any]: ...

    async def close(self) -> None: ...


def _runtime_env() -> dict[str, str]:
    env = {
        "PATH": os.environ.get(
            "PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        )
    }
    if os.name == "posix":
        runtime_dir = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        env["XDG_RUNTIME_DIR"] = runtime_dir
        env["DBUS_SESSION_BUS_ADDRESS"] = os.environ.get(
            "DBUS_SESSION_BUS_ADDRESS", f"unix:path={runtime_dir}/bus"
        )
    return env


def _browser_runtime_dir() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    root = Path(runtime) if runtime else Path("/tmp")
    path = root / "kimi-browser"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def _write_seccomp_file(unit_name: str) -> Path:
    path = _browser_runtime_dir() / f"{unit_name}.bpf"
    path.write_bytes(seccomp_bpf_bytes())
    path.chmod(0o600)
    return path


def _systemd_prefix(
    config: BrowserServiceConfig,
    *,
    unit_name: str,
    bpf_path: Path | None,
) -> list[str]:
    """Launch prefix for the worker: a scope in host mode, a service in netns mode.

    Host mode changes no privileges, so a transient user scope is enough. The
    netns path has to reach sudo, which cannot gain privilege inside the bot's
    NoNewPrivileges tree, so it uses a transient user service forked by the
    per-user systemd manager instead. The seccomp program cannot ride an
    inherited fd across that fork and the sudo boundary, so systemd opens the
    file via OpenFile= and hands it to the unit as fd 3. This mirrors
    ``sandbox/runner.py:_build_systemd_run_network_prefix``, which walks the same
    mechanism through in full. The browser helper is a separate binary from the
    code-exec one, so it needs its own sudoers drop-in granting
    closefrom_override; deploy/code-exec-netns holds the pattern.
    """

    limits = [
        "-p",
        f"TasksMax={config.max_tasks}",
        "-p",
        f"MemoryMax={config.max_total_memory_mb}M",
        "-p",
        "MemorySwapMax=0",
        "-p",
        f"CPUQuota={config.cpu_quota_percent}%",
    ]
    if config.network_mode == "host":
        return [
            config.systemd_run_bin,
            "--user",
            "--scope",
            "--quiet",
            "--collect",
            f"--unit={unit_name}",
            *limits,
            "--",
        ]
    if bpf_path is None:
        raise ValueError("netns browser launch requires a seccomp file")
    return [
        config.systemd_run_bin,
        "--user",
        "--pipe",
        "--wait",
        "--collect",
        "--quiet",
        f"--unit={unit_name}",
        *limits,
        "-p",
        f"RuntimeMaxSec={config.worker_max_lifetime_seconds + 5}",
        "-p",
        f"OpenFile={bpf_path}:seccomp:read-only",
        "--",
        config.sudo_bin,
        "-n",  # never prompt; fail closed if no NOPASSWD rule
        "-C",
        str(_NETNS_SECCOMP_FD + 1),  # raise sudo's closefrom bar to keep fd 3 open
        config.netns_helper_bin,
    ]


def build_browser_worker_command(
    config: BrowserServiceConfig,
    profile_home: Path,
    *,
    unit_name: str,
    seccomp_fd: int,
    bpf_path: Path | None = None,
) -> list[str]:
    """Compose the host/netns prefix and the shared browser sandbox."""

    runtime = str(config.runtime_dir.resolve())
    bridge = str(config.bridge_script.resolve())
    home = str(profile_home.resolve())
    resolver = config.netns_resolv_conf if config.network_mode == "netns" else "/etc/resolv.conf"
    return [
        *_systemd_prefix(config, unit_name=unit_name, bpf_path=bpf_path),
        config.prlimit_bin,
        "--core=0:0",
        f"--fsize={config.max_fsize_mb * 1024 * 1024}",
        f"--nofile={config.max_open_files}",
        "--",
        config.bwrap_bin,
        "--unshare-all",
        "--unshare-user",
        "--disable-userns",
        "--assert-userns-disabled",
        # Namespaced root makes BetterWright disable Chromium's redundant inner
        # sandbox; this grants no host privilege.
        "--uid",
        "0",
        "--gid",
        "0",
        "--seccomp",
        str(seccomp_fd),
        "--die-with-parent",
        "--new-session",
        "--clearenv",
        "--setenv",
        "PATH",
        "/usr/bin",
        "--setenv",
        "HOME",
        "/work",
        "--setenv",
        "BETTERWRIGHT_HOME",
        "/work",
        "--setenv",
        "BETTERWRIGHT_MODULE",
        "/runtime/betterwright-entry.mjs",
        "--setenv",
        "BETTERWRIGHT_CHROMIUM_PATH",
        "/runtime/.betterwright/chromium/linux-x64/betterchromium",
        "--setenv",
        "BETTERWRIGHT_TIMEZONE",
        config.timezone,
        "--setenv",
        "BETTERWRIGHT_LOCALE",
        config.locale,
        "--setenv",
        "NODE_NO_WARNINGS",
        "1",
        "--setenv",
        "LANG",
        "C.UTF-8",
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
        "/usr",
        "--symlink",
        "usr/lib",
        "/lib",
        "--symlink",
        "usr/lib64",
        "/lib64",
        "--symlink",
        "usr/bin",
        "/bin",
        "--share-net",
        "--ro-bind",
        resolver,
        "/etc/resolv.conf",
        "--ro-bind-try",
        "/etc/ssl",
        "/etc/ssl",
        "--ro-bind-try",
        "/etc/fonts",
        "/etc/fonts",
        "--ro-bind",
        runtime,
        "/runtime",
        "--ro-bind",
        bridge,
        "/bridge.mjs",
        "--bind",
        home,
        "/work",
        "--chdir",
        "/work",
        "--",
        config.node_bin,
        "/bridge.mjs",
    ]


async def _unit_state(config: BrowserServiceConfig, unit_name: str) -> str | None:
    process = await asyncio.create_subprocess_exec(
        config.systemctl_bin,
        "--user",
        "is-active",
        unit_name,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env=_runtime_env(),
    )
    stdout, _ = await process.communicate()
    state = stdout.decode(errors="replace").strip().lower()
    return state or None


async def _stop_unit(config: BrowserServiceConfig, unit_name: str) -> None:
    process = await asyncio.create_subprocess_exec(
        config.systemctl_bin,
        "--user",
        "stop",
        unit_name,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        env=_runtime_env(),
    )
    await process.wait()
    for _ in range(20):
        state = await _unit_state(config, unit_name)
        if state in {"inactive", "failed", "unknown"}:
            return
        await asyncio.sleep(0.05)
    raise BrowserWorkerTeardownError("Browser worker shutdown could not be confirmed.")


class _SubprocessBrowserWorker:
    def __init__(
        self,
        config: BrowserServiceConfig,
        home: Path,
        process: asyncio.subprocess.Process,
        unit_name: str,
    ) -> None:
        self.config = config
        self.home = home
        self.process = process
        self.unit_name = unit_name
        self._call_lock = asyncio.Lock()
        self._stderr: list[str] = []
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    @classmethod
    async def create(
        cls, config: BrowserServiceConfig, owner_id: str, home: Path
    ) -> _SubprocessBrowserWorker:
        del owner_id
        unit_name = f"kimi-browser-{uuid4().hex}"
        bpf_path: Path | None = None
        seccomp_fd = _NETNS_SECCOMP_FD
        pass_fds: tuple[int, ...] = ()
        host_fd: int | None = None
        try:
            try:
                if config.network_mode == "netns":
                    bpf_path = _write_seccomp_file(unit_name)
                else:
                    host_fd = open_bpf_fd()
                    seccomp_fd = host_fd
                    pass_fds = (host_fd,)
                command = build_browser_worker_command(
                    config,
                    home,
                    unit_name=unit_name,
                    seccomp_fd=seccomp_fd,
                    bpf_path=bpf_path,
                )
                process = await asyncio.create_subprocess_exec(
                    *command,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=_runtime_env(),
                    pass_fds=pass_fds,
                )
            finally:
                if host_fd is not None:
                    os.close(host_fd)
            worker = cls(config, home, process, unit_name)
            try:
                await worker._read_prefixed(_READY_PREFIX, timeout=config.start_timeout_seconds)
            except BaseException:
                await worker.close()
                raise
            return worker
        finally:
            if bpf_path is not None:
                with contextlib.suppress(OSError):
                    bpf_path.unlink()

    @property
    def alive(self) -> bool:
        return self.process.returncode is None

    async def _drain_stderr(self) -> None:
        if self.process.stderr is None:
            return
        while line := await self.process.stderr.readline():
            text = line.decode(errors="replace").strip()
            if text:
                self._stderr.append(text)
                self._stderr = self._stderr[-20:]

    async def _read_prefixed(self, prefix: str, *, timeout: float) -> dict[str, Any]:
        stdout = self.process.stdout
        if stdout is None:
            raise BrowserServiceError("Browser worker has no output stream.")

        async def read() -> dict[str, Any]:
            while line := await stdout.readline():
                text = line.decode(errors="replace").rstrip()
                if not text.startswith(prefix):
                    continue
                payload = json.loads(text[len(prefix) :])
                return payload if isinstance(payload, dict) else {}
            detail = self._stderr[-1] if self._stderr else "worker exited"
            raise BrowserServiceError(f"Browser worker stopped: {detail}")

        try:
            return await asyncio.wait_for(read(), timeout=timeout)
        except TimeoutError as exc:
            raise BrowserServiceError("Browser worker timed out.") from exc

    async def call(self, *, code: str, session: str) -> dict[str, Any]:
        async with self._call_lock:
            if not self.alive or self.process.stdin is None:
                raise BrowserServiceError("Browser worker is not running.")
            request_id = uuid4().hex
            message = {
                "id": request_id,
                "code": code,
                "session": session,
                "timeoutSeconds": self.config.call_timeout_seconds,
            }
            self.process.stdin.write((json.dumps(message) + "\n").encode())
            await self.process.stdin.drain()
            envelope = await self._read_prefixed(
                _RESULT_PREFIX,
                timeout=self.config.call_timeout_seconds + 5,
            )
            if envelope.get("id") != request_id:
                raise BrowserServiceError("Browser worker returned an invalid response.")
            result = envelope.get("result")
            if not isinstance(result, dict):
                raise BrowserServiceError("Browser worker returned an invalid result.")
            return result

    async def close(self) -> None:
        if self.process.stdin is not None:
            self.process.stdin.close()
            with contextlib.suppress(Exception):
                await self.process.stdin.wait_closed()
        if self.process.returncode is None:
            try:
                # EOF lets bridge.mjs run browser.close(), which releases the
                # persistent-profile lock. Only signal the process when that
                # graceful path does not finish promptly.
                await asyncio.wait_for(self.process.wait(), timeout=5)
            except TimeoutError:
                self.process.terminate()
                try:
                    await asyncio.wait_for(self.process.wait(), timeout=3)
                except TimeoutError:
                    self.process.kill()
                    await self.process.wait()
        await _stop_unit(self.config, self.unit_name)
        self._stderr_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._stderr_task


class BrowserService:
    """Own one isolated per-user worker and optionally the shared VPN lease."""

    def __init__(
        self,
        config: BrowserServiceConfig,
        *,
        worker_factory: Any | None = None,
        netns_lease: NetnsLease | None = None,
    ) -> None:
        self.config = config
        self._worker_factory = worker_factory or _SubprocessBrowserWorker.create
        self._netns_lease = netns_lease or NetnsLease()
        self._lease_held = False
        self._condition = asyncio.Condition()
        self._worker_lock = asyncio.Lock()
        self._worker: BrowserWorker | None = None
        self._worker_started_at = 0.0
        self._active_owner: str | None = None
        self._active_turns: dict[str, str] = {}
        self._inflight = 0
        self._switching = False
        self._closed = False
        self._fatal_teardown = False
        self._idle_task: asyncio.Task[None] | None = None

    @staticmethod
    def owner_dir_name(user_id: str) -> str:
        digest = hashlib.sha256(user_id.encode()).hexdigest()[:32]
        return f"user-{digest}"

    def profile_home(self, user_id: str) -> Path:
        return self.config.profiles_dir / self.owner_dir_name(user_id)

    def availability_error(self) -> str | None:
        if sys.platform != "linux":
            return "browser workers are supported only on Linux"
        required = [
            self.config.bwrap_bin,
            self.config.prlimit_bin,
            self.config.systemd_run_bin,
            self.config.systemctl_bin,
        ]
        if self.config.network_mode == "netns":
            required += [self.config.sudo_bin, self.config.netns_helper_bin]
        for binary in required:
            if not binary or (not Path(binary).is_file() and shutil.which(binary) is None):
                return f"required executable is missing: {binary}"
        node = self.config.runtime_dir / "node"
        for path, label, executable in (
            (node, "Node runtime", True),
            (self.config.betterwright_module, "BetterWright module", False),
            (self.config.chromium_binary, "BetterChromium binary", True),
            (self.config.bridge_script, "browser bridge", False),
        ):
            if not path.is_file() or (executable and not os.access(path, os.X_OK)):
                return f"{label} is missing or unusable: {path}"
        if (
            self.config.network_mode == "netns"
            and not Path(self.config.netns_resolv_conf).is_file()
        ):
            return f"VPN resolver is missing: {self.config.netns_resolv_conf}"
        if self.config.require_root_owned_runtime:
            try:
                controlled_paths = (
                    self.config.runtime_dir,
                    node,
                    self.config.betterwright_module,
                    self.config.chromium_binary,
                )
                unsafe = [
                    path
                    for path in controlled_paths
                    if path.stat().st_uid != 0
                    or path.stat().st_mode & (stat.S_IWGRP | stat.S_IWOTH)
                ]
            except OSError:
                return f"BetterWright runtime is unreadable: {self.config.runtime_dir}"
            if unsafe:
                return (
                    "BetterWright runtime files must be root-owned and not "
                    f"group/world writable: {unsafe[0]}"
                )
        try:
            seccomp_bpf_bytes()
        except SeccompUnavailableError:
            return "browser seccomp policy is unavailable"
        return None

    @property
    def available(self) -> bool:
        return self.availability_error() is None

    def uses_netns(self) -> bool:
        return self.config.network_mode == "netns"

    def has_active_turn(self, owner_id: str, turn_id: str) -> bool:
        return self._active_turns.get(turn_id) == owner_id

    def _ensure_profile_home(self, user_id: str) -> Path:
        root = self.config.profiles_dir.resolve(strict=False)
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        home = self.profile_home(user_id)
        if home.is_symlink() or not home.resolve(strict=False).is_relative_to(root):
            raise BrowserServiceError("The browser profile path is unsafe.")
        home.mkdir(mode=0o700, exist_ok=True)
        marker = home / ".last_used"
        marker.touch(mode=0o600, exist_ok=True)
        return home

    async def _acquire_physical_lease(self) -> None:
        if not self.uses_netns() or self._lease_held:
            return
        try:
            await self._netns_lease.acquire()
        except NetnsLeasePoisonedError as exc:
            raise BrowserServiceError(
                "The browser VPN namespace is unavailable until restart."
            ) from exc
        self._lease_held = True

    async def _release_physical_lease(self) -> None:
        if not self._lease_held:
            return
        await self._netns_lease.release()
        self._lease_held = False

    async def acquire_turn(self, owner_id: str, turn_id: str) -> bool:
        async with self._condition:
            if self._closed:
                raise BrowserServiceError("The browser service is shutting down.")
            if self._fatal_teardown:
                raise BrowserServiceError(
                    "The browser service failed closed after an unconfirmed worker shutdown."
                )
            existing = self._active_turns.get(turn_id)
            if existing is not None:
                if existing != owner_id:
                    raise BrowserServiceError("The browser turn lease is invalid.")
                return False
            while self._switching or (
                self._active_owner not in (None, owner_id)
                and (self._active_turns or self._inflight)
            ):
                await self._condition.wait()
            old_worker = None
            if self._active_owner not in (None, owner_id):
                self._switching = True
                old_worker = self._worker
                self._cancel_idle()
            else:
                if self._active_owner is None:
                    await self._acquire_physical_lease()
                self._active_owner = owner_id
                self._active_turns[turn_id] = owner_id
                self._cancel_idle()
                return True
        try:
            if old_worker is not None:
                await self._close_worker(old_worker)
        except BaseException:
            if self.uses_netns():
                await self._netns_lease.poison()
            async with self._condition:
                self._switching = False
                self._condition.notify_all()
            raise
        async with self._condition:
            self._active_owner = owner_id
            self._active_turns[turn_id] = owner_id
            self._switching = False
            self._condition.notify_all()
        return True

    async def release_turn(self, owner_id: str, turn_id: str) -> None:
        async with self._condition:
            if self._active_turns.get(turn_id) != owner_id:
                return
            del self._active_turns[turn_id]
            if not self._active_turns and not self._inflight:
                if self._worker is None:
                    self._active_owner = None
                    await self._release_physical_lease()
                else:
                    self._schedule_idle(owner_id)
            self._condition.notify_all()

    async def run(self, *, owner_id: str, turn_id: str, session: str, code: str) -> dict[str, Any]:
        async with self._condition:
            if self._fatal_teardown:
                raise BrowserServiceError(
                    "The browser service failed closed after an unconfirmed worker shutdown."
                )
            if self._active_turns.get(turn_id) != owner_id:
                raise BrowserServiceError("The browser turn is no longer active.")
            self._inflight += 1
        worker: BrowserWorker | None = None
        try:
            worker = await self._ensure_worker(owner_id)
            result = await worker.call(code=code, session=session)
            await asyncio.to_thread(self._touch_and_measure, worker.home)
            if (
                await asyncio.to_thread(_profile_size_bytes, worker.home)
                > self.config.max_profile_bytes
            ):
                await self._discard_worker(worker)
                worker = None
                await asyncio.to_thread(_remove_profile_dir, self.profile_home(owner_id))
                raise BrowserServiceError(
                    "The saved browser profile exceeded its storage limit and was reset."
                )
            return result
        except BrowserServiceError:
            # A timed-out request may still emit a late result. Recycling keeps
            # that stale envelope from being mistaken for the next call.
            if worker is not None:
                await self._discard_worker(worker)
            raise
        except asyncio.CancelledError:
            # Cancellation has the same framing risk as a timeout: stop the
            # worker before allowing another request onto its JSON stream.
            if worker is not None:
                await self._discard_worker(worker)
            raise
        except Exception as exc:
            if worker is not None:
                await self._discard_worker(worker)
            raise BrowserServiceError("The browser action failed.") from exc
        finally:
            async with self._condition:
                self._inflight = max(0, self._inflight - 1)
                if not self._active_turns and not self._inflight:
                    if self._worker is None:
                        self._active_owner = None
                        await self._release_physical_lease()
                    else:
                        self._schedule_idle(owner_id)
                self._condition.notify_all()

    @staticmethod
    def _touch_and_measure(home: Path) -> None:
        (home / ".last_used").touch(mode=0o600, exist_ok=True)

    async def _ensure_worker(self, owner_id: str) -> BrowserWorker:
        async with self._worker_lock:
            if self._worker is not None and self._worker.alive:
                age = time.monotonic() - self._worker_started_at
                if age < self.config.worker_max_lifetime_seconds:
                    return self._worker
                try:
                    await self._worker.close()
                except BaseException:
                    if self.uses_netns():
                        await self._netns_lease.poison()
                    self._fatal_teardown = True
                    raise
                self._worker = None
                self._worker_started_at = 0.0
            if not self.available:
                raise BrowserServiceError("The browser service is unavailable.")
            home = await asyncio.to_thread(self._ensure_profile_home, owner_id)
            if await asyncio.to_thread(_profile_size_bytes, home) > self.config.max_profile_bytes:
                await asyncio.to_thread(_remove_profile_dir, home)
                home = await asyncio.to_thread(self._ensure_profile_home, owner_id)
            self._worker = await self._worker_factory(self.config, owner_id, home)
            self._worker_started_at = time.monotonic()
            return self._worker

    async def _close_worker(self, worker: BrowserWorker) -> None:
        async with self._worker_lock:
            try:
                await worker.close()
            except BaseException:
                if self.uses_netns():
                    await self._netns_lease.poison()
                self._fatal_teardown = True
                raise
            if self._worker is worker:
                self._worker = None
                self._worker_started_at = 0.0

    async def _discard_worker(self, worker: BrowserWorker) -> None:
        await self._close_worker(worker)

    def _cancel_idle(self) -> None:
        task = self._idle_task
        self._idle_task = None
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()

    def _schedule_idle(self, owner_id: str) -> None:
        self._cancel_idle()
        self._idle_task = asyncio.create_task(self._close_after_idle(owner_id))

    async def _close_after_idle(self, owner_id: str) -> None:
        try:
            await asyncio.sleep(self.config.idle_ttl_seconds)
            async with self._condition:
                if (
                    self._closed
                    or self._active_owner != owner_id
                    or self._active_turns
                    or self._inflight
                ):
                    return
                self._switching = True
                worker = self._worker
            if worker is not None:
                await self._close_worker(worker)
            async with self._condition:
                self._active_owner = None
                self._switching = False
                await self._release_physical_lease()
                self._condition.notify_all()
        except asyncio.CancelledError:
            return
        except Exception:
            log.exception("Failed to close idle browser worker")
            async with self._condition:
                self._switching = False
                self._condition.notify_all()

    async def delete_user_data(self, user_id: str) -> int:
        async with self._condition:
            while self._switching or (
                self._active_owner == user_id and (self._active_turns or self._inflight)
            ):
                await self._condition.wait()
            worker = self._worker if self._active_owner == user_id else None
            if worker is not None:
                self._switching = True
                self._cancel_idle()
        if worker is not None:
            await self._close_worker(worker)
            async with self._condition:
                self._active_owner = None
                self._switching = False
                await self._release_physical_lease()
                self._condition.notify_all()
        removed = await asyncio.to_thread(_remove_profile_dir, self.profile_home(user_id))
        return int(removed)

    async def sweep_expired(self) -> int:
        async with self._condition:
            active = self.profile_home(self._active_owner) if self._active_owner else None
        cutoff = time.time() - self.config.profile_ttl_seconds
        return await asyncio.to_thread(_sweep_profiles, self.config.profiles_dir, cutoff, active)

    async def close(self) -> None:
        async with self._condition:
            if self._closed:
                return
            self._closed = True
            self._active_turns.clear()
            self._cancel_idle()
            worker = self._worker
            self._condition.notify_all()
        if worker is not None:
            await self._close_worker(worker)
        async with self._condition:
            self._active_owner = None
            self._switching = False
            await self._release_physical_lease()
            self._condition.notify_all()


def _remove_profile_dir(path: Path) -> bool:
    if path.is_symlink():
        path.unlink()
        return True
    if not path.exists():
        return False
    shutil.rmtree(path)
    return True


def _profile_size_bytes(root: Path) -> int:
    total = 0
    if not root.exists() or root.is_symlink():
        return 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(dirpath)
        dirnames[:] = [name for name in dirnames if not (current / name).is_symlink()]
        for filename in filenames:
            path = current / filename
            with contextlib.suppress(OSError):
                if not path.is_symlink():
                    total += path.stat().st_size
    return total


def _sweep_profiles(root: Path, cutoff: float, active_home: Path | None) -> int:
    if not root.exists():
        return 0
    active = active_home.resolve(strict=False) if active_home else None
    removed = 0
    for child in root.iterdir():
        try:
            if child.is_symlink() or not child.is_dir() or child.resolve(strict=False) == active:
                continue
            marker = child / ".last_used"
            mtime = marker.stat().st_mtime if marker.is_file() else child.stat().st_mtime
            if mtime < cutoff:
                shutil.rmtree(child)
                removed += 1
        except OSError:
            continue
    return removed
