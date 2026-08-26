"""Staff-approved, guild-scoped configuration fragment proposals."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Collection
from dataclasses import asdict, dataclass
import hashlib
import json
import logging
from pathlib import Path
import re
import stat
import time
from typing import Any, cast
import uuid

from config.fragments.guild_config import server_setup_activation
from kimi_agent_module_api import (
    ConfigSnapshot,
    ProposalActor,
    ProposalError,
    ProposalRef,
    ProposalService,
    ProposalState,
)
from kimi_agent_module_api.contracts import (
    ButtonSpec,
    InteractionRouter,
    MessageRef,
    ModuleInteraction,
    OutgoingEmbed,
    Registration,
    TrustTierName,
    parse_custom_id,
)
from storage.db import Database
from tools.embeds import FIELD_VALUE_MAX, TOTAL_MAX
from utils.files import atomic_write_text
from utils.frontmatter import FrontmatterError, split_frontmatter_strict

log = logging.getLogger(__name__)

APPROVER_TIER: TrustTierName = "staff"
ROUTER_NAME = "proposals"
CONTENT_MAX_BYTES = 1_000_000
SUMMARY_MAX_CHARS = 1_000

_SNOWFLAKE = re.compile(r"[0-9]{1,20}")
_SAFE_NAME = re.compile(r"[a-z][a-z0-9_-]{0,63}")
_PROPOSAL_ID = re.compile(r"[0-9a-f]{32}")
_DECISION_LOCK = asyncio.Lock()

_COLOR_PENDING = 0xFEE75C
_COLOR_APPLIED = 0x57F287
_COLOR_REJECTED = 0xED4245


@dataclass(frozen=True, slots=True)
class ProposalTarget:
    kind: str
    relative: Path
    guild_id: str | None
    module_name: str | None = None
    channel_id: str | None = None


@dataclass(frozen=True, slots=True)
class ProposalHost:
    config_dir: Callable[[], Path]
    review_channel_id: Callable[[str], str | None]
    channel_guild_id: Callable[[int], Awaitable[int | None]]
    known_modules: Callable[[], Collection[str]]
    post_review: Callable[..., Awaitable[MessageRef]]
    on_applied: Callable[[int], Awaitable[None]]
    verify_guild: Callable[[int], str]


@dataclass(frozen=True, slots=True)
class _FileSnapshot:
    path: Path
    exists: bool
    content: str
    revision: str


@dataclass(frozen=True, slots=True)
class ProposalRow:
    proposal_id: str
    module_name: str
    guild_id: str
    target: str
    content: str
    content_revision: str
    base_exists: bool
    base_content: str
    base_revision: str
    summary: str
    actor: ProposalActor
    state: ProposalState
    decided_by: str | None
    decision_reason: str
    message_channel_id: str | None
    message_id: str | None
    created_at: float
    updated_at: float

    def public_ref(self) -> ProposalRef:
        message = None
        if self.message_channel_id is not None and self.message_id is not None:
            message = MessageRef(
                int(self.guild_id), int(self.message_channel_id), int(self.message_id)
            )
        return ProposalRef(
            self.proposal_id,
            self.target,
            self.state,
            message,
            self.decided_by,
            self.decision_reason,
        )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _positive_id(value: str, *, label: str) -> str:
    token = value.strip()
    if not _SNOWFLAKE.fullmatch(token) or int(token) <= 0:
        raise ProposalError(f"{label} must be a positive numeric Discord id")
    return token


def resolve_target(target: str) -> ProposalTarget:
    """Map one public target token to a fixed path below CONFIG_DIR."""
    kind, separator, identifier = target.strip().partition(":")
    if not separator or not identifier:
        raise ProposalError("proposal target must use '<kind>:<identifier>'")
    if kind == "guild" and ":" in identifier:
        guild_id, _, module_name = identifier.partition(":")
        guild_id = _positive_id(guild_id, label="guild module target")
        if not _SAFE_NAME.fullmatch(module_name):
            raise ProposalError("invalid module name in guild module target")
        return ProposalTarget(
            "guild-module",
            Path("guild-modules") / guild_id / f"{module_name}.md",
            guild_id,
            module_name,
        )
    if kind == "guild":
        guild_id = _positive_id(identifier, label="guild target")
        return ProposalTarget("guild", Path("servers") / f"{guild_id}.md", guild_id)
    if kind == "channel":
        channel_id = _positive_id(identifier, label="channel target")
        return ProposalTarget(
            "channel", Path("channels") / f"{channel_id}.md", None, channel_id=channel_id
        )
    raise ProposalError(f"unsupported configuration proposal target {target!r}")


def validate_content(target: ProposalTarget, content: str) -> None:
    encoded = content.encode("utf-8")
    if len(encoded) > CONTENT_MAX_BYTES:
        raise ProposalError("configuration fragment exceeds the 1 MB limit")
    try:
        _metadata, body = split_frontmatter_strict(content)
    except FrontmatterError as exc:
        raise ProposalError(f"invalid {target.kind} configuration fragment: {exc}") from exc
    if target.kind == "guild-module" and body.strip():
        raise ProposalError("guild module configuration cannot contain a Markdown body")
    if target.kind == "guild" and server_setup_activation(content) is None:
        raise ProposalError(
            "guild configuration must include valid bot_active, trust, and channel fields"
        )


def _target_path(config_dir: Path, target: ProposalTarget) -> Path:
    root = config_dir.resolve()
    candidate = root / target.relative
    current = root
    for part in target.relative.parts[:-1]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ProposalError(f"configuration path is not accessible: {current}") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ProposalError(f"configuration path component is not a real directory: {current}")
    try:
        parent = candidate.parent.resolve()
        parent.relative_to(root)
    except ValueError as exc:
        raise ProposalError("configuration target escapes CONFIG_DIR") from exc
    try:
        info = candidate.lstat()
    except FileNotFoundError:
        return candidate
    except OSError as exc:
        raise ProposalError(f"configuration target is not accessible: {candidate}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ProposalError("configuration target must be a regular file or absent")
    return candidate


def _read_file(config_dir: Path, target: ProposalTarget) -> _FileSnapshot:
    path = _target_path(config_dir, target)
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return _FileSnapshot(path, False, "", _sha256(b""))
    except OSError as exc:
        raise ProposalError(f"could not read configuration target {path}") from exc
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProposalError("configuration target is not valid UTF-8") from exc
    return _FileSnapshot(path, True, content, _sha256(data))


class ProposalStore:
    def __init__(self, database: Database) -> None:
        self._db = database

    async def create(
        self,
        *,
        module_name: str,
        guild_id: str,
        target: str,
        content: str,
        base: _FileSnapshot,
        summary: str,
        actor: ProposalActor,
    ) -> ProposalRow:
        now = time.time()
        proposal_id = uuid.uuid4().hex
        content_revision = _sha256(content.encode("utf-8"))
        async with self._db.write_transaction() as conn:
            await conn.execute(
                "INSERT INTO config_proposals ("
                "proposal_id,module_name,guild_id,target,content,content_revision,"
                "base_exists,base_content,base_revision,summary,actor_json,state,"
                "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,'pending',?,?)",
                (
                    proposal_id,
                    module_name,
                    guild_id,
                    target,
                    content,
                    content_revision,
                    int(base.exists),
                    base.content,
                    base.revision,
                    summary,
                    json.dumps(asdict(actor), ensure_ascii=False, sort_keys=True),
                    now,
                    now,
                ),
            )
        row = await self.get(proposal_id)
        if row is None:  # pragma: no cover - protected by the insert
            raise RuntimeError("proposal insert was not readable")
        return row

    async def get(self, proposal_id: str) -> ProposalRow | None:
        async with self._db.conn.execute(
            "SELECT * FROM config_proposals WHERE proposal_id = ?", (proposal_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return None if row is None else _proposal_row(row)

    async def attach_message(self, proposal_id: str, message: MessageRef) -> None:
        async with self._db.write_transaction() as conn:
            cursor = await conn.execute(
                "UPDATE config_proposals SET message_channel_id=?,message_id=?,updated_at=? "
                "WHERE proposal_id=? AND state='pending'",
                (str(message.channel_id), str(message.message_id), time.time(), proposal_id),
            )
            if cursor.rowcount != 1:
                raise ProposalError("proposal was decided before its review message attached")

    async def decide(
        self,
        proposal_id: str,
        state: ProposalState,
        decided_by: str,
        reason: str = "",
    ) -> bool:
        if state not in ("applied", "rejected"):
            raise ValueError(f"invalid terminal proposal state {state!r}")
        async with self._db.write_transaction() as conn:
            cursor = await conn.execute(
                "UPDATE config_proposals SET state=?,decided_by=?,decision_reason=?,"
                "updated_at=? WHERE proposal_id=? AND state='pending'",
                (state, decided_by, reason, time.time(), proposal_id),
            )
        return cursor.rowcount == 1

    async def discard(self, proposal_id: str) -> None:
        async with self._db.write_transaction() as conn:
            await conn.execute(
                "DELETE FROM config_proposals WHERE proposal_id=? AND state='pending'",
                (proposal_id,),
            )

    async def unattached(self) -> tuple[str, ...]:
        async with self._db.conn.execute(
            "SELECT proposal_id FROM config_proposals "
            "WHERE state='pending' AND message_id IS NULL ORDER BY created_at"
        ) as cursor:
            rows = await cursor.fetchall()
        return tuple(str(row[0]) for row in rows)


def _proposal_row(row: Any) -> ProposalRow:
    return ProposalRow(
        proposal_id=str(row["proposal_id"]),
        module_name=str(row["module_name"]),
        guild_id=str(row["guild_id"]),
        target=str(row["target"]),
        content=str(row["content"]),
        content_revision=str(row["content_revision"]),
        base_exists=bool(row["base_exists"]),
        base_content=str(row["base_content"]),
        base_revision=str(row["base_revision"]),
        summary=str(row["summary"]),
        actor=ProposalActor(**json.loads(str(row["actor_json"]))),
        state=cast(ProposalState, str(row["state"])),
        decided_by=None if row["decided_by"] is None else str(row["decided_by"]),
        decision_reason=str(row["decision_reason"]),
        message_channel_id=(
            None if row["message_channel_id"] is None else str(row["message_channel_id"])
        ),
        message_id=None if row["message_id"] is None else str(row["message_id"]),
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
    )


class _ProposalView(ProposalService):
    def __init__(self, service: ConfigProposalService, module_name: str) -> None:
        self._service = service
        self._module_name = module_name

    async def snapshot(self, target: str, *, actor: ProposalActor) -> ConfigSnapshot:
        return await self._service.snapshot(target, actor=actor)

    async def propose(
        self,
        *,
        target: str,
        content: str,
        summary: str,
        actor: ProposalActor,
        expected_revision: str | None = None,
    ) -> ProposalRef:
        return await self._service.propose(
            self._module_name,
            target=target,
            content=content,
            summary=summary,
            actor=actor,
            expected_revision=expected_revision,
        )

    async def get(self, proposal_id: str, *, actor: ProposalActor) -> ProposalRef | None:
        return await self._service.get(proposal_id, actor=actor)


class ConfigProposalService:
    def __init__(self, database: Database, host: ProposalHost) -> None:
        self._store = ProposalStore(database)
        self._host = host
        self._registrations: list[Registration] = []

    def view_for(self, module_name: str) -> ProposalService:
        if module_name not in self._host.known_modules():
            raise ProposalError(f"unknown proposal module {module_name!r}")
        return _ProposalView(self, module_name)

    def install(self, router: InteractionRouter) -> None:
        self._registrations.extend(
            (
                router.register_component(
                    "button", "approve", self._on_approve, min_tier=APPROVER_TIER
                ),
                router.register_component(
                    "button", "reject", self._on_reject, min_tier=APPROVER_TIER
                ),
            )
        )

    async def warn_unattached(self) -> None:
        for proposal_id in await self._store.unattached():
            log.warning("Pending proposal %s has no attached review message", proposal_id)

    async def snapshot(self, target: str, *, actor: ProposalActor) -> ConfigSnapshot:
        resolved, _guild_id = await self._authorized_target(target, actor)
        snapshot = await asyncio.to_thread(_read_file, self._host.config_dir(), resolved)
        return ConfigSnapshot(target.strip(), snapshot.revision, snapshot.content)

    async def get(self, proposal_id: str, *, actor: ProposalActor) -> ProposalRef | None:
        actor_guild = _actor_guild(actor)
        if not _PROPOSAL_ID.fullmatch(proposal_id):
            return None
        row = await self._store.get(proposal_id)
        if row is None or row.guild_id != actor_guild:
            return None
        return row.public_ref()

    async def propose(
        self,
        module_name: str,
        *,
        target: str,
        content: str,
        summary: str,
        actor: ProposalActor,
        expected_revision: str | None = None,
    ) -> ProposalRef:
        known_modules = set(self._host.known_modules())
        if module_name not in known_modules:
            raise ProposalError(f"unknown proposal module {module_name!r}")
        resolved, guild_id = await self._authorized_target(target, actor)
        if resolved.module_name is not None and resolved.module_name not in known_modules:
            raise ProposalError(f"unknown guild module {resolved.module_name!r}")
        validate_content(resolved, content)
        clean_summary = summary.strip()
        if not clean_summary:
            raise ProposalError("proposal summary must not be empty")
        if len(clean_summary) > SUMMARY_MAX_CHARS:
            raise ProposalError(f"proposal summary must be {SUMMARY_MAX_CHARS} characters or fewer")
        base = await asyncio.to_thread(_read_file, self._host.config_dir(), resolved)
        if expected_revision is not None and expected_revision != base.revision:
            raise ProposalError("configuration changed since it was inspected")
        review_channel = self._host.review_channel_id(guild_id) or str(actor.channel_id or "")
        review_channel = _positive_id(review_channel, label="proposal review channel")
        channel_guild = await self._host.channel_guild_id(int(review_channel))
        if channel_guild is None or str(channel_guild) != guild_id:
            raise ProposalError("proposal review channel must belong to the actor's guild")
        row = await self._store.create(
            module_name=module_name,
            guild_id=guild_id,
            target=target.strip(),
            content=content,
            base=base,
            summary=clean_summary,
            actor=actor,
        )
        components = (
            ButtonSpec("approve", "Approve", "success", (row.proposal_id,)),
            ButtonSpec("reject", "Reject", "danger", (row.proposal_id,)),
        )
        try:
            message = await self._host.post_review(
                int(review_channel), embed=render(row), components=components
            )
        except Exception as exc:
            await self._store.discard(row.proposal_id)
            raise ProposalError("posting the proposal review card failed") from exc
        await self._store.attach_message(row.proposal_id, message)
        attached = await self._store.get(row.proposal_id)
        if attached is None:  # pragma: no cover - protected by attach_message
            raise RuntimeError("attached proposal disappeared")
        return attached.public_ref()

    async def _authorized_target(
        self, target: str, actor: ProposalActor
    ) -> tuple[ProposalTarget, str]:
        actor_guild = _actor_guild(actor)
        resolved = resolve_target(target)
        target_guild = resolved.guild_id
        if resolved.channel_id is not None:
            channel_guild = await self._host.channel_guild_id(int(resolved.channel_id))
            target_guild = None if channel_guild is None else str(channel_guild)
        if target_guild is None or target_guild != actor_guild:
            raise ProposalError("proposal target must belong to the actor's guild")
        return resolved, actor_guild

    async def _on_approve(self, interaction: ModuleInteraction) -> None:
        await interaction.defer()
        proposal_id = _interaction_proposal_id(interaction)
        async with _DECISION_LOCK:
            row = await self._store.get(proposal_id)
            if row is None:
                await interaction.follow_up("Proposal not found.", ephemeral=True)
                return
            if str(interaction.guild_id) != row.guild_id:
                await interaction.follow_up(
                    "This proposal belongs to another guild.", ephemeral=True
                )
                return
            if row.state != "pending":
                await interaction.follow_up(f"Proposal is already {row.state}.", ephemeral=True)
                await interaction.edit_original(embed=render(row), components=())
                return
            await self._approve_pending(interaction, row)

    async def _approve_pending(self, interaction: ModuleInteraction, row: ProposalRow) -> None:
        target = resolve_target(row.target)
        validate_content(target, row.content)
        live = await asyncio.to_thread(_read_file, self._host.config_dir(), target)
        if live.revision == row.base_revision:
            await asyncio.to_thread(atomic_write_text, live.path, row.content)
        elif live.revision != row.content_revision:
            reason = "configuration changed since proposal"
            await self._store.decide(row.proposal_id, "rejected", str(interaction.user_id), reason)
            decided = await self._required(row.proposal_id)
            await interaction.follow_up("Not applied: configuration changed.", ephemeral=True)
            await interaction.edit_original(embed=render(decided), components=())
            return
        failure = await self._refresh_and_verify(row)
        if failure:
            rollback = await self._restore_baseline(row, target)
            message = f"Not applied: {failure}"
            if rollback:
                message += f"; {rollback}"
            await interaction.follow_up(message, ephemeral=True)
            return
        changed = await self._store.decide(row.proposal_id, "applied", str(interaction.user_id))
        decided = await self._required(row.proposal_id)
        if not changed and decided.state != "applied":
            await interaction.follow_up(
                f"Proposal was concurrently decided as {decided.state}.", ephemeral=True
            )
        await interaction.edit_original(embed=render(decided), components=())

    async def _on_reject(self, interaction: ModuleInteraction) -> None:
        await interaction.defer()
        proposal_id = _interaction_proposal_id(interaction)
        async with _DECISION_LOCK:
            row = await self._store.get(proposal_id)
            if row is None:
                await interaction.follow_up("Proposal not found.", ephemeral=True)
                return
            if str(interaction.guild_id) != row.guild_id:
                await interaction.follow_up(
                    "This proposal belongs to another guild.", ephemeral=True
                )
                return
            if row.state != "pending":
                await interaction.follow_up(f"Proposal is already {row.state}.", ephemeral=True)
                await interaction.edit_original(embed=render(row), components=())
                return
            target = resolve_target(row.target)
            live = await asyncio.to_thread(_read_file, self._host.config_dir(), target)
            if row.content_revision != row.base_revision and live.revision == row.content_revision:
                failure = await self._refresh_and_verify(row)
                if not failure:
                    await self._store.decide(
                        row.proposal_id,
                        "applied",
                        str(interaction.user_id),
                        "recovered an interrupted approval",
                    )
                    decided = await self._required(row.proposal_id)
                    await interaction.follow_up(
                        "The fragment was already applied; its state was recovered.",
                        ephemeral=True,
                    )
                    await interaction.edit_original(embed=render(decided), components=())
                    return
                rollback = await self._restore_baseline(row, target)
                if rollback:
                    await interaction.follow_up(
                        f"Could not reject safely: {rollback}", ephemeral=True
                    )
                    return
            await self._store.decide(row.proposal_id, "rejected", str(interaction.user_id))
            decided = await self._required(row.proposal_id)
            await interaction.edit_original(embed=render(decided), components=())

    async def _refresh_and_verify(self, row: ProposalRow) -> str:
        try:
            await self._host.on_applied(int(row.guild_id))
            return self._host.verify_guild(int(row.guild_id))
        except Exception as exc:
            log.exception("Refreshing proposal %s failed", row.proposal_id)
            return str(exc) or "refreshing the guild configuration failed"

    async def _restore_baseline(self, row: ProposalRow, target: ProposalTarget) -> str:
        try:
            live = await asyncio.to_thread(_read_file, self._host.config_dir(), target)
            if live.revision != row.content_revision:
                return "the file changed again before rollback and was left untouched"
            if row.base_exists:
                await asyncio.to_thread(atomic_write_text, live.path, row.base_content)
            else:
                await asyncio.to_thread(live.path.unlink, missing_ok=True)
            await self._host.on_applied(int(row.guild_id))
        except Exception as exc:
            log.exception("Rolling proposal %s back failed", row.proposal_id)
            return f"rollback failed: {exc}"
        return ""

    async def _required(self, proposal_id: str) -> ProposalRow:
        row = await self._store.get(proposal_id)
        if row is None:  # pragma: no cover - caller just loaded/updated it
            raise RuntimeError(f"proposal {proposal_id} disappeared")
        return row


def _actor_guild(actor: ProposalActor) -> str:
    return _positive_id(str(actor.guild_id or ""), label="proposal actor guild")


def _interaction_proposal_id(interaction: ModuleInteraction) -> str:
    parsed = parse_custom_id(str(interaction.custom_id or ""))
    if parsed is None or parsed[0] != ROUTER_NAME or len(parsed[2]) != 1:
        raise ProposalError("invalid proposal control id")
    proposal_id = parsed[2][0]
    if not _PROPOSAL_ID.fullmatch(proposal_id):
        raise ProposalError("invalid proposal id")
    return proposal_id


def render(row: ProposalRow) -> OutgoingEmbed:
    title = {
        "pending": "Configuration proposal",
        "applied": "Configuration proposal applied",
        "rejected": "Configuration proposal rejected",
    }[row.state]
    color = {
        "pending": _COLOR_PENDING,
        "applied": _COLOR_APPLIED,
        "rejected": _COLOR_REJECTED,
    }[row.state]
    preview = "\n".join(row.content.splitlines()[:15]).replace("```", "``\u200b`")
    preview = _truncate(preview or "(empty fragment)", FIELD_VALUE_MAX - 8)
    fields: list[tuple[str, str, bool]] = [
        ("Target", _truncate(row.target, FIELD_VALUE_MAX), True),
        ("Proposed by", f"<@{row.actor.user_id}>", True),
        ("Preview", f"```yaml\n{preview}\n```", False),
    ]
    if row.decided_by:
        decision = f"<@{row.decided_by}>"
        if row.decision_reason:
            decision += f" - {row.decision_reason}"
        fields.append(("Decision", _truncate(decision, FIELD_VALUE_MAX), False))
    description = _truncate(row.summary, 1_000)
    total = (
        len(title)
        + len(description)
        + sum(len(name) + len(value) for name, value, _inline in fields)
    )
    if total > TOTAL_MAX:
        description = _truncate(description, max(0, len(description) - (total - TOTAL_MAX)))
    return OutgoingEmbed(
        title=title,
        description=description,
        color=color,
        fields=tuple(fields),
        footer=f"Proposal {row.proposal_id}",
        timestamp=True,
    )


def _truncate(text: str, limit: int) -> str:
    clean = text.strip()
    if len(clean) <= limit:
        return clean
    if limit <= 1:
        return clean[:limit]
    return clean[: limit - 1].rstrip() + "…"


__all__ = [
    "APPROVER_TIER",
    "CONTENT_MAX_BYTES",
    "ROUTER_NAME",
    "SUMMARY_MAX_CHARS",
    "ConfigProposalService",
    "ProposalHost",
    "ProposalRow",
    "ProposalStore",
    "ProposalTarget",
    "render",
    "resolve_target",
    "validate_content",
]
