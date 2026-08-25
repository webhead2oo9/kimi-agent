from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest

from commands.usage_cmd import format_server_usage, format_user_usage, register_usage_command
from storage.usage import SpenderRow, UsageAggregate
from trust.resolver import TrustResolver


def _agg(
    cost: float,
    turns: int = 2,
    unpriced_llm_calls: int = 0,
    paid_tool_cost_usd: float = 0.0,
    paid_tool_calls: int = 0,
) -> UsageAggregate:
    return UsageAggregate(
        input_tokens=1000,
        cached_read_tokens=500,
        cache_write_tokens=0,
        output_tokens=200,
        est_cost_usd=cost + paid_tool_cost_usd,
        llm_est_cost_usd=cost,
        paid_tool_cost_usd=paid_tool_cost_usd,
        unpriced_llm_calls=unpriced_llm_calls,
        turns=turns,
        llm_calls=turns,
        paid_tool_calls=paid_tool_calls,
    )


def test_format_user_usage_tabulates_every_window() -> None:
    text = format_user_usage(
        "u1",
        {
            "Today": _agg(0.0012),
            "Last 5h": _agg(0.12),
            "Last 7d": _agg(1.0),
            "Last 30d": _agg(
                3.0,
                unpriced_llm_calls=1,
                paid_tool_cost_usd=0.25,
                paid_tool_calls=2,
            ),
        },
    )

    assert text.startswith("```\nUsage for u1\n")
    assert "Window    Est. cost   Paid  Tokens  Turns" in text
    assert "Today       $0.0012  $0.00    1.7K      2" in text
    # The widest window carries the total, the paid split, and the caveats.
    assert "Last 30d      $3.25  $0.25    1.7K      2" in text
    assert "Paid is billed tool spend, already counted in est. cost." in text
    assert (
        "1 LLM call couldn't be priced in last 30d, "
        "so the estimated total may be lower than the actual cost."
    ) in text


def test_format_user_usage_drops_the_paid_column_without_paid_spend() -> None:
    text = format_user_usage("u1", {"Today": _agg(0.12)})

    assert "Paid" not in text
    assert "Window  Est. cost  Tokens  Turns" in text


def test_format_server_usage_ranks_top_spenders_under_the_total() -> None:
    spenders = [
        SpenderRow("u2", "Bob", 2.5, 0, 5000, 9),
        SpenderRow("u1", "Ann", 1.0, 1, 1000, 3),
    ]

    text = format_server_usage(_agg(3.5, turns=12, unpriced_llm_calls=1), spenders)

    assert "Server usage, last 30d" in text
    assert "Server total      $3.50    1.7K     12" in text
    assert "Top spenders" in text
    assert "1. Bob            $2.50    5.0K      9" in text
    assert "2. Ann            $1.00    1.0K      3" in text
    assert (
        "1 LLM call couldn't be priced in last 30d, "
        "so the estimated total may be lower than the actual cost."
    ) in text


def test_format_server_usage_reports_a_window_with_no_spenders() -> None:
    text = format_server_usage(_agg(0.0, turns=0), [])

    assert "Top spenders" not in text
    assert text.endswith("No member spend recorded in this window.")


def test_usage_tables_align_their_numeric_columns() -> None:
    text = format_user_usage("u1", {"Today": _agg(0.0012), "Last 30d": _agg(1234.5)})

    rows = [line for line in text.splitlines() if line.startswith(("Window", "Today", "Last 30d"))]
    assert len(rows) == 3
    assert len({len(row) for row in rows}) == 1


class _Tree:
    def __init__(self) -> None:
        self.commands: dict[str, Any] = {}

    def add_command(self, command: Any, *, override: bool = False) -> None:
        assert override is True
        self.commands[command.name] = command.callback


class _Response:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.deferred = False

    def is_done(self) -> bool:
        return self.deferred or bool(self.sent)

    async def defer(self, **kwargs: Any) -> None:
        self.deferred = True

    async def send_message(self, content: str, **kwargs: Any) -> None:
        self.sent.append((content, kwargs))


class _Followup:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []

    async def send(self, content: str, **kwargs: Any) -> None:
        self.sent.append((content, kwargs))


class _Interaction:
    def __init__(self, user_id: int = 5) -> None:
        self.user = SimpleNamespace(id=user_id, display_name=f"User {user_id}")
        self.guild_id = 999
        self.response = _Response()
        self.followup = _Followup()


class _Store:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    async def user_total(
        self,
        user_id: str,
        since: datetime,
        guild_id: str | None = None,
    ) -> UsageAggregate:
        self.calls.append(("user_total", user_id, since, guild_id))
        return _agg(0.12)

    async def server_total(self, guild_id: str | None, since: datetime) -> UsageAggregate:
        self.calls.append(("server_total", guild_id, since))
        return _agg(0.25)

    async def top_spenders(
        self,
        guild_id: str | None,
        since: datetime,
        *,
        limit: int,
    ) -> list[SpenderRow]:
        self.calls.append(("top_spenders", guild_id, since, limit))
        return [SpenderRow("5", "User 5", 0.25, 0, 1700, 2)]


@pytest.mark.asyncio
async def test_usage_command_allows_non_staff_to_query_their_own_usage() -> None:
    tree = _Tree()
    store = _Store()
    bot = SimpleNamespace(tree=tree)

    register_usage_command(
        cast(Any, bot),
        store,  # type: ignore[arg-type]
        TrustResolver(staff_role_ids=set(), regular_role_ids=set(), staff_ids=set()),
    )

    interaction = _Interaction(user_id=5)
    await tree.commands["usage"](interaction)

    assert interaction.response.deferred is True
    assert "Usage for User 5 (this server)" in interaction.followup.sent[0][0]
    assert [call[0] for call in store.calls] == ["user_total"] * 4
    assert all(call[1] == "5" and call[3] == "999" for call in store.calls)


@pytest.mark.asyncio
async def test_usage_command_blocks_non_staff_from_querying_another_user() -> None:
    tree = _Tree()
    store = _Store()
    bot = SimpleNamespace(tree=tree)

    register_usage_command(
        cast(Any, bot),
        store,  # type: ignore[arg-type]
        TrustResolver(staff_role_ids=set(), regular_role_ids=set(), staff_ids=set()),
    )

    interaction = _Interaction(user_id=5)
    target = SimpleNamespace(id=8, display_name="Target")
    await tree.commands["usage"](interaction, target)

    assert interaction.response.sent[0][0] == "Staff only for other-user usage."
    assert interaction.response.deferred is False
    assert store.calls == []


@pytest.mark.asyncio
async def test_usage_command_allows_staff_to_query_server_usage() -> None:
    tree = _Tree()
    store = _Store()
    bot = SimpleNamespace(tree=tree)

    register_usage_command(
        cast(Any, bot),
        store,  # type: ignore[arg-type]
        TrustResolver(staff_role_ids=set(), regular_role_ids=set(), staff_ids={"5"}),
    )

    interaction = _Interaction(user_id=5)
    await tree.commands["usage"](interaction)

    # Deferred before the aggregate queries, so the reply rides the followup.
    assert interaction.response.deferred is True
    assert interaction.response.sent == []
    assert "Server usage" in interaction.followup.sent[0][0]
    assert store.calls[0][0] == "server_total"
    assert store.calls[0][1] == "999"


@pytest.mark.asyncio
async def test_usage_command_user_query_includes_today_window() -> None:
    tree = _Tree()
    store = _Store()
    bot = SimpleNamespace(tree=tree)

    register_usage_command(
        cast(Any, bot),
        store,  # type: ignore[arg-type]
        TrustResolver(staff_role_ids=set(), regular_role_ids=set(), staff_ids={"5"}),
    )

    interaction = _Interaction(user_id=5)
    user = SimpleNamespace(id=8, display_name="Target")
    await tree.commands["usage"](interaction, user)

    assert interaction.response.deferred is True
    assert "Usage for Target (this server)" in interaction.followup.sent[0][0]
    assert "Today" in interaction.followup.sent[0][0]
    assert [call[0] for call in store.calls] == [
        "user_total",
        "user_total",
        "user_total",
        "user_total",
    ]
    # Per-user lookup is scoped to the guild the command ran in.
    assert all(call[3] == "999" for call in store.calls)
