from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import math
from typing import Any
from uuid import uuid4

from storage.db import Database
from usage.normalization import LLMUsageCall


USAGE_MARKER_RETENTION = timedelta(days=8)


@dataclass(frozen=True)
class UsageAggregate:
    input_tokens: int
    cached_read_tokens: int
    cache_write_tokens: int
    output_tokens: int
    est_cost_usd: float
    llm_est_cost_usd: float
    paid_tool_cost_usd: float
    unpriced_llm_calls: int
    turns: int
    llm_calls: int
    paid_tool_calls: int


@dataclass(frozen=True)
class PaidUsageCall:
    """One non-LLM backend charge incurred by a tool."""

    tool_name: str
    provider: str
    cost_usd: float


@dataclass(frozen=True)
class UsageMarker:
    unit_count: int
    created_at: datetime


@dataclass(frozen=True)
class ModelUsageRow:
    """One (model, role) pair's share of the LLM spend in a window.

    Split by role as well as model because the same model is routinely wired to
    several roles, and "compaction is costing more than chat" is exactly the kind
    of thing a single per-model total hides.
    """

    model: str
    role: str
    input_tokens: int
    cached_read_tokens: int
    cache_write_tokens: int
    output_tokens: int
    est_cost_usd: float
    llm_calls: int
    unpriced_llm_calls: int
    turns: int
    attribution: str


@dataclass(frozen=True)
class SpenderRow:
    user_id: str
    user_name: str | None
    est_cost_usd: float
    unpriced_llm_calls: int
    total_tokens: int
    turns: int
    paid_tool_cost_usd: float = 0.0
    paid_tool_calls: int = 0


_SUMS = (
    "COALESCE(SUM(input_tokens),0) AS input_tokens, "
    "COALESCE(SUM(cached_read_tokens),0) AS cached_read_tokens, "
    "COALESCE(SUM(cache_write_tokens),0) AS cache_write_tokens, "
    "COALESCE(SUM(output_tokens),0) AS output_tokens, "
    "COALESCE(SUM(est_cost_usd),0) AS est_cost_usd, "
    "COALESCE(SUM(CASE WHEN est_cost_usd IS NULL "
    "THEN CASE WHEN turn_id IS NULL AND iterations > 0 THEN iterations ELSE 1 END "
    "ELSE 0 END),0) AS unpriced_llm_calls, "
    "COUNT(DISTINCT turn_id) + "
    "COALESCE(SUM(CASE WHEN turn_id IS NULL THEN 1 ELSE 0 END),0) AS turns, "
    "COALESCE(SUM(CASE WHEN turn_id IS NULL "
    "THEN CASE WHEN iterations > 0 THEN iterations ELSE 1 END "
    "ELSE 1 END),0) AS llm_calls"
)


class UsageStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def record_turn(
        self,
        *,
        user_id: str,
        user_name: str | None,
        channel_id: str | None,
        guild_id: str | None,
        calls: Sequence[LLMUsageCall],
        turn_id: str | None = None,
    ) -> None:
        """Persist one row per completed LLM call under one logical turn."""
        if not calls:
            return
        ledger_turn_id = turn_id or uuid4().hex
        created_at = datetime.now(UTC).isoformat()
        rows = [
            (
                user_id,
                user_name,
                channel_id,
                guild_id,
                call.model,
                call.role,
                call.pricing_model,
                call.usage.input_tokens,
                call.usage.cached_read_tokens,
                call.usage.cache_write_tokens,
                call.usage.output_tokens,
                1,
                call.est_cost_usd,
                ledger_turn_id,
                created_at,
            )
            for call in calls
        ]
        async with self._db.write_transaction() as conn:
            await conn.executemany(
                "INSERT INTO usage_ledger ("
                "user_id, user_name, channel_id, guild_id, model, role, pricing_model, "
                "input_tokens, cached_read_tokens, cache_write_tokens, output_tokens, "
                "iterations, est_cost_usd, turn_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )

    async def record_paid_usage(
        self,
        *,
        user_id: str,
        user_name: str | None,
        channel_id: str | None,
        guild_id: str | None,
        calls: Sequence[PaidUsageCall],
        turn_id: str | None = None,
    ) -> None:
        """Persist one row per tool backend that actually charged money.

        A reported zero is authoritative and an absent price is represented by
        omitting the call. Negative and non-finite amounts are programmer or
        provider-contract errors, never ledger data.
        """
        billed = []
        for call in calls:
            if not math.isfinite(call.cost_usd) or call.cost_usd < 0:
                raise ValueError("paid tool cost must be finite and non-negative")
            if call.cost_usd == 0:
                continue
            if not call.tool_name or not call.provider:
                raise ValueError("paid tool usage requires a tool name and provider")
            billed.append(call)
        if not billed:
            return

        ledger_turn_id = turn_id or uuid4().hex
        created_at = datetime.now(UTC).isoformat()
        rows = [
            (
                user_id,
                user_name,
                channel_id,
                guild_id,
                call.tool_name,
                call.provider,
                call.cost_usd,
                ledger_turn_id,
                created_at,
            )
            for call in billed
        ]
        async with self._db.write_transaction() as conn:
            await conn.executemany(
                "INSERT INTO paid_usage_ledger ("
                "user_id, user_name, channel_id, guild_id, tool_name, provider, "
                "cost_usd, turn_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )

    async def record_usage_marker(
        self,
        *,
        user_id: str,
        surface: str,
        operation: str,
        unit_count: int = 1,
        user_name: str | None = None,
        channel_id: str | None = None,
        guild_id: str | None = None,
    ) -> None:
        """Record a zero-cost rate-limit counter outside the spend ledgers."""
        units = max(1, int(unit_count))
        now = datetime.now(UTC)
        async with self._db.write_transaction() as conn:
            await conn.execute(
                "DELETE FROM usage_markers WHERE created_at < ?",
                (_iso_utc(now - USAGE_MARKER_RETENTION),),
            )
            await conn.execute(
                "INSERT INTO usage_markers ("
                "user_id, user_name, channel_id, guild_id, surface, operation, "
                "unit_count, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    user_id,
                    user_name,
                    channel_id,
                    guild_id,
                    surface,
                    operation,
                    units,
                    now.isoformat(),
                ),
            )

    async def usage_markers(
        self,
        user_id: str,
        *,
        surfaces: Sequence[str],
        since: datetime,
    ) -> list[UsageMarker]:
        """Return a user's bounded-tool counters in the requested time window."""
        if not surfaces:
            return []
        placeholders = ",".join("?" for _ in surfaces)
        params = (user_id, *surfaces, _iso_utc(since))
        async with self._db.conn.execute(
            "SELECT unit_count, created_at FROM usage_markers "
            f"WHERE user_id = ? AND surface IN ({placeholders}) AND created_at >= ? "
            "ORDER BY created_at",
            params,
        ) as cur:
            rows = await cur.fetchall()
        return [
            UsageMarker(
                unit_count=int(row["unit_count"]),
                created_at=datetime.fromisoformat(str(row["created_at"])),
            )
            for row in rows
        ]

    async def user_total(
        self,
        user_id: str,
        since: datetime,
        guild_id: str | None = None,
    ) -> UsageAggregate:
        if guild_id is None:
            sql = f"SELECT {_SUMS} FROM usage_ledger WHERE user_id = ? AND created_at >= ?"
            params: tuple[Any, ...] = (user_id, _iso_utc(since))
        else:
            sql = (
                f"SELECT {_SUMS} FROM usage_ledger "
                "WHERE user_id = ? AND guild_id = ? AND created_at >= ?"
            )
            params = (user_id, guild_id, _iso_utc(since))
        async with self._db.conn.execute(sql, params) as cur:
            row = await cur.fetchone()
        paid_cost, paid_calls = await self._paid_total(
            since,
            user_id=user_id,
            guild_id=guild_id,
        )
        turns = await self._turn_total(since, user_id=user_id, guild_id=guild_id)
        return _aggregate(row, paid_cost=paid_cost, paid_calls=paid_calls, turns=turns)

    async def server_total(
        self,
        guild_id: str | None,
        since: datetime,
    ) -> UsageAggregate:
        if guild_id is None:
            sql = f"SELECT {_SUMS} FROM usage_ledger WHERE created_at >= ?"
            params: tuple[Any, ...] = (_iso_utc(since),)
        else:
            sql = f"SELECT {_SUMS} FROM usage_ledger WHERE guild_id = ? AND created_at >= ?"
            params = (guild_id, _iso_utc(since))
        async with self._db.conn.execute(sql, params) as cur:
            row = await cur.fetchone()
        paid_cost, paid_calls = await self._paid_total(since, guild_id=guild_id)
        turns = await self._turn_total(since, guild_id=guild_id)
        return _aggregate(row, paid_cost=paid_cost, paid_calls=paid_calls, turns=turns)

    async def _paid_total(
        self,
        since: datetime,
        *,
        user_id: str | None = None,
        guild_id: str | None = None,
    ) -> tuple[float, int]:
        clauses = ["created_at >= ?"]
        params: list[Any] = [_iso_utc(since)]
        if user_id is not None:
            clauses.append("user_id = ?")
            params.append(user_id)
        if guild_id is not None:
            clauses.append("guild_id = ?")
            params.append(guild_id)
        async with self._db.conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) AS cost_usd, COUNT(*) AS calls "
            f"FROM paid_usage_ledger WHERE {' AND '.join(clauses)}",
            tuple(params),
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        return float(row["cost_usd"]), int(row["calls"])

    async def _turn_total(
        self,
        since: datetime,
        *,
        user_id: str | None = None,
        guild_id: str | None = None,
    ) -> int:
        clauses = ["created_at >= ?"]
        params: list[Any] = [_iso_utc(since)]
        if user_id is not None:
            clauses.append("user_id = ?")
            params.append(user_id)
        if guild_id is not None:
            clauses.append("guild_id = ?")
            params.append(guild_id)
        where = " AND ".join(clauses)
        async with self._db.conn.execute(
            "SELECT COUNT(*) AS turns FROM ("
            f"SELECT COALESCE(turn_id, 'llm:' || id) AS turn_key FROM usage_ledger WHERE {where} "
            "UNION "
            f"SELECT COALESCE(turn_id, 'paid:' || id) FROM paid_usage_ledger WHERE {where}"
            ")",
            (*params, *params),
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        return int(row["turns"])

    async def top_spenders(
        self,
        guild_id: str | None,
        since: datetime,
        limit: int,
    ) -> list[SpenderRow]:
        guild_filter = "" if guild_id is None else "AND guild_id = :guild_id"
        sql = f"""
        WITH llm AS (
        SELECT
            user_id,
            MAX(user_name) AS user_name,
            COALESCE(SUM(est_cost_usd),0) AS est_cost_usd,
            COALESCE(SUM(CASE WHEN est_cost_usd IS NULL
                THEN CASE WHEN turn_id IS NULL AND iterations > 0
                    THEN iterations ELSE 1 END
                ELSE 0 END),0) AS unpriced_llm_calls,
            COALESCE(SUM(input_tokens+cached_read_tokens+cache_write_tokens+output_tokens),0)
                AS total_tokens,
            COUNT(DISTINCT turn_id)
                + COALESCE(SUM(CASE WHEN turn_id IS NULL THEN 1 ELSE 0 END),0)
                AS turns
        FROM usage_ledger
        WHERE created_at >= :since {guild_filter}
        GROUP BY user_id
        ), paid AS (
            SELECT user_id, MAX(user_name) AS user_name,
                COALESCE(SUM(cost_usd), 0) AS cost_usd, COUNT(*) AS calls
            FROM paid_usage_ledger
            WHERE created_at >= :since {guild_filter}
            GROUP BY user_id
        ), turns AS (
            SELECT user_id, COUNT(*) AS turns FROM (
                SELECT user_id, COALESCE(turn_id, 'llm:' || id) AS turn_key
                FROM usage_ledger WHERE created_at >= :since {guild_filter}
                UNION
                SELECT user_id, COALESCE(turn_id, 'paid:' || id)
                FROM paid_usage_ledger WHERE created_at >= :since {guild_filter}
            ) GROUP BY user_id
        ), users AS (
            SELECT user_id FROM llm UNION SELECT user_id FROM paid
        )
        SELECT users.user_id,
            COALESCE(llm.user_name, paid.user_name) AS user_name,
            COALESCE(llm.est_cost_usd, 0) + COALESCE(paid.cost_usd, 0) AS est_cost_usd,
            COALESCE(llm.unpriced_llm_calls, 0) AS unpriced_llm_calls,
            COALESCE(llm.total_tokens, 0) AS total_tokens,
            COALESCE(turns.turns, 0) AS turns,
            COALESCE(paid.cost_usd, 0) AS paid_tool_cost_usd,
            COALESCE(paid.calls, 0) AS paid_tool_calls
        FROM users
        LEFT JOIN llm ON llm.user_id = users.user_id
        LEFT JOIN paid ON paid.user_id = users.user_id
        LEFT JOIN turns ON turns.user_id = users.user_id
        ORDER BY est_cost_usd DESC, total_tokens DESC LIMIT :limit
        """
        params: dict[str, Any] = {"since": _iso_utc(since), "limit": limit}
        if guild_id is not None:
            params["guild_id"] = guild_id
        async with self._db.conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [
            SpenderRow(
                user_id=str(row["user_id"]),
                user_name=row["user_name"],
                est_cost_usd=float(row["est_cost_usd"]),
                unpriced_llm_calls=int(row["unpriced_llm_calls"]),
                total_tokens=int(row["total_tokens"]),
                turns=int(row["turns"]),
                paid_tool_cost_usd=float(row["paid_tool_cost_usd"]),
                paid_tool_calls=int(row["paid_tool_calls"]),
            )
            for row in rows
        ]

    async def usage_by_model(
        self,
        since: datetime,
        *,
        guild_id: str | None = None,
        user_id: str | None = None,
    ) -> list[ModelUsageRow]:
        """LLM spend grouped by (model, role), most expensive first.

        The scope filters mirror the totals they sit beside, so a per-model
        breakdown always sums to the aggregate shown above it.
        """
        clauses = ["created_at >= ?"]
        params: list[Any] = [_iso_utc(since)]
        if guild_id is not None:
            clauses.append("guild_id = ?")
            params.append(guild_id)
        if user_id is not None:
            clauses.append("user_id = ?")
            params.append(user_id)
        where = " AND ".join(clauses)
        async with self._db.conn.execute(
            f"SELECT model, role, CASE WHEN turn_id IS NULL THEN 'unattributed' "
            f"ELSE 'per_call' END AS attribution, {_SUMS} "
            f"FROM usage_ledger WHERE {where} "
            "GROUP BY model, role, attribution "
            "ORDER BY est_cost_usd DESC, turns DESC",
            tuple(params),
        ) as cur:
            rows = await cur.fetchall()
        return [
            ModelUsageRow(
                model=str(row["model"]),
                role=str(row["role"] or ""),
                input_tokens=int(row["input_tokens"]),
                cached_read_tokens=int(row["cached_read_tokens"]),
                cache_write_tokens=int(row["cache_write_tokens"]),
                output_tokens=int(row["output_tokens"]),
                est_cost_usd=float(row["est_cost_usd"]),
                llm_calls=int(row["llm_calls"]),
                unpriced_llm_calls=int(row["unpriced_llm_calls"]),
                turns=int(row["turns"]),
                attribution=str(row["attribution"]),
            )
            for row in rows
        ]


def _iso_utc(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat()


def _aggregate(
    row: Any,
    *,
    paid_cost: float = 0.0,
    paid_calls: int = 0,
    turns: int | None = None,
) -> UsageAggregate:
    llm_cost = float(row["est_cost_usd"])
    return UsageAggregate(
        input_tokens=int(row["input_tokens"]),
        cached_read_tokens=int(row["cached_read_tokens"]),
        cache_write_tokens=int(row["cache_write_tokens"]),
        output_tokens=int(row["output_tokens"]),
        est_cost_usd=llm_cost + paid_cost,
        llm_est_cost_usd=llm_cost,
        paid_tool_cost_usd=paid_cost,
        unpriced_llm_calls=int(row["unpriced_llm_calls"]),
        turns=int(row["turns"]) if turns is None else turns,
        llm_calls=int(row["llm_calls"]),
        paid_tool_calls=paid_calls,
    )
