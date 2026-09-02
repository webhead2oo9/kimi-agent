from __future__ import annotations

from dataclasses import dataclass

from xai.auth import XaiAuthError, XaiAuthRevokedError, XaiOAuthManager

AUTH_MODE_OAUTH = "oauth"
AUTH_MODE_API_KEY = "api_key"
AUTH_MODE_AUTO = "auto"
XAI_AUTH_MODES = frozenset({AUTH_MODE_OAUTH, AUTH_MODE_API_KEY, AUTH_MODE_AUTO})


@dataclass(frozen=True, slots=True)
class XaiCredential:
    bearer: str
    source: str


class XaiCredentialResolver:
    """Resolve strict or OAuth-first xAI credentials without silent mode widening."""

    def __init__(
        self,
        *,
        auth_mode: str,
        oauth_manager: XaiOAuthManager | None,
        api_key: str,
    ) -> None:
        if auth_mode not in XAI_AUTH_MODES:
            raise ValueError(f"unknown xAI auth mode: {auth_mode!r}")
        self.auth_mode = auth_mode
        self.oauth_manager = oauth_manager
        self.api_key = api_key.strip()

    def is_available(self) -> bool:
        if self.auth_mode == AUTH_MODE_API_KEY:
            return bool(self.api_key)
        oauth_ready = self.oauth_manager is not None and self.oauth_manager.is_available()
        if self.auth_mode == AUTH_MODE_OAUTH:
            return oauth_ready
        return oauth_ready or bool(self.api_key)

    async def primary(self) -> XaiCredential:
        if self.auth_mode == AUTH_MODE_API_KEY:
            return self._required_api_key()
        if self.auth_mode == AUTH_MODE_OAUTH:
            return await self._required_oauth()

        manager = self.oauth_manager
        if manager is None or not manager.is_available():
            return self._required_api_key()
        try:
            return XaiCredential(await manager.get_access_token(), AUTH_MODE_OAUTH)
        except XaiAuthRevokedError:
            if self.api_key:
                return XaiCredential(self.api_key, AUTH_MODE_API_KEY)
            raise

    async def after_unauthorized(self, credential: XaiCredential) -> XaiCredential | None:
        if credential.source != AUTH_MODE_OAUTH or self.oauth_manager is None:
            return None
        try:
            await self.oauth_manager.refresh_tokens(force=True)
            return XaiCredential(
                await self.oauth_manager.get_access_token(),
                AUTH_MODE_OAUTH,
            )
        except XaiAuthRevokedError:
            return self.api_key_fallback()

    def api_key_fallback(self) -> XaiCredential | None:
        if self.auth_mode == AUTH_MODE_AUTO and self.api_key:
            return XaiCredential(self.api_key, AUTH_MODE_API_KEY)
        return None

    async def _required_oauth(self) -> XaiCredential:
        if self.oauth_manager is None or not self.oauth_manager.is_available():
            raise XaiAuthError("xAI OAuth is selected but no OAuth tokens are available")
        return XaiCredential(await self.oauth_manager.get_access_token(), AUTH_MODE_OAUTH)

    def _required_api_key(self) -> XaiCredential:
        if not self.api_key:
            raise XaiAuthError("xAI API-key authentication is selected but GROK_API_KEY is empty")
        return XaiCredential(self.api_key, AUTH_MODE_API_KEY)
