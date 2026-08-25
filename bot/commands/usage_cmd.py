from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta, UTC

import discord
from discord import app_commands
from discord.ext import commands

from commands._shared import defer_if_needed, send_message
from storage.usage import SpenderRow, UsageAggregate, UsageStore
from trust.resolver import TrustResolver
from trust.tiers import TrustTier

_TODAY = "Today"
_WINDOWS = {
    "Last 5h": timedelta(hours=5),
    "Last 7d": timedelta(days=7),
    "Last 30d": timedelta(days=30),
}
_SERVER_WINDOW = timedelta(days=30)
# Wide enough for a full Discord user id, the fallback when a name is missing.
_MAX_NAME_CHARS = 20


def format_user_usage(
    label: str,
    windows: dict[str, UsageAggregate],
    scope_note: str = "",
) -> str:
    """One member's spend, one row per window, widest window last."""
    show_paid = any(aggregate.paid_tool_cost_usd > 0 for aggregate in windows.values())
    header = ["Window", "Est. cost", *(["Paid"] if show_paid else []), "Tokens", "Turns"]
    rows = [
        _spend_row(
            name,
            cost=aggregate.est_cost_usd,
            paid=aggregate.paid_tool_cost_usd,
            tokens=_total_tokens(aggregate),
            turns=aggregate.turns,
            show_paid=show_paid,
        )
        for name, aggregate in windows.items()
    ]
    widest = _widest_window(windows)
    notes = _notes(
        show_paid=show_paid,
        unpriced=widest[1].unpriced_llm_calls if widest else 0,
        window=widest[0] if widest else "",
    )
    return _panel(f"Usage for {_plain(label)}{scope_note}", header, rows, notes)


def format_server_usage(
    total: UsageAggregate,
    spenders: list[SpenderRow],
) -> str:
    """The server total plus the ranked spenders behind it."""
    show_paid = total.paid_tool_cost_usd > 0 or any(
        spender.paid_tool_cost_usd > 0 for spender in spenders
    )
    header = ["", "Est. cost", *(["Paid"] if show_paid else []), "Tokens", "Turns"]
    rows = [
        _spend_row(
            "Server total",
            cost=total.est_cost_usd,
            paid=total.paid_tool_cost_usd,
            tokens=_total_tokens(total),
            turns=total.turns,
            show_paid=show_paid,
        ),
    ]
    if spenders:
        rows.append(_caption_row("", len(header)))
        rows.append(_caption_row("Top spenders", len(header)))
        rows.extend(
            _spend_row(
                f"{rank}. {_plain(spender.user_name or spender.user_id, _MAX_NAME_CHARS)}",
                cost=spender.est_cost_usd,
                paid=spender.paid_tool_cost_usd,
                tokens=spender.total_tokens,
                turns=spender.turns,
                show_paid=show_paid,
            )
            for rank, spender in enumerate(spenders, start=1)
        )
    notes = _notes(
        show_paid=show_paid,
        unpriced=total.unpriced_llm_calls,
        window="Last 30d",
    )
    if not spenders:
        notes.insert(0, "No member spend recorded in this window.")
    return _panel("Server usage, last 30d", header, rows, notes)


def register_usage_command(
    bot: commands.Bot,
    store: UsageStore,
    trust_resolver: TrustResolver,
) -> None:
    @app_commands.command(name="usage", description="View LLM usage and paid tool costs")
    @app_commands.describe(user="Staff: user to inspect; omit for server totals")
    async def usage(
        interaction: discord.Interaction,
        user: discord.Member | None = None,
    ) -> None:
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        guild_id = str(interaction.guild_id) if interaction.guild_id else None
        is_staff = (
            trust_resolver.resolve(member, str(interaction.user.id), guild_id) >= TrustTier.STAFF
        )
        if not is_staff and user is not None:
            await send_message(interaction, "Staff only for other-user usage.")
            return

        # The aggregate queries queue on the shared DB connection; ack within
        # Discord's 3-second window before running them.
        await defer_if_needed(interaction)
        now = datetime.now(UTC)
        if user is not None or not is_staff:
            target = user if user is not None else interaction.user
            windows = {
                name: await store.user_total(str(target.id), since, guild_id)
                for name, since in _user_window_cutoffs(now).items()
            }
            scope_note = " (this server)" if guild_id is not None else ""
            await send_message(
                interaction,
                format_user_usage(target.display_name, windows, scope_note),
            )
            return

        since = now - _SERVER_WINDOW
        total = await store.server_total(guild_id, since)
        spenders = await store.top_spenders(guild_id, since, limit=10)
        await send_message(
            interaction,
            format_server_usage(total, spenders),
        )

    bot.tree.add_command(usage, override=True)


def _total_tokens(aggregate: UsageAggregate) -> int:
    return (
        aggregate.input_tokens
        + aggregate.cached_read_tokens
        + aggregate.cache_write_tokens
        + aggregate.output_tokens
    )


def _spend_row(
    label: str,
    *,
    cost: float,
    paid: float,
    tokens: int,
    turns: int,
    show_paid: bool,
) -> list[str]:
    return [
        label,
        _money(cost),
        *([_money(paid)] if show_paid else []),
        _tokens(tokens),
        f"{turns:,}",
    ]


def _caption_row(label: str, columns: int) -> list[str]:
    """A caption spanning the table; the blank cells are trimmed on render."""
    return [label, *[""] * (columns - 1)]


def _money(value: float) -> str:
    if 0 < value < 0.1:
        precise = f"{value:,.6f}".rstrip("0").rstrip(".")
        return f"${precise}"
    return f"${value:,.2f}"


def _tokens(count: int) -> str:
    """Compact counts: an exact token total is noise beside the cost."""
    thousands = count / 1_000
    if count < 1_000:
        return str(count)
    if thousands < 10:
        return f"{thousands:.1f}K"
    if thousands < 999.5:
        return f"{thousands:.0f}K"
    return f"{count / 1_000_000:.1f}M"


def _plain(value: str, limit: int = 32) -> str:
    """Keep a display name from breaking out of the code block."""
    cleaned = value.replace("`", "").replace("\n", " ").strip()
    if len(cleaned) > limit:
        return cleaned[: limit - 1] + "…"
    return cleaned or "unknown"


def _notes(*, show_paid: bool, unpriced: int, window: str) -> list[str]:
    notes = []
    if show_paid:
        notes.append("Paid is billed tool spend, already counted in est. cost.")
    if unpriced > 0:
        noun = "call" if unpriced == 1 else "calls"
        notes.append(
            f"{unpriced} LLM {noun} couldn't be priced in {window.lower()}, "
            "so the estimated total may be lower than the actual cost."
        )
    return notes


def _panel(
    title: str,
    header: Sequence[str],
    rows: Sequence[Sequence[str]],
    notes: Sequence[str],
) -> str:
    body = "\n".join([title, "", *_aligned(header, rows)])
    return "\n".join([f"```\n{body}\n```", *notes])


def _aligned(header: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    lines = [header, *rows]
    widths = [max(len(line[index]) for line in lines) for index in range(len(header))]
    return [_aligned_line(line, widths) for line in lines]


def _aligned_line(cells: Sequence[str], widths: Sequence[int]) -> str:
    # Labels read down the left edge; numbers right-align so the digits stack.
    rendered = [cells[0].ljust(widths[0])]
    rendered.extend(cell.rjust(width) for cell, width in zip(cells[1:], widths[1:], strict=True))
    return "  ".join(rendered).rstrip()


def _widest_window(
    windows: dict[str, UsageAggregate],
) -> tuple[str, UsageAggregate] | None:
    """The last window is the widest, so its notes cover every row above it."""
    if not windows:
        return None
    return next(reversed(windows.items()))


def _user_window_cutoffs(now: datetime) -> dict[str, datetime]:
    return {
        _TODAY: now.replace(hour=0, minute=0, second=0, microsecond=0),
        **{name: now - delta for name, delta in _WINDOWS.items()},
    }
