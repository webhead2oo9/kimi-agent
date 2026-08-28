"""The module's lifecycle object: everything that runs after the host starts it.

Layout:

- ``start()`` binds the module to ``ModuleRuntimeContext``. Everything it
  registers (commands, components, subscriptions, services) returns a
  ``Registration`` that ``close()`` releases in reverse order.
- ``_give()`` holds the business rule shared by the LLM tool, the slash
  command, and the button.
- The remaining methods are one per surface: LLM tools, ``/kudos`` commands,
  the persistent "Thank back" button, the scheduled digest, the
  ``discord.member_remove`` subscription, the provided service, and the
  guild-settings change hook.

Module code is trusted and runs in the host process. The ports are a
reviewable contract: every Discord action, event topic, and service the module
uses is declared on ``ModuleSpec`` and listed by ``/modules manifest``.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from kimi_agent_module_api import (
    ModuleRuntimeContext,
    ModuleToolContext,
    ProposalActor,
    ProposalError,
    ScopedModuleMigration,
    TrustTier,
)
from kimi_agent_module_api.contracts import (
    ButtonSpec,
    CommandOption,
    CommandSpec,
    Event,
    JobRun,
    ModuleInteraction,
    OutgoingEmbed,
    Registration,
    TrustTierName,
    parse_custom_id,
)
from kimi_agent_module_api.events import TOPIC_MEMBER_REMOVE, MemberRemoveEvent

from community_agent_reference_module.guild_settings import (
    FIELD_ALLOW_SELF_THANKS,
    FIELD_DIGEST_CHANNEL,
    FIELD_GIVER_TIER,
)
from community_agent_reference_module.ledger import DAY_SECONDS, BoardEntry, Kudos, KudosLedger
from community_agent_reference_module.migrations import MIGRATIONS
from community_agent_reference_module.settings import KudosSettings

log = logging.getLogger(__name__)

MODULE_NAME = "reference_kudos"

# Names that other code refers to. Keeping them here (and importing them in
# tests) means a rename is one edit.
TOOL_GIVE = "give_kudos"
TOOL_LEADERBOARD = "kudos_leaderboard"
COMMAND_GROUP = "kudos"
BUTTON_THANK_BACK = "thank_back"
DIGEST_JOB_KEY = "digest"
DIGEST_HANDLER = "post_digest"
SERVICE_NAME = "kudos.board"
SERVICE_VERSION = 1
# A module may publish only under its own namespace, ``<module_name>.*``.
TOPIC_GIVEN = f"{MODULE_NAME}.given"

MAX_REASON_LENGTH = 200
DIGEST_JITTER_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class KudosGivenEvent:
    """Payload of ``reference_kudos.given``. Plain data: subscribers may live in other modules."""

    guild_id: int
    giver_id: int
    receiver_id: int
    reason: str
    kudos_id: int


class KudosRefused(Exception):
    """A rule declined the kudos; the message is safe to show to the person who asked."""


class KudosBoardService:
    """What ``ctx.services.get("kudos.board", 1)`` returns to a consuming module.

    Keep a provided service small and data-only. The consumer receives a proxy
    that raises ``ServiceUnavailable`` once this module closes, so nothing here
    should hand out live objects that outlive the module.
    """

    def __init__(self, ledger: KudosLedger, clock: Callable[[], float]) -> None:
        self._ledger = ledger
        self._clock = clock

    async def leaderboard(self, guild_id: int, *, days: int, limit: int) -> list[BoardEntry]:
        since = self._clock() - days * DAY_SECONDS
        return await self._ledger.top(guild_id, since=since, limit=limit)


class KudosModule:
    """Implements the ``AppModule`` protocol: ``scoped_migrations``, ``start``, ``close``."""

    scoped_migrations: Sequence[ScopedModuleMigration] = MIGRATIONS

    def __init__(self, settings: KudosSettings, *, clock: Callable[[], float] = time.time) -> None:
        self._settings = settings
        # Injectable clock so tests can drive the daily limit and digest windows.
        self._clock = clock
        self._ctx: ModuleRuntimeContext | None = None
        self._ledger: KudosLedger | None = None
        self._registrations: list[Registration] = []
        self._digest_failures = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self, ctx: ModuleRuntimeContext) -> None:
        """Bind to the runtime. Raising here aborts bot startup."""
        self._ctx = ctx
        self._ledger = KudosLedger(ctx.storage)

        # Durable background work. The handler is bound by name each start
        # because the job row outlives the process; ``run_every`` with the same
        # key replaces the persisted schedule, so a changed interval takes
        # effect on the next restart.
        ctx.scheduler.register(DIGEST_HANDLER, self._post_digest)
        await ctx.scheduler.run_every(
            DIGEST_JOB_KEY,
            float(self._settings.digest_interval_seconds),
            DIGEST_HANDLER,
            jitter_seconds=DIGEST_JITTER_SECONDS,
        )

        # Core events. The topic is declared in ``ModulePermissions.event_topics``;
        # subscribing to an undeclared topic raises at this line.
        self._registrations.append(
            ctx.events.subscribe(TOPIC_MEMBER_REMOVE, self._on_member_remove)
        )

        # Slash commands, grouped as ``/kudos give|top|setup``.
        interactions = ctx.interactions
        self._registrations.extend(
            (
                interactions.add_command(
                    CommandSpec(
                        name="give",
                        description="Thank a member for something they did.",
                        group=COMMAND_GROUP,
                        group_description="Member-to-member recognition.",
                        # Everyone may run the command; the guild's own
                        # ``giver_min_tier`` is enforced inside ``_give``.
                        min_tier="member",
                        options=(
                            CommandOption("member", "user", "Who deserves it.", required=True),
                            CommandOption("reason", "string", "What they did.", required=True),
                        ),
                    ),
                    self._command_give,
                ),
                interactions.add_command(
                    CommandSpec(
                        name="top",
                        description="Show who received the most kudos recently.",
                        group=COMMAND_GROUP,
                        min_tier="member",
                        options=(
                            CommandOption(
                                "days",
                                "integer",
                                "Window in days.",
                                min_value=1,
                                max_value=365,
                            ),
                        ),
                    ),
                    self._command_top,
                ),
                interactions.add_command(
                    CommandSpec(
                        name="setup",
                        description="Propose the channel that receives the kudos digest.",
                        group=COMMAND_GROUP,
                        min_tier="staff",
                        options=(
                            CommandOption("channel", "channel", "Digest channel.", required=True),
                        ),
                    ),
                    self._command_setup,
                ),
                # A persistent button. The host routes clicks by the key that
                # ``ButtonSpec`` names, so a button posted before a restart keeps
                # working as long as this registration happens on every start.
                interactions.register_component(
                    "button", BUTTON_THANK_BACK, self._button_thank_back, min_tier="member"
                ),
            )
        )

        # A service other modules may consume. Declared in ``ModuleSpec.provides``;
        # a module that declares a service and never provides it is marked degraded.
        self._registrations.append(
            ctx.services.provide(
                SERVICE_NAME, SERVICE_VERSION, KudosBoardService(self._ledger, self._clock)
            )
        )

        if ctx.guild_settings is not None:
            self._registrations.append(ctx.guild_settings.on_change(self._on_guild_change))

        # The host reports ``healthy`` after a clean ``start()`` anyway; reporting
        # explicitly is how a module attaches its own metrics from the outset.
        self._report_health()

    async def close(self) -> None:
        """Release everything ``start()`` acquired, newest first."""
        for registration in reversed(self._registrations):
            registration.close()
        self._registrations.clear()
        self._ledger = None
        self._ctx = None

    # ------------------------------------------------------------------
    # The business rule, shared by every entry point
    # ------------------------------------------------------------------

    async def _give(
        self,
        guild_id: int,
        giver_id: int,
        receiver_id: int,
        reason: str,
        *,
        giver_tier: TrustTierName,
    ) -> Kudos:
        """Record one kudos or raise ``KudosRefused`` with a user-facing reason."""
        ctx, ledger = self._require_started()
        if ctx.guild_settings is not None and not ctx.guild_settings.is_enabled(guild_id):
            raise KudosRefused("Kudos are not enabled in this server.")

        values = ctx.guild_settings.get(guild_id).values if ctx.guild_settings else {}
        required = TrustTier(str(values.get(FIELD_GIVER_TIER) or "member"))
        if TrustTier(giver_tier) < required:
            raise KudosRefused(f"Only {required.value} members and above may give kudos here.")

        if giver_id == receiver_id:
            raise KudosRefused("You cannot give kudos to yourself.")
        reason = " ".join(reason.split())
        if not reason:
            raise KudosRefused("Say what the kudos is for.")
        if len(reason) > MAX_REASON_LENGTH:
            raise KudosRefused(f"Keep the reason under {MAX_REASON_LENGTH} characters.")

        now = self._clock()
        given = await ledger.given_recently(guild_id, giver_id, now)
        if given >= self._settings.daily_limit:
            raise KudosRefused(
                f"You have given {self._settings.daily_limit} kudos in the last 24 hours."
            )

        kudos = await ledger.give(guild_id, giver_id, receiver_id, reason, now)
        # Fire-and-forget: delivery is asynchronous and events are not durable.
        ctx.events.publish(
            TOPIC_GIVEN, KudosGivenEvent(guild_id, giver_id, receiver_id, reason, kudos.id)
        )
        return kudos

    # ------------------------------------------------------------------
    # LLM tools (registered at load time in spec.py)
    # ------------------------------------------------------------------

    async def tool_give(self, arguments: dict[str, Any], tool_ctx: ModuleToolContext) -> str:
        """``give_kudos``: the model gives kudos on behalf of the person talking to it."""
        # ``guild_id`` is None in DMs and in personal chat. Kudos belong to a
        # guild, so a guild-less caller is refused.
        if tool_ctx.guild_id is None:
            return "Kudos can only be given inside a server."
        receiver_id = _parse_user_id(arguments.get("user"))
        if receiver_id is None:
            return "Provide the recipient as a numeric Discord user id or an @mention."
        try:
            kudos = await self._give(
                int(tool_ctx.guild_id),
                int(tool_ctx.user_id),
                receiver_id,
                str(arguments.get("reason") or ""),
                giver_tier=tool_ctx.trust_tier.value,
            )
        except KudosRefused as refused:
            return str(refused)
        return _summary(kudos)

    async def tool_leaderboard(self, arguments: dict[str, Any], tool_ctx: ModuleToolContext) -> str:
        """``kudos_leaderboard``: a read-only view the model can quote."""
        if tool_ctx.guild_id is None:
            return "The kudos leaderboard is only available inside a server."
        days = _clamp_days(arguments.get("days"))
        entries = await self._leaderboard(int(tool_ctx.guild_id), days)
        if not entries:
            return f"Nobody has received kudos in the last {days} day(s)."
        lines = [f"{rank}. <@{e.user_id}> — {e.count}" for rank, e in enumerate(entries, 1)]
        return f"Kudos received in the last {days} day(s):\n" + "\n".join(lines)

    # ------------------------------------------------------------------
    # Slash commands
    # ------------------------------------------------------------------

    async def _command_give(self, interaction: ModuleInteraction) -> None:
        ctx, _ = self._require_started()
        # ``user`` options arrive as stable ids.
        receiver_id = int(interaction.options["member"])
        # ``ctx.trust`` is the same read-only lookup core uses for its own tiers.
        tier = await ctx.trust.tier(interaction.guild_id, interaction.user_id)
        try:
            kudos = await self._give(
                interaction.guild_id,
                interaction.user_id,
                receiver_id,
                str(interaction.options.get("reason") or ""),
                giver_tier=tier,
            )
        except KudosRefused as refused:
            await interaction.respond(str(refused), ephemeral=True)
            return
        await interaction.respond(
            _summary(kudos),
            components=(
                ButtonSpec(
                    key=BUTTON_THANK_BACK,
                    label="Thank back",
                    style="success",
                    # ``parts`` ride inside the custom id, so the handler can
                    # find the original kudos after a restart with no memory.
                    parts=(str(kudos.id),),
                ),
            ),
        )

    async def _command_top(self, interaction: ModuleInteraction) -> None:
        days = _clamp_days(interaction.options.get("days"))
        entries = await self._leaderboard(interaction.guild_id, days)
        await interaction.respond(embed=self._board_embed(entries, days), ephemeral=not entries)

    async def _command_setup(self, interaction: ModuleInteraction) -> None:
        """Propose a guild-settings change for staff approval.

        Modules do not write below ``CONFIG_DIR``. The proposal port records the
        exact current document as a rollback baseline, posts a review card with
        staff-only Approve/Reject buttons, and applies the change on approval.
        """
        ctx, _ = self._require_started()
        if ctx.proposals is None:
            await interaction.respond("Configuration proposals are unavailable.", ephemeral=True)
            return
        guild_id = interaction.guild_id
        actor = ProposalActor(
            user_id=str(interaction.user_id),
            source=f"{MODULE_NAME}:setup",
            guild_id=str(guild_id),
            channel_id=str(interaction.channel_id),
        )
        target = f"guild:{guild_id}:{MODULE_NAME}"
        current = ctx.guild_settings.get(guild_id).values if ctx.guild_settings else {}
        proposed = {**current, FIELD_DIGEST_CHANNEL: int(interaction.options["channel"])}
        try:
            snapshot = await ctx.proposals.snapshot(target, actor=actor)
            ref = await ctx.proposals.propose(
                target=target,
                content=_render_guild_document(proposed),
                summary=f"Post the kudos digest in <#{proposed[FIELD_DIGEST_CHANNEL]}>",
                actor=actor,
                # Refuse to clobber an edit made between the snapshot and the proposal.
                expected_revision=snapshot.revision,
            )
        except ProposalError as error:
            await interaction.respond(f"Could not propose the change: {error}", ephemeral=True)
            return
        await interaction.respond(
            f"Proposed (`{ref.proposal_id}`). Staff can approve it from the review card.",
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    # Persistent button
    # ------------------------------------------------------------------

    async def _button_thank_back(self, interaction: ModuleInteraction) -> None:
        ctx, ledger = self._require_started()
        parsed = parse_custom_id(interaction.custom_id or "")
        original = await ledger.get(int(parsed[2][0])) if parsed and parsed[2] else None
        if original is None or original.guild_id != interaction.guild_id:
            await interaction.respond("That kudos is no longer available.", ephemeral=True)
            return
        values = ctx.guild_settings.get(interaction.guild_id).values if ctx.guild_settings else {}
        allow_self = bool(values.get(FIELD_ALLOW_SELF_THANKS, False))
        if interaction.user_id != original.receiver_id and not (
            allow_self and interaction.user_id == original.giver_id
        ):
            await interaction.respond("Only the person thanked can thank back.", ephemeral=True)
            return
        tier = await ctx.trust.tier(interaction.guild_id, interaction.user_id)
        try:
            kudos = await self._give(
                interaction.guild_id,
                interaction.user_id,
                original.giver_id,
                "thanks for the kudos!",
                giver_tier=tier,
            )
        except KudosRefused as refused:
            await interaction.respond(str(refused), ephemeral=True)
            return
        await interaction.respond(_summary(kudos))

    # ------------------------------------------------------------------
    # Scheduled digest
    # ------------------------------------------------------------------

    async def _post_digest(self, run: JobRun) -> None:
        """Post the leaderboard to every guild that configured a digest channel.

        Raising out of a job handler makes the scheduler back the job off and
        retry, which is right for a transient outage but wrong for one guild's
        bad channel. So failures are per guild: logged, counted into the health
        metrics, and the job itself succeeds.
        """
        ctx, ledger = self._require_started()
        if ctx.guild_settings is None:
            return
        window = self._settings.digest_interval_seconds
        since = self._clock() - window
        posted = 0
        failed = 0
        for guild_id in ctx.guild_settings.guild_ids():
            if not ctx.guild_settings.is_enabled(guild_id):
                continue
            channel_id = ctx.guild_settings.get(guild_id).values.get(FIELD_DIGEST_CHANNEL)
            if not channel_id:
                continue
            entries = await ledger.top(guild_id, since=since, limit=self._settings.board_size)
            if not entries:
                continue
            days = max(1, round(window / DAY_SECONDS))
            try:
                # ``send_message`` is declared in ``permissions.discord_actions``;
                # the port raises ``UndeclaredDiscordAction`` for anything else.
                await ctx.discord.send_message(
                    int(channel_id), embed=self._board_embed(entries, days)
                )
                posted += 1
            except Exception:
                failed += 1
                log.exception(
                    "kudos digest failed for guild %s (attempt %s)", guild_id, run.attempt
                )
        self._digest_failures = failed
        self._report_health(digest_posted=posted)

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    async def _on_member_remove(self, event: Event) -> None:
        """Drop a departed member's rows: data minimization, and the ids mean nothing now."""
        payload = event.payload
        if not isinstance(payload, MemberRemoveEvent):
            return
        _, ledger = self._require_started()
        removed = await ledger.forget_member(payload.guild_id, payload.user_id)
        if removed:
            log.info(
                "forgot %s kudos rows for departed member in guild %s", removed, payload.guild_id
            )

    def _on_guild_change(self, guild_id: int) -> None:
        """Called after a guild document changes (including an approved proposal)."""
        log.info("kudos guild settings changed for guild %s", guild_id)
        self._report_health()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _require_started(self) -> tuple[ModuleRuntimeContext, KudosLedger]:
        if self._ctx is None or self._ledger is None:
            raise RuntimeError(f"{MODULE_NAME} is not started")
        return self._ctx, self._ledger

    async def _leaderboard(self, guild_id: int, days: int) -> list[BoardEntry]:
        _, ledger = self._require_started()
        since = self._clock() - days * DAY_SECONDS
        return await ledger.top(guild_id, since=since, limit=self._settings.board_size)

    def _board_embed(self, entries: Sequence[BoardEntry], days: int) -> OutgoingEmbed:
        if not entries:
            description = f"Nobody has received kudos in the last {days} day(s)."
        else:
            description = "\n".join(
                f"**{rank}.** <@{entry.user_id}> — {entry.count}"
                for rank, entry in enumerate(entries, 1)
            )
        return OutgoingEmbed(
            title=f"Kudos, last {days} day(s)",
            description=description,
            footer=f"Top {self._settings.board_size}",
            timestamp=True,
        )

    def _report_health(self, *, digest_posted: int | None = None) -> None:
        """Two independent concerns: overall metrics, and the digest's own state.

        The keyed report is tracked separately by the host, so a later unkeyed
        ``healthy`` (say, after a guild-settings change) does not erase a
        ``degraded`` digest. The module shows as the worst of the two.
        """
        ctx, _ = self._require_started()
        metrics: dict[str, float] = {}
        if ctx.guild_settings is not None:
            metrics["guilds"] = float(len(ctx.guild_settings.guild_ids()))
        ctx.health.report("healthy", "", metrics)
        if digest_posted is None:
            return
        digest_metrics = {
            "digest_posted": float(digest_posted),
            "digest_failures": float(self._digest_failures),
        }
        if self._digest_failures:
            detail = f"{self._digest_failures} guild digest(s) failed"
            ctx.health.report("degraded", detail, digest_metrics, key="digest")
        else:
            ctx.health.report("healthy", "", digest_metrics, key="digest")


def _summary(kudos: Kudos) -> str:
    return f"Kudos to <@{kudos.receiver_id}>: {kudos.reason}"


def _parse_user_id(raw: Any) -> int | None:
    """Accept ``123``, ``"123"``, ``<@123>`` or ``<@!123>``; reject anything else."""
    token = str(raw or "").strip()
    if token.startswith("<@") and token.endswith(">"):
        token = token[2:-1].lstrip("!")
    return int(token) if token.isdecimal() else None


def _clamp_days(raw: Any, *, default: int = 30) -> int:
    try:
        days = int(raw) if raw is not None else default
    except TypeError, ValueError:
        return default
    return min(max(days, 1), 365)


def _render_guild_document(values: dict[str, Any]) -> str:
    """Frontmatter-only document in the shape the host stores under ``guild-modules/``."""
    lines = ["---"]
    for key in sorted(values):
        value = values[key]
        rendered = str(value).lower() if isinstance(value, bool) else str(value)
        lines.append(f"{key}: {rendered}")
    lines.append("---")
    return "\n".join(lines) + "\n"


__all__ = [
    "BUTTON_THANK_BACK",
    "COMMAND_GROUP",
    "DIGEST_HANDLER",
    "DIGEST_JOB_KEY",
    "MODULE_NAME",
    "SERVICE_NAME",
    "SERVICE_VERSION",
    "TOOL_GIVE",
    "TOOL_LEADERBOARD",
    "TOPIC_GIVEN",
    "KudosBoardService",
    "KudosGivenEvent",
    "KudosModule",
    "KudosRefused",
]
