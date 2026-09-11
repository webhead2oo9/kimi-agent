"""HTTP boundary for members' private Discord Activity chats in the bot process."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

import aiohttp
import discord
from aiohttp import web
from discord import app_commands
from discord.ext import commands

from app.dashboard_access import DashboardAccess
from app.dashboard_auth import (
    CHALLENGE_COOKIE,
    SESSION_COOKIE,
    DashboardAuth,
    DashboardSession,
    png_data_url,
    secure_cookie,
)
from app.dashboard_files import DashboardFiles
from app.dashboard_tasks import DashboardTasks
from app.dashboard_turn import DashboardTurns
from config.settings import Settings
from storage.dashboard import (
    DashboardBusyError,
    DashboardConversation,
    DashboardFile,
    DashboardStore,
)
from storage.dashboard_branches import DashboardBranches
from utils.plugin_privacy import PrivacyDeletionCallbackResult, PrivacyDeletionScope
from utils.privacy_barrier import PrivacyDeletionPendingError, UserPrivacyBarrier
from utils.asyncio import await_uncancellable

log = logging.getLogger(__name__)
_SESSION = web.RequestKey("dashboard_session", DashboardSession)
_BOT_AVATAR_FRESH_SECONDS = 900.0
_BOT_AVATAR_RETRY_SECONDS = 60.0


async def object_body(request: web.Request) -> dict[str, Any]:
    if request.content_type != "application/json":
        raise web.HTTPBadRequest(reason="Expected a JSON request")
    try:
        value = await request.json()
    except ValueError:
        raise web.HTTPBadRequest(reason="Invalid JSON request") from None
    if not isinstance(value, dict):
        raise web.HTTPBadRequest(reason="Expected a JSON object")
    return value


def text_field(body: dict[str, Any], name: str, default: str = "") -> str:
    value = body.get(name, default)
    if not isinstance(value, str):
        raise web.HTTPBadRequest(reason=f"Invalid {name}")
    return value


def cursor(value: str | None) -> int | None:
    if value is None:
        return None
    if not value.isdigit() or len(value) > 18:
        raise web.HTTPBadRequest(reason="Invalid history cursor")
    return int(value)


class Dashboard:
    def __init__(
        self,
        *,
        bot: commands.Bot,
        settings: Settings,
        store: DashboardStore,
        access: DashboardAccess,
        files: DashboardFiles,
        turns: DashboardTurns,
        tasks: DashboardTasks,
        privacy: UserPrivacyBarrier,
        ready: Callable[[], bool],
    ) -> None:
        self.bot, self.settings, self.store = bot, settings, store
        self.access, self.files, self.turns, self.tasks = access, files, turns, tasks
        self.privacy, self.ready = privacy, ready
        self.auth: DashboardAuth | None = None
        self._http: aiohttp.ClientSession | None = None
        self._runner: web.AppRunner | None = None
        self._sockets: dict[web.WebSocketResponse, DashboardSession] = {}
        self._requests = asyncio.Semaphore(8)
        self._rates: dict[str, deque[float]] = {}
        self._unauth_rates: dict[str, deque[float]] = {}
        self._unauth_global: deque[float] = deque()
        self._bot_avatar: tuple[str | None, float] = (None, 0.0)
        self._bot_avatar_lock = asyncio.Lock()
        if settings.dashboard_enabled:
            self._register_command()

    def _register_command(self) -> None:
        @app_commands.command(
            name="dashboard",
            description=f"Open your private {self.settings.bot_name} conversations and files",
        )
        @app_commands.guild_only()
        @app_commands.allowed_installs(guilds=True, users=False)
        async def dashboard(interaction: discord.Interaction) -> None:
            try:
                async with asyncio.timeout(2):
                    await self.access.resolve(
                        user_id=str(interaction.user.id),
                        guild_id=str(interaction.guild_id or ""),
                        channel_id=str(interaction.channel_id or ""),
                    )
                await interaction.response.launch_activity()
            except TimeoutError:
                await interaction.response.send_message(
                    "Channel verification took too long. Please try /dashboard again.",
                    ephemeral=True,
                )
            except web.HTTPException as exc:
                await interaction.response.send_message(exc.reason, ephemeral=True)

        self.bot.tree.add_command(dashboard)

    async def start(self) -> None:
        await self.files.recover_deletions()
        await self.store.interrupt_unfinished()
        if not self.settings.dashboard_enabled or self._runner is not None:
            return
        if not self.settings.dashboard_client_secret.get_secret_value():
            raise ValueError("DASHBOARD_CLIENT_SECRET is required when the dashboard is enabled")
        if self.bot.application_id is None:
            raise ValueError("Discord application identity is unavailable")
        self._http = aiohttp.ClientSession()
        self.auth = DashboardAuth(
            application_id=str(self.bot.application_id),
            bot_token=self.settings.discord_bot_token.get_secret_value(),
            client_secret=self.settings.dashboard_client_secret.get_secret_value(),
            session_seconds=self.settings.dashboard_session_seconds,
            max_sessions=self.settings.dashboard_max_sessions,
            http=self._http,
        )
        try:
            self._runner = web.AppRunner(self.application(), access_log=None, shutdown_timeout=10)
            await self._runner.setup()
            await web.TCPSite(
                self._runner, self.settings.dashboard_host, self.settings.dashboard_port
            ).start()
        except BaseException:
            await self.close()
            raise
        log.info(
            "Dashboard listener started on %s:%d",
            self.settings.dashboard_host,
            self.settings.dashboard_port,
        )

    def application(self, *, static: bool = True) -> web.Application:
        app = web.Application(middlewares=[self._boundary], client_max_size=self.files.upload_limit)
        app.add_routes(
            [
                web.get("/api/bootstrap", self.bootstrap),
                web.post("/api/auth", self.login),
                web.get("/api/session", self.session),
                web.post("/api/logout", self.logout),
                web.post("/api/consent", self.consent),
                web.get("/api/chats", self.list_chats),
                web.post("/api/chats", self.create_chat),
                web.get("/api/chats/{chat}", self.get_chat),
                web.patch("/api/chats/{chat}", self.rename_chat),
                web.delete("/api/chats/{chat}", self.delete_chat),
                web.get("/api/chats/{chat}/events", self.events),
                web.post("/api/chats/{chat}/messages", self.message),
                web.post("/api/chats/{chat}/branches", self.branch_chat),
                web.post("/api/chats/{chat}/return-result", self.return_branch_result),
                web.post("/api/chats/{chat}/stop", self.stop),
                web.post("/api/chats/{chat}/upload", self.upload),
                web.get("/api/chats/{chat}/files", self.file_list),
                web.get("/api/chats/{chat}/workspace", self.workspace_list),
                web.post("/api/chats/{chat}/workspace/snapshot", self.workspace_snapshot),
                web.get("/api/chats/{chat}/tasks", self.task_states),
                web.post("/api/chats/{chat}/task-actions", self.task_action),
                web.get("/api/files/{file}/content", self.file_content),
                web.get("/api/files/{file}/preview", self.file_preview),
                web.get("/api/ws", self.websocket),
            ]
        )
        if static:
            dist = (
                Path(self.settings.dashboard_frontend_dir)
                if self.settings.dashboard_frontend_dir
                else Path(__file__).parent.parent / "dashboard" / "dist"
            )
            if not (dist / "index.html").is_file():
                raise ValueError(
                    "Build the dashboard frontend with npm ci && npm run build in bot/dashboard"
                )

            async def index(request: web.Request) -> web.Response:
                return web.Response(
                    body=await asyncio.to_thread((dist / "index.html").read_bytes),
                    content_type="text/html",
                )

            app.router.add_get("/", index)
            app.router.add_static(
                "/assets/", dist / "assets", show_index=False, follow_symlinks=False
            )
        return app

    @web.middleware
    async def _boundary(
        self, request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]
    ) -> web.StreamResponse:
        try:
            if request.path.startswith("/api/"):
                if not self.ready():
                    raise web.HTTPServiceUnavailable(
                        reason=(
                            f"{self.settings.bot_name} is starting up or reconnecting. "
                            "Try again shortly"
                        )
                    )
                if self.auth is None:
                    raise web.HTTPServiceUnavailable(reason="The dashboard is disabled")
                if request.path not in {"/api/bootstrap", "/api/auth"}:
                    session = self.auth.session(request)
                    request[_SESSION] = session
                    self._rate_limit(session)
                    if request.path == "/api/ws":
                        return await handler(request)
                    async with (
                        self._requests,
                        asyncio.timeout(30),
                        self.privacy.activity(session.user_id),
                    ):
                        if not self.auth.valid(session):
                            raise web.HTTPUnauthorized(
                                reason="Your session expired. Reopen the dashboard"
                            )
                        await self.access.resolve(
                            user_id=session.user_id,
                            guild_id=session.guild_id,
                            channel_id=session.channel_id,
                        )
                        response = await handler(request)
                else:
                    self._rate_limit_unauthenticated(request)
                    async with self._requests, asyncio.timeout(30):
                        response = await handler(request)
            else:
                response = await handler(request)
        except web.HTTPException as exc:
            response = web.json_response({"error": exc.reason}, status=exc.status)
            if exc.status == 429:
                response.headers["Retry-After"] = "60"
        except PrivacyDeletionPendingError:
            response = web.json_response(
                {"error": "Your data deletion is still in progress"}, status=409
            )
        except TimeoutError:
            response = web.json_response(
                {"error": "This request timed out. Try again shortly"}, status=503
            )
        except ValueError, OSError:
            response = web.json_response(
                {"error": "This file or action is unavailable"}, status=400
            )
        except Exception:
            log.exception("Dashboard request failed")
            response = web.json_response(
                {"error": "The request could not be completed"}, status=500
            )
        response.headers.update(
            {
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob: data:; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors https://discord.com https://*.discord.com https://discordapp.com https://*.discordapp.com",
                "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
            }
        )
        return response

    def _rate_limit(self, session: DashboardSession) -> None:
        now = time.monotonic()
        self._rates = {
            key: values for key, values in self._rates.items() if values and values[-1] > now - 60
        }
        recent = self._rates.setdefault(session.user_id, deque())
        while recent and recent[0] <= now - 60:
            recent.popleft()
        if len(recent) >= 120:
            raise web.HTTPTooManyRequests(
                reason="Too many dashboard requests. Try again in a minute"
            )
        recent.append(now)

    def _rate_limit_unauthenticated(self, request: web.Request) -> None:
        # Use the transport peer only. Forwarded / X-Forwarded-For / Cloudflare
        # headers supplied by a client must never mint additional rate buckets.
        peer = request.transport.get_extra_info("peername") if request.transport else None
        source = str(peer[0]) if isinstance(peer, tuple) and peer else "unknown"
        now = time.monotonic()
        self._unauth_rates = {
            key: values
            for key, values in self._unauth_rates.items()
            if values and values[-1] > now - 60
        }
        while self._unauth_global and self._unauth_global[0] <= now - 60:
            self._unauth_global.popleft()
        recent = self._unauth_rates.get(source, deque())
        while recent and recent[0] <= now - 60:
            recent.popleft()
        if len(recent) >= 60 or len(self._unauth_global) >= 240:
            raise web.HTTPTooManyRequests(reason="Too many sign-in requests. Try again in a minute")
        recent.append(now)
        self._unauth_rates[source] = recent
        self._unauth_global.append(now)

    async def bootstrap(self, request: web.Request) -> web.Response:
        assert self.auth is not None
        bot_avatar = await self.bot_avatar()
        response = web.json_response({})
        state = self.auth.challenge(response)
        response.text = json.dumps(
            {
                "client_id": self.auth.application_id,
                "state": state,
                "bot_name": self.settings.bot_name,
                "bot_avatar": bot_avatar,
            }
        )
        return response

    async def bot_avatar(self) -> str | None:
        """The bot's Discord avatar as an inline PNG, re-read a few times an hour."""
        async with self._bot_avatar_lock:
            value, fresh_until = self._bot_avatar
            now = time.monotonic()
            if now < fresh_until:
                return value
            value = await self._read_bot_avatar()
            ttl = _BOT_AVATAR_FRESH_SECONDS if value else _BOT_AVATAR_RETRY_SECONDS
            self._bot_avatar = (value, now + ttl)
            return value

    async def _read_bot_avatar(self) -> str | None:
        user = getattr(self.bot, "user", None)
        if user is None:
            return None
        try:
            async with asyncio.timeout(5):
                data = await user.display_avatar.replace(size=128, format="png").read()
        except discord.DiscordException, aiohttp.ClientError, ValueError, OSError, TimeoutError:
            log.debug("Could not read the bot avatar", exc_info=True)
            return None
        return png_data_url(data)

    async def login(self, request: web.Request) -> web.Response:
        assert self.auth is not None
        body = await object_body(request)
        session, access_token = await self.auth.login(
            request,
            code=text_field(body, "code"),
            state=text_field(body, "state"),
            instance_id=text_field(body, "instance_id"),
        )
        try:
            async with self.privacy.activity(session.user_id):
                await self.access.resolve(
                    user_id=session.user_id,
                    guild_id=session.guild_id,
                    channel_id=session.channel_id,
                )
                if not self.auth.valid(session):
                    raise web.HTTPUnauthorized(reason="Your sign-in expired")
                response = web.json_response(
                    {"access_token": access_token, **await self._session_payload(session)}
                )
                secure_cookie(
                    response, SESSION_COOKIE, session.token, self.settings.dashboard_session_seconds
                )
                secure_cookie(response, CHALLENGE_COOKIE, "", 0)
                return response
        except BaseException:
            self.auth.logout(session)
            raise

    async def _session_payload(self, session: DashboardSession) -> dict[str, Any]:
        return {
            "user_id": session.user_id,
            "guild_id": session.guild_id,
            "channel_id": session.channel_id,
            "csrf": session.csrf,
            "bot_name": self.settings.bot_name,
            "retention_days": self.settings.transcript_retention_days,
            "consent_required": await self.access.consent_required(session.user_id),
            "consent_title": self.settings.privacy_consent_title,
            "consent_text": self.settings.privacy_consent_text,
            "max_upload_bytes": self.files.upload_limit,
            "max_message_chars": self.settings.dashboard_max_message_chars,
            "user_avatar": session.avatar,
        }

    async def session(self, request: web.Request) -> web.Response:
        return web.json_response(await self._session_payload(request[_SESSION]))

    async def logout(self, request: web.Request) -> web.Response:
        assert self.auth is not None
        self.auth.logout(request[_SESSION])
        response = web.json_response({"ok": True})
        secure_cookie(response, SESSION_COOKIE, "", 0)
        return response

    async def consent(self, request: web.Request) -> web.Response:
        body = await object_body(request)
        if type(body.get("accept")) is not bool:
            raise web.HTTPBadRequest(reason="Choose Accept or Decline")
        await self.access.preferences.set_consent(request[_SESSION].user_id, body["accept"])
        return web.json_response({"ok": True})

    async def _chat(
        self, request: web.Request, chat_id: str | None = None
    ) -> DashboardConversation:
        session = request[_SESSION]
        identity = chat_id if chat_id is not None else request.match_info.get("chat")
        if not identity:
            raise web.HTTPBadRequest(reason="Choose a conversation")
        chat = await self.store.get(
            identity,
            user_id=session.user_id,
            guild_id=session.guild_id,
        )
        if chat is None:
            raise web.HTTPNotFound(reason="Conversation not found")
        if chat.channel_id != session.channel_id:
            await self.access.resolve(
                user_id=session.user_id, guild_id=session.guild_id, channel_id=chat.channel_id
            )
        return chat

    async def list_chats(self, request: web.Request) -> web.Response:
        session = request[_SESSION]
        before = request.query.get("before")
        if before is not None and (len(before) > 30 or not before.replace(".", "", 1).isdigit()):
            raise web.HTTPBadRequest(reason="Invalid conversation cursor")
        chats = await self.store.list_chats(
            user_id=session.user_id,
            guild_id=session.guild_id,
            before=float(before) if before else None,
            before_id=request.query.get("before_id", "")[:100],
        )
        # Titles belong to this owner/server; opening their content separately
        # checks the saved channel. A revoked channel does not hide Delete.
        return web.json_response({"chats": [chat.public() for chat in chats]})

    async def create_chat(self, request: web.Request) -> web.Response:
        session = request[_SESSION]
        ctx = await self.access.resolve(
            user_id=session.user_id,
            guild_id=session.guild_id,
            channel_id=session.channel_id,
            continuing=True,
        )
        chat = await self.store.create(
            user_id=session.user_id,
            guild_id=session.guild_id,
            channel_id=session.channel_id,
            parent_channel_id=ctx.parent_id,
            channel_name=ctx.channel.name,
        )
        return web.json_response(chat.public(), status=201)

    async def rename_chat(self, request: web.Request) -> web.Response:
        chat = await self._chat(request)
        await self.store.rename(chat, text_field(await object_body(request), "title"))
        return web.json_response({"ok": True})

    async def get_chat(self, request: web.Request) -> web.Response:
        return web.json_response((await self._chat(request)).public())

    async def _branch_access(self, chat: DashboardConversation) -> None:
        await self.access.resolve(
            user_id=chat.user_id,
            guild_id=chat.guild_id,
            channel_id=chat.channel_id,
            continuing=True,
        )
        if await self.access.consent_required(chat.user_id):
            raise web.HTTPForbidden(reason="Accept the privacy notice before chatting")

    @staticmethod
    def _branch_event(body: dict[str, Any]) -> int:
        event_id = body.get("event_id")
        if type(event_id) is not int or event_id <= 0 or event_id > 2**63 - 1:
            raise web.HTTPBadRequest(reason="Choose a saved message")
        return event_id

    async def branch_chat(self, request: web.Request) -> web.Response:
        chat = await self._chat(request)
        await self._branch_access(chat)
        body = await object_body(request)
        event_id = self._branch_event(body)
        request_id = text_field(body, "request_id")
        if not request_id or len(request_id) > 128:
            raise web.HTTPBadRequest(reason="Invalid branch request")

        async def fork() -> DashboardConversation:
            async with self.files.branch_copies(chat) as copy_file:
                return await DashboardBranches(self.store, copy_file=copy_file).fork(
                    chat, event_id=event_id, request_id=request_id
                )

        try:
            branch = await await_uncancellable(fork())
        except LookupError as exc:
            raise web.HTTPNotFound(reason=str(exc)) from None
        except DashboardBusyError as exc:
            raise web.HTTPConflict(reason=str(exc)) from None
        return web.json_response(branch.public(), status=201)

    async def return_branch_result(self, request: web.Request) -> web.Response:
        branch = await self._chat(request)
        if branch.parent_id is None:
            raise web.HTTPNotFound(reason="Parent conversation no longer exists")
        parent = await self._chat(request, branch.parent_id)
        await self._branch_access(parent)
        event_id = self._branch_event(await object_body(request))

        async def bring_back() -> None:
            async with self.files.branch_copies(branch) as copy_file:
                await DashboardBranches(self.store, copy_file=copy_file).return_result(
                    branch, parent, event_id=event_id
                )

        try:
            await await_uncancellable(bring_back())
        except LookupError as exc:
            raise web.HTTPNotFound(reason=str(exc)) from None
        except DashboardBusyError as exc:
            raise web.HTTPConflict(reason=str(exc)) from None
        updated = await self.store.get(parent.id, user_id=parent.user_id, guild_id=parent.guild_id)
        if updated is None:
            raise web.HTTPNotFound(reason="Parent conversation no longer exists")
        return web.json_response(updated.public())

    async def delete_chat(self, request: web.Request) -> web.Response:
        session = request[_SESSION]
        chat = await self.store.get(
            request.match_info["chat"], user_id=session.user_id, guild_id=session.guild_id
        )
        if chat:
            await self.turns.delete(chat)
        return web.json_response({"ok": True})

    async def events(self, request: web.Request) -> web.Response:
        chat = await self._chat(request)
        events = await self.store.events(
            chat.id,
            after=cursor(request.query.get("after")),
            before=cursor(request.query.get("before")),
        )
        return web.json_response({"events": [asdict(event) for event in events]})

    async def message(self, request: web.Request) -> web.Response:
        chat = await self._chat(request)
        body = await object_body(request)
        files = body.get("file_ids", [])
        if not isinstance(files, list) or not all(isinstance(item, str) for item in files):
            raise web.HTTPBadRequest(reason="Invalid attachments")
        turn_id = await self.turns.submit(
            chat,
            request_id=text_field(body, "request_id"),
            text=text_field(body, "text"),
            file_ids=files,
        )
        return web.json_response({"turn_id": turn_id}, status=202)

    async def stop(self, request: web.Request) -> web.Response:
        clean = await self.turns.stop(await self._chat(request))
        return web.json_response({"ok": True, "clean": clean})

    async def upload(self, request: web.Request) -> web.Response:
        chat = await self._chat(request)
        if await self.access.consent_required(chat.user_id):
            raise web.HTTPForbidden(reason="Accept the privacy notice before uploading")
        filename = request.query.get("filename", "attachment")
        if len(filename) > 500:
            raise web.HTTPBadRequest(reason="Filename is too long")
        payload = await request.read()
        async with self.turns.roots.hold(chat.key):
            chat = await self._chat(request)
            record = await self.files.save(chat, filename, payload, kind="upload")
        return web.json_response(record.public(), status=201)

    async def file_list(self, request: web.Request) -> web.Response:
        chat = await self._chat(request)
        return web.json_response(
            {"files": [record.public() for record in await self.store.files(chat)]}
        )

    async def workspace_list(self, request: web.Request) -> web.Response:
        chat = await self._chat(request)
        return web.json_response(
            {"files": await self.files.workspace_files(chat, request.query.get("directory", ""))}
        )

    async def workspace_snapshot(self, request: web.Request) -> web.Response:
        chat = await self._chat(request)
        path = text_field(await object_body(request), "path")
        async with self.turns.roots.hold(chat.key):
            chat = await self._chat(request)
            record = await self.files.snapshot_workspace(chat, path)
        return web.json_response(record.public())

    async def _file(self, request: web.Request) -> DashboardFile:
        session = request[_SESSION]
        record = await self.store.file(
            request.match_info["file"], user_id=session.user_id, guild_id=session.guild_id
        )
        if record is None:
            raise web.HTTPNotFound(reason="File not found")
        await self._chat(request, record.dashboard_id)
        return record

    async def file_content(self, request: web.Request) -> web.Response:
        record = await self._file(request)
        payload = await self.files.payload(record)
        inline = (
            request.query.get("image") == "1"
            and (await self.files.preview(record)).get("kind") == "image"
        )
        response = web.Response(
            body=payload, content_type=record.media_type if inline else "application/octet-stream"
        )
        response.headers["Content-Disposition"] = aiohttp.helpers.content_disposition_header(
            "inline" if inline else "attachment", filename=record.filename
        )
        return response

    async def file_preview(self, request: web.Request) -> web.Response:
        return web.json_response(await self.files.preview(await self._file(request)))

    async def task_states(self, request: web.Request) -> web.Response:
        chat = await self._chat(request)
        return web.json_response(
            {
                "tasks": await self.tasks.states(chat),
                "events": [asdict(event) for event in await self.store.work_events(chat.id)],
            }
        )

    async def task_action(self, request: web.Request) -> web.Response:
        chat = await self._chat(request)
        body = await object_body(request)
        revision = body.get("revision", 0)
        if type(revision) is not int or not 0 <= revision < 1_000_000:
            raise web.HTTPBadRequest(reason="Invalid task revision")
        action_id = await self.tasks.submit_action(
            chat,
            request_id=text_field(body, "request_id"),
            task_id=text_field(body, "task_id"),
            action=text_field(body, "action"),
            revision=revision,
            message=text_field(body, "message"),
        )
        return web.json_response({"action_id": action_id}, status=202)

    async def websocket(self, request: web.Request) -> web.StreamResponse:
        assert self.auth is not None
        self.auth.check_origin(request)
        session = request[_SESSION]
        if sum(value.user_id == session.user_id for value in self._sockets.values()) >= 3:
            # Browsers hide the HTTP status/body of a rejected upgrade. Deliver
            # only this fixed notice on a short-lived socket, never private events.
            limited = web.WebSocketResponse(timeout=2)
            await limited.prepare(request)
            await limited.send_json(
                {
                    "code": "socket_limit",
                    "error": "Close another dashboard tab before reconnecting",
                }
            )
            await limited.close(code=4008, message=b"Close another dashboard tab")
            return limited
        after = cursor(request.query.get("after")) or 0
        socket = web.WebSocketResponse(heartbeat=20, max_msg_size=1024)
        # Reserve the slot before any lookup awaits, so simultaneous handshakes
        # cannot all pass the same per-user connection count.
        self._sockets[socket] = session
        try:
            chat = await self._chat(request, request.query.get("chat", ""))
            await socket.prepare(request)
            check_at = 0.0
            verified = False
            while not socket.closed:
                if not self.auth.valid(session):
                    raise web.HTTPUnauthorized(reason="Session expired")
                if time.monotonic() >= check_at:
                    await self.access.resolve(
                        user_id=session.user_id,
                        guild_id=session.guild_id,
                        channel_id=session.channel_id,
                    )
                    await self.access.resolve(
                        user_id=session.user_id,
                        guild_id=session.guild_id,
                        channel_id=chat.channel_id,
                    )
                    guild, channel = await self.auth.verify_instance(
                        session.instance_id, session.user_id
                    )
                    if (guild, channel) != (session.guild_id, session.channel_id):
                        raise web.HTTPForbidden(reason="Activity context changed")
                    check_at = time.monotonic() + 15
                async with self.privacy.activity(session.user_id):
                    if (
                        not self.auth.valid(session)
                        or await self.store.get(
                            chat.id, user_id=session.user_id, guild_id=session.guild_id
                        )
                        is None
                    ):
                        raise web.HTTPForbidden(reason="Conversation access expired")
                    events = await self.store.events(chat.id, after=after)
                    if events or not verified:
                        async with asyncio.timeout(10):
                            await socket.send_json(
                                {"events": [asdict(event) for event in events], "ready": True}
                            )
                        verified = True
                        if events:
                            after = events[-1].id
                if len(events) == 200:
                    continue
                try:
                    message = await socket.receive(timeout=1)
                    if message.type in {
                        aiohttp.WSMsgType.CLOSE,
                        aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.ERROR,
                    }:
                        break
                    if message.type == aiohttp.WSMsgType.TEXT:
                        await socket.close(code=1008, message=b"This socket only delivers updates")
                except TimeoutError:
                    pass
        except web.HTTPException as exc:
            if not socket.prepared:
                raise
            if exc.status == 429 or exc.status >= 500:
                await socket.close(code=1013, message=b"Verification unavailable. Reconnecting")
            else:
                await socket.close(code=1008, message=b"Access expired. Reopen the dashboard")
        except PrivacyDeletionPendingError:
            await socket.close(code=1008, message=b"Access expired. Reopen the dashboard")
        except ConnectionError, TimeoutError:
            if not socket.prepared:
                raise
        finally:
            self._sockets.pop(socket, None)
            if socket.prepared:
                await socket.close()
        return socket

    async def delete_user(
        self, user_id: str, scope: PrivacyDeletionScope
    ) -> PrivacyDeletionCallbackResult:
        await self.turns.delete_user(user_id)
        if self.auth:
            await self.auth.delete_user(user_id)
        for socket, session in list(self._sockets.items()):
            if session.user_id == user_id and socket.prepared:
                await socket.close(code=1008, message=b"Your data was deleted")
        return PrivacyDeletionCallbackResult(
            True, ("Revoked dashboard sessions and pending chat requests.",)
        )

    async def close(self) -> None:
        for socket in list(self._sockets):
            if socket.prepared:
                await socket.close(code=1001, message=b"The dashboard is restarting")
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        await self.turns.close()
        await self.tasks.close()
        if self._http:
            await self._http.close()
            self._http = None
        self.auth = None
