from __future__ import annotations

import asyncio
import base64
import importlib
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiohttp

from utils.files import atomic_write_text

XAI_OAUTH_ISSUER = "https://auth.x.ai"
XAI_OAUTH_DISCOVERY_URL = f"{XAI_OAUTH_ISSUER}/.well-known/openid-configuration"
XAI_OAUTH_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
XAI_OAUTH_SCOPE = "openid profile email offline_access grok-cli:access api:access"
XAI_DEVICE_AUTHORIZATION_ENDPOINT = f"{XAI_OAUTH_ISSUER}/oauth2/device/code"
XAI_API_BASE_URL = "https://api.x.ai/v1"
DEFAULT_XAI_TOKEN_FILE = Path("secrets/xai-oauth.json")

_REFRESH_BUFFER_SECONDS = 120
_HTTP_TIMEOUT_SECONDS = 30
# A refresh holds the lock across discovery and the token request, each with its
# own HTTP timeout. Give a healthy in-flight owner enough time to finish before
# another process treats lock acquisition as failed.
_LOCK_TIMEOUT_SECONDS = _HTTP_TIMEOUT_SECONDS * 2 + 15

_TERMINAL_REFRESH_ERRORS = frozenset(
    {
        "access_denied",
        "expired_token",
        "invalid_client",
        "invalid_grant",
        "invalid_scope",
        "invalid_token",
        "refresh_token_expired",
        "refresh_token_invalidated",
        "refresh_token_reused",
        "unauthorized_client",
    }
)
_TRANSIENT_OAUTH_ERRORS = frozenset({"server_error", "temporarily_unavailable"})

log = logging.getLogger(__name__)


class XaiAuthError(RuntimeError):
    pass


class XaiAuthRevokedError(XaiAuthError):
    """The refresh credential was rejected and interactive login is required."""


@dataclass(frozen=True, slots=True)
class XaiOAuthDiscovery:
    token_endpoint: str
    device_authorization_endpoint: str


def _validate_xai_endpoint(value: str, *, field: str) -> str:
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host or (host != "x.ai" and not host.endswith(".x.ai")):
        raise XaiAuthError(f"xAI OAuth discovery returned an invalid {field}")
    return value


async def discover_xai_oauth(
    *,
    http_session: Any | None = None,
    timeout_seconds: float = _HTTP_TIMEOUT_SECONDS,
) -> XaiOAuthDiscovery:
    async def fetch(session: Any) -> dict[str, Any]:
        async with session.get(XAI_OAUTH_DISCOVERY_URL) as response:
            text = await response.text()
            if response.status < 200 or response.status >= 300:
                raise XaiAuthError(f"xAI OAuth discovery failed ({response.status})")
            try:
                payload = json.loads(text)
            except json.JSONDecodeError as exc:
                raise XaiAuthError("xAI OAuth discovery returned invalid JSON") from exc
            if not isinstance(payload, dict):
                raise XaiAuthError("xAI OAuth discovery returned a non-object payload")
            return payload

    if http_session is not None:
        payload = await fetch(http_session)
    else:
        timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
            payload = await fetch(session)

    token_endpoint = payload.get("token_endpoint")
    if not isinstance(token_endpoint, str) or not token_endpoint:
        raise XaiAuthError("xAI OAuth discovery did not include token_endpoint")
    device_endpoint = payload.get("device_authorization_endpoint")
    if not isinstance(device_endpoint, str) or not device_endpoint:
        device_endpoint = XAI_DEVICE_AUTHORIZATION_ENDPOINT
    return XaiOAuthDiscovery(
        token_endpoint=_validate_xai_endpoint(token_endpoint, field="token_endpoint"),
        device_authorization_endpoint=_validate_xai_endpoint(
            device_endpoint,
            field="device_authorization_endpoint",
        ),
    )


def write_xai_tokens(path: Path, tokens: dict[str, Any]) -> None:
    # The mode is applied only when the leaf directory is created. Never chmod
    # an existing operator-selected parent (which could be a shared directory).
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_write_text(path, json.dumps(tokens, indent=2), fsync=False, mode=0o600)


async def write_xai_tokens_locked(path: Path, tokens: dict[str, Any]) -> None:
    """Serialize an interactive credential replacement with runtime refreshes."""

    file_lock = _CrossProcessFileLock(path.with_suffix(".lock"))
    await file_lock.acquire()
    try:
        write_xai_tokens(path, tokens)
    finally:
        await file_lock.release()


def token_record(
    payload: dict[str, Any],
    *,
    previous_refresh_token: str = "",
) -> dict[str, Any]:
    access_token = payload.get("access_token")
    refresh_token = payload.get("refresh_token") or previous_refresh_token
    if not isinstance(access_token, str) or not access_token:
        raise XaiAuthError("xAI token response did not include access_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise XaiAuthError("xAI token response did not include refresh_token")
    expires_in = payload.get("expires_in", 3600)
    try:
        expires_in_seconds = max(0, int(expires_in))
    except TypeError, ValueError:
        expires_in_seconds = 3600
    id_token = payload.get("id_token")
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "id_token": id_token if isinstance(id_token, str) else "",
        "account_id": _jwt_subject(id_token) if isinstance(id_token, str) else "",
        "expires_at": int(time.time() * 1000) + expires_in_seconds * 1000,
    }


def _jwt_subject(token: str) -> str:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return ""
        raw = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(raw).decode("utf-8"))
        subject = payload.get("sub") if isinstance(payload, dict) else None
        return subject if isinstance(subject, str) else ""
    except ValueError, UnicodeDecodeError, json.JSONDecodeError:
        return ""


class _CrossProcessFileLock:
    """Small stdlib-only advisory lock used to serialize rotating refresh tokens."""

    def __init__(self, path: Path, timeout_seconds: float = _LOCK_TIMEOUT_SECONDS) -> None:
        self.path = path
        self.timeout_seconds = timeout_seconds
        self._handle: Any | None = None

    async def acquire(self) -> None:
        await asyncio.to_thread(self._acquire_sync)

    async def release(self) -> None:
        await asyncio.to_thread(self._release_sync)

    def _acquire_sync(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        handle = self.path.open("a+b")
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt: Any = importlib.import_module("msvcrt")
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._handle = handle
                return
            except OSError as exc:
                if time.monotonic() >= deadline:
                    handle.close()
                    raise XaiAuthError("timed out waiting for the xAI token lock") from exc
                time.sleep(0.05)

    def _release_sync(self) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                msvcrt: Any = importlib.import_module("msvcrt")
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


class XaiOAuthManager:
    def __init__(
        self,
        token_file: str | Path = DEFAULT_XAI_TOKEN_FILE,
        *,
        http_session: Any | None = None,
    ) -> None:
        self.token_file = Path(token_file)
        self._http_session = http_session
        self._tokens: dict[str, Any] | None = self._read_tokens()
        self._refresh_lock = asyncio.Lock()

    def is_available(self) -> bool:
        disk_tokens = self._read_tokens()
        self._tokens = disk_tokens
        return bool(self._tokens and self._tokens.get("refresh_token"))

    def get_account_id(self) -> str:
        return str((self._tokens or {}).get("account_id") or "")

    async def get_access_token(self) -> str:
        if self._tokens is None:
            self._tokens = self._read_tokens()
        if not self._tokens:
            raise XaiAuthError(
                "No xAI OAuth tokens are available. Run: python scripts/xai_auth.py "
                f"--token-file {self.token_file}"
            )
        if self._is_expired(self._tokens):
            await self.refresh_tokens()
        access_token = (self._tokens or {}).get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise XaiAuthError("xAI token file does not contain an access_token")
        return access_token

    async def refresh_tokens(self, *, force: bool = False) -> None:
        async with self._refresh_lock:
            before_lock = self._tokens
            file_lock = _CrossProcessFileLock(self.token_file.with_suffix(".lock"))
            await file_lock.acquire()
            try:
                disk_tokens = self._read_tokens()
                if not disk_tokens:
                    raise XaiAuthError("xAI token file does not contain a refresh_token")
                changed = disk_tokens != before_lock
                self._tokens = disk_tokens
                if changed and not self._is_expired(disk_tokens):
                    log.info("Reloaded xAI credentials refreshed by another process")
                    return
                if not force and not self._is_expired(disk_tokens):
                    return
                await self._do_refresh()
            finally:
                await file_lock.release()

    def _read_tokens(self) -> dict[str, Any] | None:
        try:
            raw = self.token_file.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise XaiAuthError(f"Could not read xAI token file: {self.token_file}") from exc
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise XaiAuthError(f"xAI token file contains invalid JSON: {self.token_file}") from exc
        if not isinstance(payload, dict) or not payload.get("refresh_token"):
            raise XaiAuthError(f"xAI token file is missing refresh_token: {self.token_file}")
        return payload

    @staticmethod
    def _is_expired(tokens: dict[str, Any]) -> bool:
        access_token = tokens.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            return True
        expires_at = tokens.get("expires_at")
        if isinstance(expires_at, bool) or not isinstance(expires_at, str | int | float):
            return True
        try:
            expires_at_ms = float(expires_at)
        except TypeError, ValueError:
            return True
        return time.time() * 1000 >= expires_at_ms - _REFRESH_BUFFER_SECONDS * 1000

    async def _do_refresh(self) -> None:
        assert self._tokens is not None
        refresh_token = self._tokens.get("refresh_token")
        if not isinstance(refresh_token, str) or not refresh_token:
            raise XaiAuthError("xAI token file does not contain a refresh_token")
        discovery = await discover_xai_oauth(http_session=self._http_session)
        payload = await self._post_form(
            discovery.token_endpoint,
            {
                "client_id": XAI_OAUTH_CLIENT_ID,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "scope": XAI_OAUTH_SCOPE,
            },
        )
        self._tokens = token_record(payload, previous_refresh_token=refresh_token)
        write_xai_tokens(self.token_file, self._tokens)

    async def _post_form(self, url: str, body: dict[str, str]) -> dict[str, Any]:
        async def post(session: Any) -> dict[str, Any]:
            async with session.post(
                url,
                data=body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            ) as response:
                text = await response.text()
                if response.status < 200 or response.status >= 300:
                    error_code = _oauth_error_code(text)
                    if error_code in _TRANSIENT_OAUTH_ERRORS:
                        raise XaiAuthError(f"xAI OAuth refresh failed temporarily ({error_code})")
                    if error_code in _TERMINAL_REFRESH_ERRORS or response.status in {401, 403}:
                        raise XaiAuthRevokedError(
                            f"xAI OAuth refresh was rejected ({error_code or response.status}); "
                            "run scripts/xai_auth.py again"
                        )
                    raise XaiAuthError(f"xAI OAuth refresh failed ({response.status})")
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError as exc:
                    raise XaiAuthError("xAI OAuth refresh returned invalid JSON") from exc
                if not isinstance(payload, dict):
                    raise XaiAuthError("xAI OAuth refresh returned a non-object payload")
                return payload

        if self._http_session is not None:
            return await post(self._http_session)
        timeout = aiohttp.ClientTimeout(total=_HTTP_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
            return await post(session)


def _oauth_error_code(text: str) -> str:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return ""
    if not isinstance(payload, dict):
        return ""
    error = payload.get("error")
    if isinstance(error, dict):
        error = error.get("code") or error.get("error")
    return error if isinstance(error, str) else ""
