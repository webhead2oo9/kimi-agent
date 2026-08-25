from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from pathlib import Path

from utils.files import atomic_write_text
from typing import Any

import aiohttp

CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
TOKEN_URL = "https://auth.openai.com/oauth/token"
REFRESH_BUFFER_MS = 5 * 60 * 1000
UNRECOVERABLE_REFRESH_ERRORS = {
    "refresh_token_expired",
    "refresh_token_reused",
    "refresh_token_invalidated",
}

log = logging.getLogger(__name__)


def write_owner_only(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically with owner-only (0600) permissions.

    The temp file is created 0600 by ``mkstemp`` and the mode is re-applied
    after the replace, so the refresh token is never briefly world-readable --
    not even in the window between creating the file and restricting it.
    """

    atomic_write_text(path, text, fsync=False, mode=0o600)


class CodexAuthError(RuntimeError):
    pass


class CodexAuthRevokedError(CodexAuthError):
    """Refresh failed with an unrecoverable error; re-authentication is required."""


class CodexAuthManager:
    def __init__(
        self,
        token_file: str | Path = "secrets/codex-auth.json",
        *,
        http_session: Any | None = None,
    ) -> None:
        self.token_file = Path(token_file)
        self._http_session = http_session
        self._tokens: dict[str, Any] | None = None
        self._refresh_lock = asyncio.Lock()
        self._load_tokens()

    def is_available(self) -> bool:
        return bool(self._tokens and self._tokens.get("refresh_token"))

    def get_account_id(self) -> str:
        return str((self._tokens or {}).get("account_id") or "")

    async def get_access_token(self) -> str:
        if not self._tokens:
            raise CodexAuthError(
                f"No Codex auth tokens available. Run: python scripts/codex_auth.py "
                f"--token-file {self.token_file}"
            )
        if self._is_expired():
            await self.refresh_tokens()
        token = self._tokens.get("access_token") if self._tokens else None
        if not isinstance(token, str) or not token:
            raise CodexAuthError("Codex token file does not contain an access_token")
        return token

    async def refresh_tokens(self, *, force: bool = False) -> None:
        async with self._refresh_lock:
            expected_account_id = self.get_account_id()
            disk_tokens = self._read_tokens()
            disk_account_id = str((disk_tokens or {}).get("account_id") or "")
            if not expected_account_id or disk_account_id != expected_account_id:
                raise CodexAuthError(
                    "Codex auth changed to a different account; restart the bot or "
                    "run scripts/codex_auth.py again"
                )

            auth_changed = disk_tokens != self._tokens
            self._tokens = disk_tokens
            if auth_changed and not self._is_expired():
                log.info("Reloaded refreshed Codex credentials from the token file")
                return
            if not force and self._tokens and not self._is_expired():
                return
            await self._do_refresh()

    def _load_tokens(self) -> None:
        self._tokens = self._read_tokens()

    def _read_tokens(self) -> dict[str, Any] | None:
        if not self.token_file.exists():
            log.warning("Codex token file not found: %s", self.token_file)
            return None
        try:
            parsed = json.loads(self.token_file.read_text(encoding="utf-8"))
        except OSError, json.JSONDecodeError:
            log.exception("Failed to load Codex token file: %s", self.token_file)
            return None
        if not isinstance(parsed, dict) or not parsed.get("refresh_token"):
            log.error("Codex token file is missing refresh_token: %s", self.token_file)
            return None
        if not parsed.get("account_id") and isinstance(parsed.get("id_token"), str):
            parsed["account_id"] = self.decode_account_id(parsed["id_token"])
        return parsed

    def _is_expired(self) -> bool:
        if not self._tokens:
            return True
        access_token = self._tokens.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            return True
        expires_at = self._tokens.get("expires_at")
        if not isinstance(expires_at, str | int | float):
            return True
        try:
            expires_at_ms = float(expires_at)
        except TypeError, ValueError:
            return True
        return time.time() * 1000 >= expires_at_ms - REFRESH_BUFFER_MS

    async def _do_refresh(self) -> None:
        if not self._tokens or not self._tokens.get("refresh_token"):
            raise CodexAuthError("No Codex refresh_token available")
        body = {
            "client_id": CODEX_CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": str(self._tokens["refresh_token"]),
            "scope": "openid profile email",
        }
        payload = await self._post_token(body)
        access_token = payload.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise CodexAuthError("Codex token refresh response did not include access_token")
        id_token = payload.get("id_token")
        refresh_token = payload.get("refresh_token") or self._tokens.get("refresh_token")
        expires_in = payload.get("expires_in")
        if expires_in is None:
            expires_in = 3600
        try:
            expires_in_seconds = int(expires_in)
        except TypeError, ValueError:
            expires_in_seconds = 3600
        self._tokens = {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "id_token": id_token or self._tokens.get("id_token", ""),
            "account_id": (
                self.decode_account_id(id_token)
                if isinstance(id_token, str)
                else self._tokens.get("account_id", "")
            ),
            "expires_at": int(time.time() * 1000) + expires_in_seconds * 1000,
        }
        self._save_tokens()

    async def _post_token(self, body: dict[str, str]) -> dict[str, Any]:
        if self._http_session is not None:
            return await self._post_token_with_session(self._http_session, body)
        # The refresh runs on the user-facing turn path; fail fast instead of
        # inheriting aiohttp's 300s default total timeout.
        client_timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=client_timeout, trust_env=False) as session:
            return await self._post_token_with_session(session, body)

    async def _post_token_with_session(
        self,
        session: Any,
        body: dict[str, str],
    ) -> dict[str, Any]:
        async with session.post(
            TOKEN_URL,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        ) as response:
            text = await response.text()
            if response.status < 200 or response.status >= 300:
                error_code = self._error_code(text)
                if error_code in UNRECOVERABLE_REFRESH_ERRORS:
                    log.error(
                        "Codex refresh token is invalid (%s); run python scripts/codex_auth.py",
                        error_code,
                    )
                    raise CodexAuthRevokedError(
                        f"Codex refresh token is invalid ({error_code}); "
                        "run python scripts/codex_auth.py"
                    )
                raise CodexAuthError(f"Codex token refresh failed ({response.status}): {text}")
            try:
                payload = json.loads(text)
            except json.JSONDecodeError as exc:
                raise CodexAuthError("Codex token refresh returned invalid JSON") from exc
            if not isinstance(payload, dict):
                raise CodexAuthError("Codex token refresh returned a non-object JSON payload")
            return payload

    def _save_tokens(self) -> None:
        if not self._tokens:
            return
        write_owner_only(self.token_file, json.dumps(self._tokens, indent=2))

    @staticmethod
    def decode_account_id(id_token: str) -> str:
        try:
            parts = id_token.split(".")
            if len(parts) < 2:
                return ""
            payload_part = parts[1]
            payload_part += "=" * (-len(payload_part) % 4)
            payload = base64.urlsafe_b64decode(payload_part.encode()).decode("utf-8")
            claims = json.loads(payload)
        except ValueError, json.JSONDecodeError, UnicodeDecodeError:
            log.warning("Failed to decode Codex id_token")
            return ""
        if not isinstance(claims, dict):
            return ""
        auth_claim = claims.get("https://api.openai.com/auth") or {}
        if not isinstance(auth_claim, dict):
            auth_claim = {}
        for key in ("chatgpt_account_id", "organization_id"):
            value = auth_claim.get(key) or claims.get(key)
            if isinstance(value, str) and value:
                return value
        return ""

    @staticmethod
    def _error_code(text: str) -> str:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return ""
        if not isinstance(payload, dict):
            return ""
        error = payload.get("error")
        value = error.get("code") or error.get("error") if isinstance(error, dict) else error
        return value if isinstance(value, str) else ""
