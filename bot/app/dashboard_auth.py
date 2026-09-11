"""Discord Activity authentication. Client-supplied identity is never authority."""

from __future__ import annotations

import asyncio
import base64
import re
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import aiohttp
from aiohttp import web

SESSION_COOKIE = "__Host-kimi-dashboard"
CHALLENGE_COOKIE = "__Host-kimi-dashboard-login"
_INSTANCE_ID = re.compile(r"[A-Za-z0-9_-]{1,200}\Z")
_API = "https://discord.com/api/v10"
_CDN = "https://cdn.discordapp.com"
_AVATAR_HASH = re.compile(r"[A-Za-z0-9_]{1,64}\Z")
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# Avatars are requested at 128px; anything past this is not one.
AVATAR_MAX_BYTES = 512 * 1024


@dataclass(frozen=True, slots=True)
class DashboardSession:
    token: str
    csrf: str
    user_id: str
    guild_id: str
    channel_id: str
    instance_id: str
    expires: float
    # The member's Discord avatar as an inline PNG, so the page keeps its
    # same-origin image policy. None when Discord served nothing usable.
    avatar: str | None = None


def avatar_url(user_id: str, identity: Mapping[str, Any]) -> str:
    """The CDN image Discord itself shows for this user, as a static PNG."""
    avatar = identity.get("avatar")
    if isinstance(avatar, str) and _AVATAR_HASH.fullmatch(avatar):
        return f"{_CDN}/avatars/{user_id}/{avatar}.png?size=128"
    discriminator = str(identity.get("discriminator") or "0")
    if discriminator in {"0", "0000"} or not discriminator.isdigit():
        index = (int(user_id) >> 22) % 6
    else:
        index = int(discriminator) % 5
    return f"{_CDN}/embed/avatars/{index}.png"


def png_data_url(data: bytes) -> str | None:
    """Inline verified PNG bytes; anything else is dropped rather than served."""
    if not data.startswith(_PNG_SIGNATURE) or len(data) > AVATAR_MAX_BYTES:
        return None
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii")


def secure_cookie(response: web.StreamResponse, name: str, value: str, age: int) -> None:
    response.set_cookie(
        name,
        value,
        max_age=age,
        path="/",
        secure=True,
        httponly=True,
        samesite="None",
        partitioned=True,
    )


class DashboardAuth:
    def __init__(
        self,
        *,
        application_id: str,
        bot_token: str,
        client_secret: str,
        session_seconds: int,
        max_sessions: int,
        http: aiohttp.ClientSession,
    ) -> None:
        self.application_id = application_id
        self.origin = f"https://{application_id}.discordsays.com"
        self._bot_token, self._client_secret = bot_token, client_secret
        self._seconds, self._maximum, self._http = session_seconds, max_sessions, http
        self._sessions: dict[str, DashboardSession] = {}
        self._challenges: dict[str, tuple[str, float]] = {}
        self._generation = 0

    def check_origin(self, request: web.Request) -> None:
        if request.headers.get("Origin") != self.origin:
            raise web.HTTPForbidden(reason="Open this dashboard inside Discord")

    def challenge(self, response: web.StreamResponse) -> str:
        self._prune()
        if len(self._challenges) >= self._maximum:
            raise web.HTTPTooManyRequests(reason="Please try signing in again shortly")
        cookie, state = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        self._challenges[cookie] = (state, time.monotonic() + 300)
        secure_cookie(response, CHALLENGE_COOKIE, cookie, 300)
        return state

    async def login(
        self,
        request: web.Request,
        *,
        code: str,
        state: str,
        instance_id: str,
    ) -> tuple[DashboardSession, str]:
        self.check_origin(request)
        self._prune()
        challenge = self._challenges.pop(request.cookies.get(CHALLENGE_COOKIE, ""), None)
        if not challenge or not secrets.compare_digest(challenge[0], state):
            raise web.HTTPUnauthorized(reason="Sign-in expired. Reopen the dashboard")
        if not _INSTANCE_ID.fullmatch(instance_id) or not code or len(code) > 2048:
            raise web.HTTPBadRequest(reason="Invalid Activity sign-in")
        generation = self._generation
        token = await self._json(
            "POST",
            f"{_API}/oauth2/token",
            data={
                "client_id": self.application_id,
                "client_secret": self._client_secret,
                "grant_type": "authorization_code",
                "code": code,
            },
        )
        access_token = token.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise web.HTTPUnauthorized(reason="Discord sign-in failed")
        identity = await self._json(
            "GET",
            f"{_API}/users/@me",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        user_id = str(identity.get("id", ""))
        if not user_id.isdigit():
            raise web.HTTPUnauthorized(reason="Discord identity is unavailable")
        guild_id, channel_id = await self.verify_instance(instance_id, user_id)
        avatar = await self.fetch_avatar(avatar_url(user_id, identity))
        if generation != self._generation:
            raise web.HTTPUnauthorized(reason="Sign-in expired. Reopen the dashboard")
        # Reopening the Activity reauthenticates and overwrites this cookie.
        # Reclaim that browser's old slot only after verifying the same owner;
        # otherwise repeated launches exhaust capacity until the sessions expire.
        previous = self._sessions.get(request.cookies.get(SESSION_COOKIE, ""))
        replacing = previous is not None and previous.user_id == user_id
        if len(self._sessions) - int(replacing) >= self._maximum:
            raise web.HTTPTooManyRequests(reason="Dashboard is busy. Try again shortly")
        if replacing and previous is not None:
            self.logout(previous)
        session = DashboardSession(
            secrets.token_urlsafe(32),
            secrets.token_urlsafe(32),
            user_id,
            guild_id,
            channel_id,
            instance_id,
            time.monotonic() + self._seconds,
            avatar,
        )
        self._sessions[session.token] = session
        return session, access_token

    async def fetch_avatar(self, url: str) -> str | None:
        """Read a CDN avatar as an inline PNG. A missing avatar never fails sign-in."""
        try:
            async with self._http.get(url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status != 200 or response.content_type != "image/png":
                    return None
                try:
                    data = await response.content.readexactly(AVATAR_MAX_BYTES + 1)
                except asyncio.IncompleteReadError as exc:
                    data = exc.partial
        except aiohttp.ClientError, TimeoutError:
            return None
        return png_data_url(data)

    async def verify_instance(self, instance_id: str, user_id: str) -> tuple[str, str]:
        if not _INSTANCE_ID.fullmatch(instance_id):
            raise web.HTTPUnauthorized(reason="Invalid Activity instance")
        instance = await self._json(
            "GET",
            f"{_API}/applications/{self.application_id}/activity-instances/{instance_id}",
            headers={"Authorization": f"Bot {self._bot_token}"},
        )
        location = instance.get("location", {})
        if (
            instance.get("application_id") != self.application_id
            or instance.get("instance_id") != instance_id
            or user_id not in instance.get("users", [])
            or not isinstance(location, dict)
            or location.get("kind") != "gc"
        ):
            raise web.HTTPForbidden(reason="You are not in this server Activity")
        guild, channel = str(location.get("guild_id", "")), str(location.get("channel_id", ""))
        if not guild.isdigit() or not channel.isdigit():
            raise web.HTTPForbidden(reason="Launch the dashboard in a server channel")
        return guild, channel

    def session(self, request: web.Request) -> DashboardSession:
        session = self._sessions.get(request.cookies.get(SESSION_COOKIE, ""))
        if session is None or not self.valid(session):
            raise web.HTTPUnauthorized(reason="Your session expired. Reopen the dashboard")
        if request.method not in {"GET", "HEAD"}:
            self.check_origin(request)
            if not secrets.compare_digest(request.headers.get("X-CSRF-Token", ""), session.csrf):
                raise web.HTTPForbidden(reason="Invalid dashboard request")
        return session

    def valid(self, session: DashboardSession) -> bool:
        return session.expires > time.monotonic() and self._sessions.get(session.token) is session

    def logout(self, session: DashboardSession) -> None:
        self._sessions.pop(session.token, None)

    async def delete_user(self, user_id: str) -> None:
        self._generation += 1
        self._challenges.clear()
        self._sessions = {k: v for k, v in self._sessions.items() if v.user_id != user_id}

    def _prune(self) -> None:
        now = time.monotonic()
        self._sessions = {k: v for k, v in self._sessions.items() if v.expires > now}
        self._challenges = {k: v for k, v in self._challenges.items() if v[1] > now}

    async def _json(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        try:
            async with self._http.request(
                method,
                url,
                timeout=aiohttp.ClientTimeout(total=15),
                **kwargs,
            ) as response:
                if response.status == 429 or response.status >= 500:
                    raise web.HTTPServiceUnavailable(reason="Discord verification is unavailable")
                if response.status != 200:
                    raise web.HTTPUnauthorized(reason="Discord could not verify this session")
                result = await response.json()
                if not isinstance(result, dict):
                    raise web.HTTPUnauthorized(reason="Discord returned an invalid session")
                return result
        except aiohttp.ClientError, TimeoutError:
            raise web.HTTPServiceUnavailable(reason="Discord verification is unavailable") from None
